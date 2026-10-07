# -*- coding: utf-8 -*-
"""
live_replay.py - 回放夹具：把 messages.json 灌过摄取链路的产物形状，生成独立夹具目录

背景
----
实时链路（live_daemon / live_ingest / live_serve）在聊天房间安静下来时无法
测试也无法演示 —— 一旦几天没人说话，状态机、进度、增量、守护进程存活判断
全部失去数据来源。本夹具用脚本化时钟把历史消息分批写进一份独立目录，模拟
消息真的在「实时到来」，让整条链路（尤其是 live_serve 的仪表盘）有可控的
演示与测试数据。

安全边界（硬要求）
------------------
1. 默认写 data/live_replay/，绝不写 data/live/。真实数据不能被夹具覆盖。
2. 只新建/修改 live_replay.py 这一个文件。live_serve/live_ui/live_ingest/
   live_daemon/fts_fallback 一个都不改，只 import 它们、调用它们的公开函数。
3. 产出的文件名与结构必须和 data/live/ 完全一致，这样
   `python scripts/live_serve.py --live data/live_replay --port 8779`
   能直接服务起来。

产物
----
  data/live_replay/live_messages.jsonl     只追加的尾巴（批量模拟增量到达）
  data/live_replay/live_view.json          仪表盘快照
  data/live_replay/daemon_status.json      心跳（last_tick_at 年龄是存活的判据）
  data/live_replay/ingest_state.json       摄取状态（含 identity / cursor）
  data/live_replay/inbox.json              标注收件箱（--with-annotations）
  data/live_replay/runs/<request_id>.progress.json
  data/live_replay/annotations/<request_id>.json

用 --stale 把 last_tick_at 写得足够旧，就可以演示「守护进程已死」横插。
用法：
  python scripts/live_replay.py --bundle data --out data/live_replay \
      --since 2026-09-01 --steps 6 --with-annotations
  python scripts/live_replay.py --bundle data --out data/live_replay \
      --since 2026-09-01 --with-annotations --stale
  python scripts/live_replay.py --bundle data --out data/live_replay \
      --steps 12 --loop --interval 5
"""

import argparse
import hashlib
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
import live_ingest  # noqa: E402

TAIL_NAME = live_ingest.TAIL_NAME      # live_messages.jsonl
VIEW_NAME = live_ingest.VIEW_NAME      # live_view.json
STATE_NAME = live_ingest.STATE_NAME    # ingest_state.json
STATUS_NAME = 'daemon_status.json'

ANNOTATION_REQUEST_ID = 'r-20261007T145500-a3f9'
ANNOTATION_TOTAL = 20           # 收件箱待标注总数
ANNOTATION_DONE = 9             # 已完成
ANNOTATION_INCOMPLETE = 2       # 不完整
# 其余 20-9-2=9 条 pending


def _ts(s):
    """YYYY-MM-DD 转 epoch 秒。"""
    return int(datetime.strptime(s, '%Y-%m-%d').timestamp())


def load_bundle(bundle_dir, since):
    payload = live_ingest.read_json(os.path.join(bundle_dir, 'messages.json'))
    if payload is None:
        raise RuntimeError('读不到 messages.json：' + os.path.join(bundle_dir, 'messages.json'))
    msgs = payload.get('messages') or []
    if since is not None:
        msgs = [m for m in msgs if (m.get('timestamp') or 0) >= since]
    return payload, msgs


