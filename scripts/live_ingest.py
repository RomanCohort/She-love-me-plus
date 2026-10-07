# -*- coding: utf-8 -*-
"""
live_ingest.py - 实时链路的摄取层：把「源库里的新消息」变成「live 产物」

这是方案里原先缺的那一环。live_probe 只负责说「会话变了」，但没有任何东西
把变化真的取回来。本模块就是那一步。

一次 tick 做这些事：
  1. 读 bundle 的 messages.json —— 已知消息 + 已知最大时间戳
  2. 把 message_fts.db(+WAL) 解密/更新到 <live>/decrypted/  （私有路径）
  3. list_fts_tables → identify_session（缓存）→ extract_since(增量)
  4. msg_key 去重
  5. 追加 <live>/live_messages.jsonl
  6. 重写 <live>/live_view.json（原子写）

绝不写 messages.json。理由：extract_messages.py 会整体重写它；两个写者
一个文件；实时尾巴会失去来源可追溯性；326KB 的 JSON 重写不是崩溃安全的。
要合并进主库只能走显式 --promote。

为什么 FTS 是唯一通道（2026-10-07 实测）：
  message_0.db 的 Msg_<md5> 表冻在 2026-07-05；message_1.db（24MB，今天还在写）
  在 all_keys.json 里没有密钥。所以 07-05 之后的每一条消息都只存在于 FTS。

看门狗：
  list_fts_tables 是正则驱动的，分片轮换（v4_4 出现）自动纳入。但如果微信
  升级后表名不再匹配这个正则，它会静默地少匹配几张表、永远报「0 条新消息」。
  所以每次都记录 tables_found 和每表行数，数量掉下来就报 health="shrunk"。

用法：
  python scripts/live_ingest.py --bundle data --live data/live --json
  python scripts/live_ingest.py --bundle data --live data/live --since 2026-07-04 --json
"""

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import fts_fallback as F  # noqa: E402
import live_decrypt  # noqa: E402

DEFAULT_VENDOR = os.path.join(REPO_ROOT, 'vendor', 'wechat-decrypt')
FTS_REL = os.path.join('message', 'message_fts.db')
STATE_NAME = 'ingest_state.json'
TAIL_NAME = 'live_messages.jsonl'
VIEW_NAME = 'live_view.json'
STATE_VERSION = 1
INBOX_NAME = 'inbox.json'
ANNOTATIONS_DIR = 'annotations'
ANNOTATION_SCHEMA_VERSION = '1.0.0'


# ---------------------------------------------------------------- 读写工具

def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except Exception:
        return default


