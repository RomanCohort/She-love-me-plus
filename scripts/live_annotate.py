# -*- coding: utf-8 -*-
"""
live_annotate.py - 语义标注层的 CLI（给 Claude 会话用）

守护进程永不调 LLM。语义层是 Claude Code 会话里的一件事：Claude 读
`data/live/inbox.json`（待标注清单），本模块负责把读/写规范好，Claude 不用手搓 JSON。

三个文件、三个写者（一个文件一个写者，无锁）：

    inbox.json                    守护进程（live_ingest.py）写，Claude 读
    annotations/<request_id>.json Claude 写标注结果
    runs/<request_id>.progress.json Claude 写进度（文件存在 + mtime = 认领）

契约全文见
`.agents/skills/she-love-me/references/live-annotation-schema.md`。

子命令：
  --index     列出 inbox + 所有已写标注 + 所有进度文件，看整体状态
  --claim     认领 inbox 里的 request：写 progress 文件，state=claimed
  --progress  更新 progress（state / done 计数 / 每条消息状态）
  --write     把标注 body 落到 annotations/<request_id>.json（原子写）
  --status    单条 request 的完整状态（inbox + annotation + progress 三份一起看）

用法示例（在 Claude 会话里）：
  python scripts/live_annotate.py --live data/live --index
  python scripts/live_annotate.py --live data/live --claim --request-id r-xxx
  python scripts/live_annotate.py --live data/live --progress --request-id r-xxx \
      --state working --per-message '{"<key>":"done"}' --done 9 --total 20
  python scripts/live_annotate.py --live data/live --write --request-id r-xxx \
      --file /path/to/annotation.json
  python scripts/live_annotate.py --live data/live --status --request-id r-xxx

只用标准库，零第三方依赖。读文件一律 encoding='utf-8'。
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

import live_ingest  # noqa: E402  (read_json / write_json / read_tail)

ANNOTATIONS_DIR = 'annotations'
RUNS_DIR = 'runs'
INBOX_NAME = 'inbox.json'
STALE_SECONDS = 600.0  # progress.updated_at 超过 10 分钟没动 → stale


# ---------------------------------------------------------------- 路径

def _dir(live_dir, sub):
    return os.path.join(live_dir, sub)


def annotations_dir(live_dir):
    return _dir(live_dir, ANNOTATIONS_DIR)


def runs_dir(live_dir):
    return _dir(live_dir, RUNS_DIR)


def inbox_path(live_dir):
    return os.path.join(live_dir, INBOX_NAME)


def annotation_path(live_dir, request_id):
    return os.path.join(annotations_dir(live_dir), request_id + '.json')


def progress_path(live_dir, request_id):
    return os.path.join(runs_dir(live_dir), request_id + '.progress.json')


# ---------------------------------------------------------------- 读取

def read_inbox(live_dir):
    return live_ingest.read_json(inbox_path(live_dir)) or {}


def read_annotations(live_dir):
    """读 annotations/ 下所有标注文件，返回 [request_id, body] 列表（按 produced_at 排序）。

    容错：body 不是 dict 的跳过。文件接口读侧宽容——坏标注不该让 --index 崩掉。
    """
    out = []
    for path in glob.glob(os.path.join(annotations_dir(live_dir), '*.json')):
        body = live_ingest.read_json(path)
        if not isinstance(body, dict):
            continue
        rid = body.get('request_id') or os.path.basename(path)[:-5]
        out.append((rid, body))
    out.sort(key=lambda t: t[1].get('produced_at') or 0)
    return out


def read_progress(live_dir, request_id):
    return live_ingest.read_json(progress_path(live_dir, request_id)) or {}


def list_runs(live_dir):
    out = []
    for path in glob.glob(os.path.join(runs_dir(live_dir), '*.progress.json')):
        body = live_ingest.read_json(path)
        if not isinstance(body, dict):
            continue
        rid = body.get('request_id') or os.path.basename(path).replace('.progress.json', '')
        out.append((rid, body))
    return out


def covered_msg_keys(live_dir):
    """annotations/ 下所有文件的 per_message[].msg_key 并集 —— 判定「已覆盖」的权威口径。"""
    keys = set()
    for _rid, body in read_annotations(live_dir):
        for pm in body.get('per_message') or []:
            if not isinstance(pm, dict):
                continue
            k = pm.get('msg_key')
            if k:
                keys.add(k)
    return keys


# ---------------------------------------------------------------- 写

def write_annotation(live_dir, request_id, body):
    """把 Claude 的标注 body 落到 annotations/<request_id>.json。原子写。"""
    body = dict(body)
    if not body.get('schema_version'):
        body['schema_version'] = '1.0.0'
    if not body.get('request_id'):
        body['request_id'] = request_id
    if not body.get('produced_at'):
        body['produced_at'] = time.time()
    live_ingest.write_json(annotation_path(live_dir, request_id), body)
    return annotation_path(live_dir, request_id)


def write_progress(live_dir, request_id, state, total=None, done=None,
                   per_message=None, started_at=None):
    """认领 / 更新进度。文件存在 + mtime 就是认领。"""
    now = time.time()
    prev = read_progress(live_dir, request_id)
    body = {
        'request_id': request_id,
        'state': state,
        'total': total if total is not None else prev.get('total', 0),
        'done': done if done is not None else prev.get('done', 0),
        'started_at': started_at if started_at is not None else prev.get('started_at', now),
        'updated_at': now,
        'per_message': per_message if per_message is not None else prev.get('per_message', {}),
    }
    live_ingest.write_json(progress_path(live_dir, request_id), body)
    return progress_path(live_dir, request_id)


# ---------------------------------------------------------------- 派生状态

def effective_state(progress=None):
    """progress 的四态，但陈旧会被覆盖成 stale。"""
    p = progress or {}
    if p.get('state') == 'stale':
        return 'stale'
    updated = p.get('updated_at')
    if updated and (time.time() - float(updated)) > STALE_SECONDS:
        return 'stale'
    return p.get('state') or 'unclaimed'


def summarize(live_dir):
    """给 --index 用的整体视图。"""
    inbox = read_inbox(live_dir)
    covered = covered_msg_keys(live_dir)
    pending_total = (inbox.get('counts') or {}).get('pending_total')
    if pending_total is None:
        # 兜底：inbox 没写 counts 时，从 messages 数组里数没被覆盖的
        pending_keys = [m.get('msg_key') for m in inbox.get('messages') or []
                        if m.get('msg_key') and m.get('msg_key') not in covered]
        pending_total = len(pending_keys)

    annotations = []
    for rid, body in read_annotations(live_dir):
        annotations.append({
            'request_id': rid,
            'status': body.get('status'),
            'produced_at': body.get('produced_at'),
            'produced_at_human': _human(body.get('produced_at')),
            'model': body.get('model'),
            'n_messages': len(body.get('per_message') or []),
        })

    runs = []
    for rid, body in list_runs(live_dir):
        runs.append({
            'request_id': rid,
            'state': effective_state(body),
            'total': body.get('total'),
            'done': body.get('done'),
            'updated_at_human': _human(body.get('updated_at')),
        })

    return {
        'live_dir': os.path.abspath(live_dir),
        'inbox': {
            'request_id': inbox.get('request_id'),
            'created_at_human': _human(inbox.get('created_at')),
            'counts': inbox.get('counts') or {},
            'n_pending': pending_total,
            'contact': inbox.get('contact') or {},
        },
        'annotations': annotations,
        'runs': runs,
    }


def _human(ts):
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OSError):
        return str(ts)


# ---------------------------------------------------------------- CLI

def _emit(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(obj, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description='语义标注层 CLI（给 Claude 会话用）')
    parser.add_argument('--live', default=os.path.join(REPO_ROOT, 'data', 'live'))
    parser.add_argument('--json', action='store_true', help='输出紧凑 JSON（默认已人性化换行）')
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--index', action='store_true', help='整体状态：inbox + 标注 + 进度')
    g.add_argument('--claim', action='store_true', help='认领 request（写 progress state=claimed）')
    g.add_argument('--progress', action='store_true', help='更新进度')
    g.add_argument('--write', action='store_true', help='写标注结果 annotations/<id>.json')
    g.add_argument('--status', action='store_true', help='单条 request 的完整状态')
    parser.add_argument('--request-id', help='目标 request_id')
    parser.add_argument('--state', help='progress state：claimed/working/done/stale')
    parser.add_argument('--done', type=int, help='已完成条数')
    parser.add_argument('--total', type=int, help='总条数')
    parser.add_argument('--per-message', help='JSON 字符串 {"<msg_key>":"done", ...}')
    parser.add_argument('--file', help='标注 body 的 JSON 文件路径（--write 用）')
    args = parser.parse_args(argv)

    live_dir = os.path.abspath(args.live)

    if args.index:
        return _cmd(lambda: summarize(live_dir), args.json)

    if args.claim or args.progress:
        if not args.request_id:
            _err('--claim/--progress 需要 --request-id')
            return 2
        return _cmd(lambda: {
            'path': write_progress(live_dir, args.request_id,
                                   args.state or 'claimed',
                                   total=args.total, done=args.done,
                                   per_message=_parse_per_message(args.per_message)),
        }, args.json)

    if args.write:
        if not args.request_id:
            _err('--write 需要 --request-id')
            return 2
        if not args.file:
            _err('--write 需要 --file（标注 body 的 JSON 文件）')
            return 2
        return _cmd(lambda: _do_write(live_dir, args.request_id, args.file), args.json)

    if args.status:
        if not args.request_id:
            _err('--status 需要 --request-id')
            return 2
        return _cmd(lambda: _do_status(live_dir, args.request_id), args.json)

    parser.print_help()
    return 0


def _parse_per_message(raw):
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _err('--per-message 不是合法 JSON：%s' % exc)
        raise SystemExit(2)
    if not isinstance(parsed, dict):
        _err('--per-message 必须是 JSON 对象')
        raise SystemExit(2)
    return parsed


def _do_write(live_dir, request_id, file_path):
    body = live_ingest.read_json(file_path) or {}
    path = write_annotation(live_dir, request_id, body)
    return {'path': path, 'status': body.get('status'),
            'n_messages': len(body.get('per_message') or [])}


def _do_status(live_dir, request_id):
    inbox = read_inbox(live_dir)
    annotation = live_ingest.read_json(annotation_path(live_dir, request_id)) or {}
    progress = read_progress(live_dir, request_id)
    return {
        'request_id': request_id,
        'inbox_match': inbox.get('request_id') == request_id,
        'inbox_pending_total': (inbox.get('counts') or {}).get('pending_total'),
        'annotation': {
            'exists': bool(annotation),
            'status': annotation.get('status'),
            'produced_at_human': _human(annotation.get('produced_at')),
            'model': annotation.get('model'),
            'n_messages': len(annotation.get('per_message') or []),
        },
        'progress': {
            'exists': bool(progress),
            'state': effective_state(progress) if progress else None,
            'total': progress.get('total'),
            'done': progress.get('done'),
            'updated_at_human': _human(progress.get('updated_at')),
        },
    }


def _cmd(fn, as_json):
    try:
        obj = fn()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI 边界
        _emit({'status': 'error', 'error': '%s: %s' % (type(exc).__name__, exc)}, as_json)
        return 2
    _emit(obj, as_json)
    return 0


def _err(msg):
    print(json.dumps({'status': 'error', 'error': msg}, ensure_ascii=False),
          file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