def build_view(contact, known_n, tail, cursor, added_this_tick, generated_at_gap=0.0):
    """组装 live_view.json，形状对齐 live_ingest.build_view 的产物。

    回放没有真实 FTS/解密，所以 decrypt / watchdog 字段给确定性占位值。
    """
    last = tail[-1] if tail else None
    return {
        'schema_version': '1.0.0',
        'generated_at': time.time() + generated_at_gap,
        'generated_at_human': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'contact': contact,
        'counts': {
            'bundle_messages': known_n,
            'live_tail': len(tail),
            'total': known_n + len(tail),
            'added_this_tick': added_this_tick,
        },
        'cursor': cursor,
        'health': 'ok',
        'identity': {'session_id': 325, 'anchor_hits': 817},
        'decrypt': {
            'mode': 'replay',
            'wal_frames': 0,
            'db_mtime': None,
            'wal_mtime': None,
            'stale_risk': False,
            'wal_error': None,
        },
        'watchdog': {
            'tables_found': ['message_fts_v4_0', 'message_fts_v4_1',
                             'message_fts_v4_2', 'message_fts_v4_3'],
            'table_rows': {'message_fts_v4_2': 817, 'message_fts_v4_3': 631},
            'consecutive_empty': 0,
        },
        'last_message': {
            'msg_key': last.get('msg_key'),
            'sender': last.get('sender'),
            'timestamp': last.get('timestamp'),
            'human': datetime.fromtimestamp(last['timestamp'])
                     .strftime('%Y-%m-%d %H:%M:%S')
                     if last and last.get('timestamp') else None,
            'text_missing': bool(last.get('text_missing')) if last else None,
        } if last else None,
        'blind_spots': {
            'text_missing_in_tail': sum(1 for m in tail if m.get('text_missing')),
            'synthesized_last_tick': False,
            'note': '回放夹具：数据来自 messages.json 历史，非真实 FTS',
        },
        'sources': {},
        'tail': tail[-200:],
    }


def write_replay_status(live_dir, ticks, interval, phase, stale, started_at,
                        last_ingest, extra=None):
    """写 daemon_status.json，形状对齐 live_daemon.Daemon.status()。

    stale=True 时把 last_tick_at 写到 interval * 20 秒之前，
    让 live_serve 的 DEAD_MULTIPLIER(=3) 判定守护进程已死。
    """
    now = time.time()
    last_tick_at = now - (interval * 20 if stale else 0.0)
    payload = {
        'schema_version': '1.0.0',
        'pid': os.getpid(),
        'started_at': started_at,
        'started_at_human': datetime.fromtimestamp(started_at)
                                 .strftime('%Y-%m-%d %H:%M:%S'),
        'last_tick_at': last_tick_at,
        'last_tick_human': datetime.fromtimestamp(last_tick_at)
                                .strftime('%Y-%m-%d %H:%M:%S'),
        'ticks': ticks,
        'interval': interval,
        'phase': phase,
        'bundle': None,
        'live': os.path.abspath(live_dir),
        'last_probe': {
            'changed': bool(last_ingest and last_ingest.get('added', 0) > 0),
            'reason': '回放 tick' if (last_ingest and last_ingest.get('added', 0) > 0)
                      else '回放空闲',
            'last_message_human': None,
            'age_seconds': None,
            'decrypt_mode': 'replay',
            'decrypt_seconds': 0.0,
        },
        'last_ingest': last_ingest,
        'errors': [],
        'health': 'ok' if not stale else 'stopped',
    }
    if extra:
        payload.update(extra)
    live_ingest.write_json(os.path.join(live_dir, STATUS_NAME), payload)
    return payload


def write_ingest_state(live_dir, contact, cursor, tail_count, ticks):
    live_ingest.write_json(os.path.join(live_dir, STATE_NAME), {
        'version': live_ingest.STATE_VERSION,
        'identity': {
            'session_id': 325, 'me_sender_id': 2, 'them_sender_id': 325,
            'hits': 817,
            'tables': ['message_fts_v4_0', 'message_fts_v4_1',
                       'message_fts_v4_2', 'message_fts_v4_3'],
            'resolved_at': time.time(),
        },
        'consecutive_empty': 0,
        'contact': contact,
        'tables_found': ['message_fts_v4_0', 'message_fts_v4_1',
                         'message_fts_v4_2', 'message_fts_v4_3'],
        'table_rows': {'message_fts_v4_2': 817, 'message_fts_v4_3': 631},
        'cursor': cursor,
        'last_run': time.time(),
        'last_run_human': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'health': 'ok',
        'tail_count': tail_count,
    })


# ------------------------------------------------------------------ 标注夹具

def _norm_record(m):
    """把 messages.json 记录转成 live 尾巴风格（含 msg_key / ingested_at）。"""
    r = dict(m)
    r.pop('ingested_at', None)
    r.setdefault('source', 'message_fts')
    if not r.get('content'):
        r['text_missing'] = True
    r['msg_key'] = F.msg_key_of(r)
    return r


def _emotion_for(i):
    """确定性但多样化的 emotion 标签，供部分标注渲染。"""
    labels = ['平静', '开心', '关心', '期待', '敷衍', '好奇']
    return labels[i % len(labels)]