def write_json(path, payload):
    """原子写：先写 .tmp 再 replace。守护进程崩在中途也不会留半截文件。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_tail(path):
    """读只追加的 jsonl。坏行跳过 —— 崩在写一半时最后一行可能是残的。"""
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, 'r', encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_tail(path, records):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as handle:
        for r in records:
            handle.write(json.dumps(r, ensure_ascii=False) + '\n')


# ---------------------------------------------------------------- 标注 inbox

def covered_msg_keys(live_dir):
    """annotations/ 下所有文件的 per_message[].msg_key 并集 —— 「已覆盖」的权威口径。

    一条消息只要出现在任何一份标注的 per_message 里就算已覆盖（不管 state 是
    done 还是 skipped）——skipped 也是「看过、决定不标」，不是「漏了」。

    容错：文件接口的读侧必须宽容。坏 JSON（read_json 已兜底）、body 不是 dict、
    per_message 里的元素不是 dict、msg_key 缺失——全部跳过，绝不因为一个坏标注
    文件就让守护进程的 ingest tick 崩溃（这些文件是 Claude 会话手写的，会崩）。
    """
    keys = set()
    ann_dir = os.path.join(live_dir, ANNOTATIONS_DIR)
    for path in glob.glob(os.path.join(ann_dir, '*.json')):
        body = read_json(path)
        if not isinstance(body, dict):
            continue
        for pm in body.get('per_message') or []:
            if not isinstance(pm, dict):
                continue
            k = pm.get('msg_key')
            if k:
                keys.add(k)
    return keys


def build_inbox(live_dir, tail, request_id, contact):
    """把「还没被任何 annotation 覆盖的尾巴消息」打包成 inbox.json 的 body。

    守护进程永不调 LLM，也永不修改已存在 request 的 messages 数组——
    这里是纯数据差集：tail 里没出现在 annotations 并集里的，就是待标注。
    """
    covered = covered_msg_keys(live_dir)
    pending = [m for m in tail if m.get('msg_key') and m.get('msg_key') not in covered]

    messages = []
    for m in pending:
        messages.append({
            'msg_key': m.get('msg_key'),
            'local_id': m.get('local_id'),
            'sender': m.get('sender'),
            'timestamp': m.get('timestamp'),
            'type': m.get('type'),
            'content': m.get('content'),
            'quote': m.get('quote'),
            'text_missing': bool(m.get('text_missing')),
            'source': m.get('source'),
        })

    ts = [m.get('timestamp') for m in pending if m.get('timestamp')]
    from_ts = int(min(ts)) if ts else None
    to_ts = int(max(ts)) if ts else None
    keys = [m.get('msg_key') for m in pending if m.get('msg_key')]
    text_missing = sum(1 for m in pending if m.get('text_missing'))

    return {
        'schema_version': ANNOTATION_SCHEMA_VERSION,
        'request_id': request_id,
        'created_at': time.time(),
        'contact': contact or {},
        'cursor': {
            'from_ts': from_ts,
            'to_ts': to_ts,
            'from_msg_key': keys[0] if keys else None,
            'to_msg_key': keys[-1] if keys else None,
        },
        'counts': {
            'new': len(pending),
            'pending_total': len(pending),
            'text_missing': text_missing,
        },
        'messages': messages,
        'context': {
            'reply_pending': None,
            'pending_duration': None,
            'hour': int(datetime.fromtimestamp(to_ts).hour) if to_ts else None,
            'data_age_seconds': round(time.time() - to_ts, 1) if to_ts else None,
            'advice': {},
            'stats_summary': {},
        },
    }


def write_inbox(live_dir, tail, contact):
    """写 inbox.json（原子写）。any_pending 为 True 时才写，为 False 时删掉旧的。"""
    request_id = 'r-%s-%05d' % (datetime.now().strftime('%Y%m%dT%H%M%S'), os.getpid() % 100000)
    body = build_inbox(live_dir, tail, request_id, contact)
    path = os.path.join(live_dir, INBOX_NAME)
    if body['counts']['pending_total'] > 0:
        write_json(path, body)
    elif os.path.exists(path):
        os.remove(path)
    return body


# ---------------------------------------------------------------- 会话识别缓存

def table_row_counts(conn, tables, session_id=None):
    """每张分片的行数（可选按 session 过滤）。给看门狗用。"""
    counts = {}
    for t in tables:
        try:
            if session_id is None:
                n = conn.execute('SELECT COUNT(*) FROM [%s]' % t).fetchone()[0]
            else:
                n = conn.execute(
                    'SELECT COUNT(*) FROM [%s] WHERE session_id=?' % t,
                    (session_id,)).fetchone()[0]
        except Exception:  # noqa: BLE001 - 分片结构异常不该中断
            n = None
        counts[t] = n
    return counts


def resolve_identity(conn, tables, known_messages, state, force=False):
    """拿到 (session_id, me_id, them_id, hits)，尽量走缓存。

    identify_session 会遍历每张分片的每一行，是冷启动级别的开销
    （60 张表 × 22MB），不能每 tick 都跑。
    """
    cached = state.get('identity')
    if cached and not force and cached.get('hits', 0) >= 3:
        # 缓存可用，但仍要确认那个 session 在现在的分片里还有行 —— 分片轮换后
        # 旧的 session_id 可能整片搬走
        tables_now = set(tables)
        if set(cached.get('tables') or []) <= tables_now:
            return (cached['session_id'], cached['me_sender_id'],
                    cached['them_sender_id'], cached['hits'])
    ident = F.identify_session(conn, tables, known_messages)
    if not ident:
        return None
    sid, me_id, them_id, hits = ident
    state['identity'] = {
        'session_id': sid, 'me_sender_id': me_id, 'them_sender_id': them_id,
        'hits': hits, 'tables': sorted(tables),
        'resolved_at': time.time(),
    }
    return ident


# ---------------------------------------------------------------- 主流程

def ingest(bundle_dir, live_dir, vendor_dir=DEFAULT_VENDOR, since=None,
           force_ident=False, session_row=None, verbose=False):
    """跑一次摄取。返回一份可 JSON 化的结果。"""
    started = time.time()
    bundle_dir = os.path.abspath(bundle_dir)
    live_dir = os.path.abspath(live_dir)

    payload = read_json(os.path.join(bundle_dir, 'messages.json'))
    if payload is None:
        raise RuntimeError('读不到 messages.json：' + os.path.join(bundle_dir, 'messages.json'))
    known = payload.get('messages') or []
    if not known:
        raise RuntimeError('messages.json 里没有消息，无法建立锚点')

    state_path = os.path.join(live_dir, STATE_NAME)
    tail_path = os.path.join(live_dir, TAIL_NAME)
    state = read_json(state_path, {}) or {}
    state.setdefault('version', STATE_VERSION)

    # ---- 2. 解密 FTS 到私有路径（带 WAL 补丁）
    dst = os.path.join(live_dir, 'decrypted', 'message_fts.db')
    dec_state = dict(state.get('decrypt') or {})
    dec = live_decrypt.refresh(FTS_REL, dst, vendor_dir, state=dec_state)
    state['decrypt'] = dec_state

    conn = live_decrypt.open_ro(dst)
    try:
        tables = F.list_fts_tables(conn)
        if not tables:
            raise RuntimeError('message_fts.db 里没有 FTS 分片表（表名正则可能已失效）')

        ident = resolve_identity(conn, tables, known, state, force=force_ident)
        if not ident:
            raise RuntimeError('锚点匹配失败，无法在 FTS 中定位该联系人')
        sid, me_id, them_id, hits = ident
        if me_id is None or them_id is None:
            raise RuntimeError('锚点不足，无法确定双方 sender_id')

        # ---- 游标：已知消息 + 已摄取尾巴里的最大时间戳
        tail = read_tail(tail_path)
        stamps = [m.get('timestamp') for m in known if m.get('timestamp')]
        stamps += [m.get('timestamp') for m in tail if m.get('timestamp')]
        if since is not None:
            cursor = int(since)
        else:
            cursor = int(max(stamps)) if stamps else 0

        new_rows = F.extract_since(conn, tables, sid, me_id, them_id, cursor)
        counts = table_row_counts(conn, tables, sid)
    finally:
        conn.close()

    # ---- 4. 去重：对「已知 + 已摄取」一起判重
    seen = {F.msg_key_of(m) for m in known}
    seen |= {F.msg_key_of(m) for m in tail}
    fresh = []
    for r in new_rows:
        key = F.msg_key_of(r)
        if key in seen:
            continue
        seen.add(key)
        r = dict(r)
        r['msg_key'] = key
        r['ingested_at'] = time.time()
        fresh.append(r)

    if fresh:
        append_tail(tail_path, fresh)
        tail = tail + fresh

    # ---- 非文本盲区：会话前进了但 FTS 一条都没取到
    synthesized = None
    if not fresh and session_row:
        prev_ts = state.get('last_session_ts')
        cur_ts = session_row.get('last_timestamp')
        if cur_ts and prev_ts and float(cur_ts) > float(prev_ts):
            synthesized = {
                'local_id': None,
                'sender': 'unknown',
                'content': session_row.get('summary') or '',
                'timestamp': int(float(cur_ts)),
                'type': 'nontext' if session_row.get('last_msg_type') != 1 else 'text',
                'local_type': session_row.get('last_msg_type'),
                'source': 'session_only',
                'text_missing': True,
                'msg_key': F.msg_key('session', int(float(cur_ts)),
                                     session_row.get('summary') or ''),
                'ingested_at': time.time(),
                'note': 'FTS 没有这一条（非文本或尚未建索引）；'
                        '内容不可得，时序按 session.db 补齐',
            }
            append_tail(tail_path, [synthesized])
            tail = tail + [synthesized]
    if session_row and session_row.get('last_timestamp'):
        state['last_session_ts'] = session_row['last_timestamp']

    # ---- 看门狗
    prev_tables = state.get('tables_found') or []
    health = 'ok'
    if prev_tables and set(tables) < set(prev_tables):
        health = 'shrunk'
    elif not prev_tables:
        health = 'baseline'
    if not fresh and not synthesized:
        misses = int(state.get('consecutive_empty') or 0) + 1
        state['consecutive_empty'] = misses
        if misses >= 2 and session_row and session_row.get('changed'):
            health = 'ingest_stalled'
    else:
        state['consecutive_empty'] = 0

    state.update({
        'contact': {
            'display': payload.get('contact_display'),
            'username': payload.get('contact_username'),
        },
        'tables_found': sorted(tables),
        'table_rows': counts,
        'cursor': cursor,
        'last_run': time.time(),
        'last_run_human': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'health': health,
        'tail_count': len(tail),
    })
    write_json(state_path, state)

    # ---- 6. 仪表盘快照
    view = build_view(bundle_dir, payload, live_dir, state, tail, dec, fresh,
                      synthesized, health, hits, sid)
    write_json(os.path.join(live_dir, VIEW_NAME), view)

    # ---- 7. 标注 inbox：把还没被任何 annotation 覆盖的消息打包给 Claude 会话
    write_inbox(live_dir, tail, state.get('contact') or {})

    result = {
        'status': 'ok',
        'health': health,
        'session_id': sid,
        'ident_hits': hits,
        'cursor': cursor,
        'cursor_human': datetime.fromtimestamp(cursor).strftime('%Y-%m-%d %H:%M:%S') if cursor else None,
        'scanned': len(new_rows),
        'added': len(fresh),
        'tail_count': len(tail),
        'synthesized': bool(synthesized),
        'decrypt_mode': dec.get('mode'),
        'decrypt_wal_frames': dec.get('patched'),
        'tables': len(tables),
        'elapsed': round(time.time() - started, 3),
    }
    if verbose:
        print('[live_ingest] %s' % json.dumps(result, ensure_ascii=False),
              file=sys.stderr)
    return result


def build_view(bundle_dir, payload, live_dir, state, tail, dec, fresh,
               synthesized, health, hits, sid):
    """组装 live_view.json —— 仪表盘读的是这份。"""
    known_n = len(payload.get('messages') or [])
    last = tail[-1] if tail else None
    return {
        'schema_version': '1.0.0',
        'generated_at': time.time(),
        'generated_at_human': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'contact': state.get('contact') or {},
        'counts': {
            'bundle_messages': known_n,
            'live_tail': len(tail),
            'total': known_n + len(tail),
            'added_this_tick': len(fresh),
        },
        'cursor': state.get('cursor'),
        'health': health,
        'identity': {'session_id': sid, 'anchor_hits': hits},
        'decrypt': {
            'mode': dec.get('mode'),
            'wal_frames': dec.get('patched'),
            'db_mtime': dec.get('db_mtime'),
            'wal_mtime': dec.get('wal_mtime'),
            'stale_risk': bool(dec.get('stale_risk')),
            'wal_error': dec.get('wal_error'),
        },
        'watchdog': {
            'tables_found': state.get('tables_found') or [],
            'table_rows': state.get('table_rows') or {},
            'consecutive_empty': state.get('consecutive_empty') or 0,
        },
        'last_message': {
            'msg_key': last.get('msg_key'),
            'sender': last.get('sender'),
            'timestamp': last.get('timestamp'),
            'human': datetime.fromtimestamp(last['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
                     if last and last.get('timestamp') else None,
            'text_missing': bool(last.get('text_missing')) if last else None,
        } if last else None,
        'blind_spots': {
            'text_missing_in_tail': sum(1 for m in tail if m.get('text_missing')),
            'synthesized_last_tick': bool(synthesized),
            'note': 'message_1.db 无密钥，FTS 只索引文本 —— '
                    '07-05 之后的图片/表情/语音内容结构上不可得',
        },
        # 数据源指针，不复制正文：仪表盘自己读 bundle 拿历史
        'sources': {
            'bundle_messages': os.path.join(bundle_dir, 'messages.json'),
            'live_tail': os.path.join(live_dir, TAIL_NAME),
        },
        'tail': tail[-200:],
    }


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description='把源库里的新消息摄取进 live 产物')
    parser.add_argument('--bundle', default=os.path.join(REPO_ROOT, 'data'))
    parser.add_argument('--live', default=os.path.join(REPO_ROOT, 'data', 'live'))
    parser.add_argument('--vendor', default=DEFAULT_VENDOR)
    parser.add_argument('--since', help='YYYY-MM-DD 或 epoch；默认从已知最大时间戳续')
    parser.add_argument('--force-ident', action='store_true', help='忽略缓存的会话识别')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    since = None
    if args.since:
        try:
            since = int(float(args.since))
        except ValueError:
            since = int(datetime.strptime(args.since, '%Y-%m-%d').timestamp())

    try:
        result = ingest(args.bundle, args.live, args.vendor, since=since,
                        force_ident=args.force_ident)
    except Exception as exc:  # noqa: BLE001 - CLI 边界
        print(json.dumps({'status': 'error',
                          'error': type(exc).__name__ + ': ' + str(exc)},
                         ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
