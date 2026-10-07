# -*- coding: utf-8 -*-
"""
live_probe.py - 「关注目标有没有更新」探测器

这是实时链路里最便宜的一层。它只做一件事：
  只看目标会话那一行有没有变，不碰 54MB 的 message_0.db，也不碰 22MB 的 FTS。

做法：只解密 session.db（475 KB / 116 pages，含 WAL 补丁），读 SessionTable 里目标那一行。

为什么必须带 WAL（2026-10-07 实测）：
  vendor 的 decrypt_database() 只读主文件、不读 -wal。而这台机器上
  session.db-wal 有 4MB、里面有 salt 匹配的新帧。实测只读主文件会让
  last_timestamp 停在 10-04 21:59，而真实值是 10-07 14:37 —— 差 2 天 16 小时。

变更判定用 last_msg_locald_id（严格递增的整数），不是时间戳 ——
同秒内的两条消息时间戳可能相同。last_msg_sender 在这个会话里是空字符串，
所以 reply_pending 不再瞎猜，由调用方从 FTS 的 sender_id 推。

退出码 0 总是表示「探测本身成功了」；有没有新消息看输出里的 changed。

用法：
  python scripts/live_probe.py --bundle data
  python scripts/live_probe.py --bundle data --json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import live_decrypt  # noqa: E402

DEFAULT_VENDOR = os.path.join(REPO_ROOT, 'vendor', 'wechat-decrypt')
PROBE_STATE = 'probe_state.json'
SESSION_REL = os.path.join('session', 'session.db')


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except Exception:
        return default


def write_json(path, payload):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def probe(bundle_dir, vendor_dir, dst_dir=None):
    messages_path = os.path.join(bundle_dir, 'messages.json')
    payload = read_json(messages_path)
    if payload is None:
        raise RuntimeError('读不到 messages.json：' + messages_path)
    username = payload.get('contact_username')
    if not username:
        raise RuntimeError('messages.json 里没有 contact_username，无法定位会话')

    # 解密产物放哪：默认跟 bundle 同级，调用方可指到私有目录
    dst_dir = dst_dir or os.path.join(bundle_dir, 'live', 'decrypted')
    dst = os.path.join(dst_dir, 'session.db')

    state_path = os.path.join(bundle_dir, PROBE_STATE)
    previous = read_json(state_path, {}) or {}
    dec_state = dict(previous.get('decrypt') or {})

    started = time.time()
    result = live_decrypt.refresh(SESSION_REL, dst, vendor_dir, state=dec_state)
    decrypt_seconds = round(time.time() - started, 3)

    con = live_decrypt.open_ro(dst)
    try:
        row = con.execute(
            'SELECT last_timestamp, unread_count, last_msg_type, last_msg_sender,'
            ' last_msg_locald_id, summary, last_sender_display_name, draft'
            ' FROM SessionTable WHERE username = ?',
            (username,),
        ).fetchone()
    finally:
        con.close()

    if row is None:
        return {
            'status': 'ok',
            'changed': None,
            'reason': 'session.db 里找不到这个会话（可能被清过）',
            'found': False,
            'decrypt_mode': result.get('mode'),
            'decrypt_seconds': decrypt_seconds,
        }

    last_ts = float(row[0] or 0)
    unread = int(row[1] or 0)
    last_type = row[2]
    last_sender = row[3] or ''
    last_mlid = int(row[4] or 0)
    summary = row[5] or ''

    prev_ts = previous.get('last_timestamp')
    prev_unread = previous.get('unread_count')
    prev_mlid = previous.get('last_msg_locald_id')

    if prev_ts is None:
        changed, reason = True, '首次探测，建立基线'
    elif prev_mlid is None:
        # 老的状态文件没有 local_id 字段，退回时间戳比较
        changed = last_ts > float(prev_ts)
        reason = '时间戳变化（旧状态文件，无 local_id）'
    elif last_mlid != int(prev_mlid):
        delta = last_mlid - int(prev_mlid)
        changed = True
        reason = '目标会话新增 %d 条（local_id %s -> %s）' % (delta, prev_mlid, last_mlid)
    elif last_ts > float(prev_ts):
        changed = True
        reason = '目标会话有 %.1f 分钟的新内容（local_id 未变）' % (
            (last_ts - float(prev_ts)) / 60)
    elif unread != prev_unread:
        changed = True
        reason = '未读数变化 %s -> %s' % (prev_unread, unread)
    else:
        changed, reason = False, '目标会话没有更新'

    write_json(state_path, {
        'checked_at': time.time(),
        'checked_at_human': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'last_timestamp': last_ts,
        'unread_count': unread,
        'last_msg_locald_id': last_mlid,
        'last_msg_type': last_type,
        'source_mtime': result.get('db_mtime'),
        'wal_mtime': result.get('wal_mtime'),
        'decrypt': dec_state,
    })

    return {
        'status': 'ok',
        'changed': changed,
        'reason': reason,
        'found': True,
        'decrypt_mode': result.get('mode'),
        'decrypt_seconds': decrypt_seconds,
        'last_timestamp': last_ts,
        'last_message_human': datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M:%S'),
        'age_seconds': round(time.time() - last_ts, 1),
        'last_msg_locald_id': last_mlid,
        'unread_count': unread,
        'last_msg_type': last_type,
        'last_msg_sender': last_sender,
        'summary': summary,
        # last_msg_sender 在这个会话里是空的，不再据此瞎猜谁在等回复。
        # 权威的 sender 从 FTS 的 sender_id 读（见 live_ingest）。
        'sender_known': bool(last_sender),
        'probe_state': state_path,
    }


def main():
    parser = argparse.ArgumentParser(description='探测关注目标会话有没有更新（只解 session.db）')
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--vendor', default=DEFAULT_VENDOR)
    parser.add_argument('--decrypted-dir', help='解密产物目录（默认 <bundle>/live/decrypted）')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    bundle = os.path.abspath(args.bundle)
    if not os.path.isdir(bundle):
        print(json.dumps({'status': 'error', 'error': 'bundle 不存在：' + bundle},
                         ensure_ascii=False))
        return 2

    started = time.time()
    try:
        result = probe(bundle, os.path.abspath(args.vendor),
                       os.path.abspath(args.decrypted_dir) if args.decrypted_dir else None)
    except Exception as exc:  # noqa: BLE001 - CLI 边界
        print(json.dumps({'status': 'error',
                          'error': type(exc).__name__ + ': ' + str(exc)},
                         ensure_ascii=False))
        return 2
    result['total_seconds'] = round(time.time() - started, 3)
    if not args.json:
        result.pop('probe_state', None)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