def build_annotations(messages, out_dir):
    """生成部分完成的标注夹具：inbox.json + runs/*.progress.json + annotations/*.json。

    状态机要处理的核心是「中间态」：20 条待标注里 9 条 done、2 条 incomplete、
    9 条 pending。progress.json 的 per_message 只写前 11 条（9 done + 2 incomplete），
    其余不出现即隐含 pending。
    """
    rid = ANNOTATION_REQUEST_ID
    sample = messages[-ANNOTATION_TOTAL:] if len(messages) >= ANNOTATION_TOTAL else messages
    # 不足 20 条时用最后一条重复填充，保证 total 字段与 record 数一致
    records = list(sample)
    while len(records) < ANNOTATION_TOTAL and records:
        records.append(records[-1])

    from_ts = records[0].get('timestamp') if records else 0
    to_ts = records[-1].get('timestamp') if records else 0
    from_key = F.msg_key_of(records[0]) if records else ''
    to_key = F.msg_key_of(records[-1]) if records else ''
    # 回放夹具用占位联系人，不写真实昵称/wxid
    contact = {'display': '对方', 'username': 'wxid_xxxxxxxxxxxxxxxx'}

    # ---- inbox.json
    inbox_msgs = [{
        'msg_key': F.msg_key_of(m),
        'local_id': m.get('local_id'),
        'sender': m.get('sender'),
        'timestamp': m.get('timestamp'),
        'type': m.get('type'),
        'content': (m.get('content') or '')[:500],
        'quote': None,
        'text_missing': bool(m.get('text_missing')),
        'source': 'message_fts',
    } for m in records]

    inbox = {
        'schema_version': '1.0.0',
        'request_id': rid,
        'created_at': float(to_ts) + 1840.0,
        'contact': contact,
        'cursor': {
            'from_ts': from_ts, 'to_ts': to_ts,
            'from_msg_key': from_key, 'to_msg_key': to_key,
        },
        'counts': {'new': len(records), 'pending_total': ANNOTATION_TOTAL,
                   'text_missing': sum(1 for m in records if m.get('text_missing'))},
        'messages': inbox_msgs,
        'context': {
            'reply_pending': True, 'pending_duration': 1800.0, 'hour': 21,
            'data_age_seconds': 300, 'advice': {}, 'stats_summary': {},
        },
    }
    live_ingest.write_json(os.path.join(out_dir, 'inbox.json'), inbox)

    # ---- 中间态：9 done + 2 incomplete，其余 pending
    per_message = []
    progress_map = {}
    for i, m in enumerate(records):
        key = F.msg_key_of(m)
        if i < ANNOTATION_DONE:
            state = 'done'
        elif i < ANNOTATION_DONE + ANNOTATION_INCOMPLETE:
            state = 'incomplete'
        else:
            state = 'pending'
            continue
        progress_map[key] = state
        per_message.append({
            'msg_key': key,
            'state': state,
            'emotion': {'label': _emotion_for(i), 'valence': 0.1, 'arousal': 0.3,
                        'confidence': 0.6},
            'intent': {'label': '回答问题', 'confidence': 0.7},
            'probs': {'平淡': 0.4, '敷衍': 0.3, '关心': 0.3},
            'reason': '一句话理由：回放夹具合成的确定性标注',
            'evidence': [key],
        })

    annotation = {
        'schema_version': '1.0.0',
        'request_id': rid,
        'produced_at': float(to_ts) + 2040.0,
        'model': 'claude-code-session',
        'status': 'partial',
        'reason': None,
        'per_message': per_message,
        'key_moments': [{
            'at_msg_key': to_key, 'timestamp': to_ts, 'kind': 'invitation',
            'title': '回放演示关键时刻', 'why': '夹具合成，用于渲染 key_moments',
            'evidence': [to_key],
        }],
        'threshold_notes': [{'signal': 'cold_response', 'read': '读法', 'confidence': 0.5}],
        'blind_spots': {'text_missing': 0, 'unrecoverable_media': 3},
    }
    live_ingest.write_json(
        os.path.join(out_dir, 'annotations', rid + '.json'), annotation)

    progress = {
        'request_id': rid,
        'state': 'working',
        'total': ANNOTATION_TOTAL,
        'done': ANNOTATION_DONE,
        'started_at': float(to_ts) + 1990.0,
        'updated_at': float(to_ts) + 2040.0,
        'per_message': progress_map,
    }
    live_ingest.write_json(
        os.path.join(out_dir, 'runs', rid + '.progress.json'), progress)

    return {'total': ANNOTATION_TOTAL, 'done': ANNOTATION_DONE,
            'incomplete': ANNOTATION_INCOMPLETE,
            'request_id': rid}


# ------------------------------------------------------------------ 主流程

def replay(bundle_dir, out_dir, since=None, steps=6, speed=1.0, interval=5.0,
           stale=False, stop_daemon_at=None, with_annotations=False, loop=False,
           verbose=False):
    bundle_dir = os.path.abspath(bundle_dir)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    payload, msgs = load_bundle(bundle_dir, since)
    contact = {'display': payload.get('contact_display'),
               'username': payload.get('contact_username')}
    known_n = len(payload.get('messages') or [])

    if not msgs:
        raise RuntimeError('--since 之后没有消息可回放')

    # ---- 幂等：把尾巴清空，避免上一次运行残留导致重复追加。
    # 真实 live_ingest 只追加（它是唯一写者，靠 msg_key 去重）；但回放是一次性
    # 生成（或 --loop 反复生成）完整尾巴，必须从空文件开始，否则 re-run 会叠加。
    tail_path = os.path.join(out_dir, TAIL_NAME)
    if os.path.exists(tail_path):
        os.remove(tail_path)

    # ---- 分批：把 msgs 分进 steps 个 batch，按时间顺序
    batch_size = max(1, (len(msgs) + steps - 1) // steps)
    batches = [msgs[i:i + batch_size] for i in range(0, len(msgs), batch_size)]
    if len(batches) > steps:
        batches = batches[:steps]
    elif len(batches) < steps:
        # steps 比实际 batch 数多时，后面补空 batch（模拟空 tick）
        batches = batches + [[] for _ in range(steps - len(batches))]

    started_at = time.time()
    tail = []
    cursor = msgs[0].get('timestamp') - 1 if msgs else 0
    per_step_sleep = (float(interval) / max(1.0, float(speed))) if not loop else float(interval)

    def emit(tick_idx, added, phase):
        last_ingest = {
            'added': added, 'scanned': added, 'cursor_human': None,
            'health': 'ok', 'synthesized': False, 'elapsed': 0.0,
        }
        view = build_view(contact, known_n, tail, cursor,
                          added_this_tick=added)
        live_ingest.write_json(os.path.join(out_dir, VIEW_NAME), view)
        write_ingest_state(out_dir, contact, cursor or 0, len(tail), tick_idx)
        stale_now = stale
        if stop_daemon_at is not None and tick_idx >= stop_daemon_at:
            stale_now = True
        write_replay_status(out_dir, tick_idx, interval, phase, stale_now,
                            started_at, last_ingest)
        return view

    # 首 tick：建立基线（空尾巴）
    emit(0, 0, 'idle')

    seen = set()  # msg_key 去重，保证 --loop 反复重放同一批也不重
    ticks = 0
    flushed = 0  # 已落盘的条数（loop 模式下增量 flush，避免中断丢尾巴）
    while True:
        for batch in batches:
            batch_added = []
            if batch:
                for m in batch:
                    r = _norm_record(m)
                    if r['msg_key'] in seen:
                        continue
                    seen.add(r['msg_key'])
                    tail.append(r)
                    batch_added.append(r)
                cursor = max(cursor, max(m.get('timestamp') or 0 for m in batch))
            added = len(batch_added)
            ticks += 1
            phase = 'ok' if added else 'idle'
            # loop 模式下增量落盘：每个新批次立刻写进 jsonl，
            # 这样长跑中断（Ctrl-C）时已到达的消息不会丢，且随时可被 serve 读。
            if loop and batch_added:
                live_ingest.append_tail(tail_path, batch_added)
            if verbose:
                print('[replay] tick=%d added=%d tail=%d phase=%s'
                      % (ticks, added, len(tail), phase), file=sys.stderr)
            emit(ticks, added, phase)
            if loop or ticks < len(batches):
                time.sleep(per_step_sleep)
        if not loop:
            break
    flushed = len(tail)

    result = {
        'status': 'ok',
        'out_dir': out_dir,
        'replayed': len(msgs),
        'steps': len(batches),
        'tail_count': len(tail),
        'cursor_human': datetime.fromtimestamp(cursor).strftime('%Y-%m-%d %H:%M:%S')
                        if cursor else None,
        'stale': stale,
        'annotations': None,
    }
    if with_annotations:
        result['annotations'] = build_annotations(msgs, out_dir)

    # 非 loop（一次性）模式：最后统一落盘；loop 模式已在 emit 间增量落盘，
    # 这里不再重复写（写两次会因 append_tail 叠加重复）。
    if not loop:
        live_ingest.append_tail(tail_path, tail)
    return result


# ------------------------------------------------------------------ 自检 & 保护

def _dir_fingerprint(dirpath):
    """对目录下所有文件算 sha256，返回 {相对路径: hexdigest}。

    用于断言 data/live/（真实数据）在回放前后没有被碰过。
    """
    out = {}
    for root, _dirs, files in os.walk(dirpath):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, dirpath)
            h = hashlib.sha256()
            try:
                with open(p, 'rb') as f:
                    for chunk in iter(lambda: f.read(65536), b''):
                        h.update(chunk)
                out[rel] = h.hexdigest()
            except OSError:
                out[rel] = '<unreadable>'
    return out


def self_check(live_dir, replay_fn):
    """跑一次回放，断言 data/live/ 前后指纹完全一致。

    replay_fn 是零参可调对象：调用它生成夹具。返回 (ok, before, after)。
    """
    before = _dir_fingerprint(live_dir)
    replay_fn()
    after = _dir_fingerprint(live_dir)
    changed = [k for k in before if before.get(k) != after.get(k)]
    changed += [k for k in after if k not in before]
    return (not changed), before, after, changed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='回放夹具：把历史消息分批灌成 live 产物，模拟实时到达')
    parser.add_argument('--bundle', default=os.path.join(REPO_ROOT, 'data'))
    parser.add_argument('--out', default=os.path.join(REPO_ROOT, 'data', 'live_replay'))
    parser.add_argument('--speed', type=float, default=1.0, help='回放速度倍率')
    parser.add_argument('--steps', type=int, default=6, help='分几个 tick 放完')
    parser.add_argument('--interval', type=float, default=5.0, help='--loop 时每个 tick 的间隔秒')
    parser.add_argument('--since', help='只回放此日期之后的消息，YYYY-MM-DD')
    parser.add_argument('--stale', action='store_true',
                        help='把 last_tick_at 写旧，演示「守护进程已死」横插')
    parser.add_argument('--stop-daemon-at', type=int, default=None,
                        help='第 N 个 tick 之后把心跳写旧（等价于此刻起守护进程停跳）')
    parser.add_argument('--with-annotations', action='store_true',
                        help='生成部分完成的标注夹具（inbox + runs + annotations）')
    parser.add_argument('--loop', action='store_true',
                        help='每个 batch 之间按 --interval 睡一下，长跑观察用')
    parser.add_argument('--self-check', action='store_true',
                        help='跑一次回放并断言 data/live/ 的 sha256 指纹前后一致')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    since = _ts(args.since) if args.since else None

    if args.self_check:
        # 真实 live 目录（默认 data/live）绝不能被动
        real_live = os.path.join(REPO_ROOT, 'data', 'live')
        def _do_replay():
            replay(args.bundle, args.out, since=since, steps=args.steps,
                   speed=args.speed, interval=args.interval, stale=args.stale,
                   stop_daemon_at=args.stop_daemon_at,
                   with_annotations=args.with_annotations, loop=False)
        ok, before, after, changed = self_check(real_live, _do_replay)
        n = len(before)
        result = {'self_check': 'ok' if ok else 'DIRTY',
                  'live_dir': real_live,
                  'files_hashed': n,
                  'changed': changed}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    result = replay(args.bundle, args.out, since=since, steps=args.steps,
                    speed=args.speed, interval=args.interval, stale=args.stale,
                    stop_daemon_at=args.stop_daemon_at,
                    with_annotations=args.with_annotations, loop=args.loop,
                    verbose=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
