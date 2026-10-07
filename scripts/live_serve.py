# -*- coding: utf-8 -*-
"""
live_serve.py - 实时仪表盘的只读服务

只用标准库。项目自身零依赖这条约束在这里同样成立 —— 不引入 flask/fastapi。

只读：本进程从不写任何 live 产物。唯一的写者是 live_daemon.py（数据）
和 Claude 会话（标注）。一个文件一个写者，所以这里不需要任何跨进程锁。

版本信息：
  GET /              仪表盘（零 CDN，离线可用）
  GET /api/snapshot  小份元数据，2 秒轮询一次（健康 / 计数 / 最新一条）
  GET /api/messages  全量消息列表，只在计数变化时取
  GET /api/health    给脚本看的紧凑健康状态

HTTP/1.0（不设 protocol_version）：响应体靠关连接界定，所以不用手搓 chunked
分帧。SSE 也照这个来 —— 但这个仪表盘把「2 秒轮询 snapshot」当权威，
不依赖 SSE 的可靠性。

「守护进程死了没」看 last_tick_at 的年龄，不看 phase —— 被 SIGKILL 的进程
来不及把 phase 改成 stopped，年龄是唯一可信的信号。
"""

import argparse
import glob
import http.server
import json
import os
import sys
import threading
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import fts_fallback as F  # noqa: E402
import live_ingest  # noqa: E402
import live_ui  # noqa: E402

# 超过这个倍数 × 轮询间隔没有心跳就认定守护进程已死
DEAD_MULTIPLIER = 3

ANNOTATIONS_DIR = 'annotations'
RUNS_DIR = 'runs'
INBOX_NAME = 'inbox.json'
# progress.updated_at 超过这么久没动就视为 stale（认领了但会话死了）
STALE_SECONDS = 600.0


def read_annotations(live_dir):
    """读 annotations/ 下所有标注文件，返回 [(request_id, body)] 按 produced_at 排序。

    容错：body 不是 dict（比如合法 JSON 但形状是 list）的跳过；坏 JSON 已被
    read_json 兜底成 None。文件接口读侧必须宽容——这些文件是 Claude 会话手写的。
    """
    out = []
    for path in glob.glob(os.path.join(live_dir, ANNOTATIONS_DIR, '*.json')):
        body = live_ingest.read_json(path)
        if not isinstance(body, dict):
            continue
        rid = body.get('request_id') or os.path.basename(path)[:-5]
        out.append((rid, body))
    out.sort(key=lambda t: t[1].get('produced_at') or 0)
    return out


def read_progress_all(live_dir, now=None):
    """读 runs/ 下所有 progress 文件，返回 {request_id: 状态摘要}。

    每个 request 的最终态用 progress 的 state，但认领了超过 STALE_SECONDS 没动
    的判 stale（会话死了）。这是「analyzing 是否可达」的关键：没有它，UI 永远
    只能显示 pending 或 done，区分不出「正在标」和「没人管」。
    """
    now = now or time.time()
    out = {}
    for path in glob.glob(os.path.join(live_dir, RUNS_DIR, '*.progress.json')):
        body = live_ingest.read_json(path)
        if not isinstance(body, dict):
            continue
        rid = body.get('request_id') or os.path.basename(path).replace('.progress.json', '')
        state = body.get('state') or 'claimed'
        updated = body.get('updated_at')
        if state in ('claimed', 'working') and updated and (now - float(updated)) > STALE_SECONDS:
            state = 'stale'
        out[rid] = {
            'state': state,
            'total': body.get('total', 0),
            'done': body.get('done', 0),
            'updated_at': updated,
            'per_message': body.get('per_message') or {},
        }
    return out


def annotation_summary(live_dir, now=None):
    """组装「标注」摘要：总数 / 已完成 / 待标注 / 正在标注 / 上次标注时间 / 映射。

    返回 (summary, by_key, moments, progress)：
    - by_key: msg_key -> annotation（成品，最终 state 的权威）
    - progress: {request_id -> {state, total, done, updated_at, per_message}}
      per_message 是 msg_key -> progress 态（pending/analyzing/done/...），
      UI 拿它和 by_key 合并：同一 msg_key 若都在, annotation 赢(成品优先)。
    """
    now = now or time.time()
    anns = read_annotations(live_dir)
    inbox = live_ingest.read_json(os.path.join(live_dir, INBOX_NAME)) or {}
    progress = read_progress_all(live_dir, now=now)

    total = 0
    done = 0
    last_annotated_at = None
    by_key = {}
    moments = []
    for _rid, body in anns:
        produced = body.get('produced_at')
        if produced and (last_annotated_at is None or produced > last_annotated_at):
            last_annotated_at = produced
        if body.get('status') == 'ok':
            done += 1
        for pm in body.get('per_message') or []:
            if not isinstance(pm, dict):
                continue  # 坏标注条目：跳过，别让整个快照崩掉
            total += 1
            k = pm.get('msg_key')
            if k:
                by_key[k] = pm
        for km in body.get('key_moments') or []:
            if isinstance(km, dict):
                moments.append(km)

    # 正在标注：所有 state 是 claimed/working 的 request 里、还没出成品、且 progress
    # 态是「未完成」的消息数。已经 done/skipped/incomplete 的不算「正在」。
    analyzing = 0
    for _rid, pr in progress.items():
        if pr['state'] in ('claimed', 'working'):
            for k, s in (pr['per_message'] or {}).items():
                if k in by_key:
                    continue  # 已经出成品的这条不算「正在」
                if s in ('done', 'skipped', 'incomplete'):
                    continue
                analyzing += 1

    pending_total = (inbox.get('counts') or {}).get('pending_total', 0)
    # 本批队列里到底是哪几条。前端靠它把「这一批正等着标」和「历史从没
    # 标注过」分成两种视觉 —— 不分开的话 1541 条历史全挂红色「待标注」，
    # 那正是这套设计最该避免的失败模式：看起来像坏了。
    queued_keys = [m.get('msg_key') for m in (inbox.get('messages') or [])
                   if isinstance(m, dict) and m.get('msg_key')]
    summary = {
        'total_annotated': total,
        'requests': len(anns),
        'requests_ok': done,
        'pending': pending_total,
        'queued_keys': queued_keys,
        'analyzing': analyzing,
        'stale_runs': sum(1 for pr in progress.values() if pr['state'] == 'stale'),
        'last_annotated_at': last_annotated_at,
        'last_annotated_human': (
            datetime.fromtimestamp(last_annotated_at).strftime('%Y-%m-%d %H:%M:%S')
            if last_annotated_at else None),
    }
    return summary, by_key, moments, progress


class Snapshot:
    """内存快照。后台线程建新的 dict，加锁整体替换 —— 持锁时不碰 IO。"""

    def __init__(self, bundle, live):
        self.bundle = os.path.abspath(bundle)
        self.live = os.path.abspath(live)
        self.lock = threading.Lock()
        self._snapshot = {}
        self._messages = []
        self._messages_sig = None  # (count, last_key) —— 计数没变就不重建
        self._bundle_cache = None
        self._annotations = {}   # msg_key -> 标注
        self._moments = []       # key_moments 时间轴
        self._progress = {}      # request_id -> progress 状态摘要

    # ------------------------------------------------------------ 读 bundle
    def bundle_messages(self):
        path = os.path.join(self.bundle, 'messages.json')
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return []
        cache = self._bundle_cache
        if cache and cache[0] == mtime:
            return cache[1]
        payload = live_ingest.read_json(path) or {}
        msgs = payload.get('messages') or []
        self._bundle_cache = (mtime, msgs)
        return msgs

    # ------------------------------------------------------------ 组装
    def build(self):
        view = live_ingest.read_json(os.path.join(self.live, live_ingest.VIEW_NAME)) or {}
        status = live_ingest.read_json(os.path.join(self.live, live_daemon_status())) or {}
        bundle = self.bundle_messages()
        tail = live_ingest.read_tail(os.path.join(self.live, live_ingest.TAIL_NAME))

        now = time.time()
        last_tick = status.get('last_tick_at')
        interval = status.get('interval') or 5.0
        tick_age = (now - last_tick) if last_tick else None
        daemon_alive = tick_age is not None and tick_age < interval * DEAD_MULTIPLIER

        last = tail[-1] if tail else (bundle[-1] if bundle else None)
        last_ts = (last or {}).get('timestamp')

        health = []
        if not daemon_alive:
            health.append({
                'level': 'down',
                'label': '守护进程没有心跳',
                'detail': ('最后一次 tick %s 前（间隔 %.0fs）'
                           % (human_age(tick_age), interval)) if tick_age is not None
                          else '还没有任何心跳，守护进程可能没启动过',
            })
        ing = status.get('last_ingest') or {}
        if ing.get('health') == 'ingest_stalled':
            health.append({
                'level': 'warn',
                'label': 'ingest 卡住',
                'detail': '会话有变化但连续取不到内容 —— 可能是密钥轮换或 FTS 表名失效',
            })
        if (view.get('watchdog') or {}).get('consecutive_empty', 0) >= 20 and not tail:
            health.append({'level': 'warn', 'label': '长时间没有新消息',
                           'detail': '只是安静，也可能 ingest 静默失效'})
        if (view.get('decrypt') or {}).get('stale_risk'):
            health.append({'level': 'warn', 'label': 'WAL 补丁失败',
                           'detail': str((view.get('decrypt') or {}).get('wal_error'))})
        if (status.get('errors') or []):
            last_err = status['errors'][-1]
            health.append({'level': 'warn', 'label': '最近有错误',
                           'detail': '%s: %s' % (last_err.get('stage'), last_err.get('error'))})

        blind = view.get('blind_spots') or {}
        if blind.get('text_missing_in_tail'):
            health.append({
                'level': 'info',
                'label': '%d 条非文本消息内容不可得' % blind['text_missing_in_tail'],
                'detail': blind.get('note', ''),
            })

        snapshot = {
            'generated_at': now,
            'contact': view.get('contact') or {},
            'counts': (view.get('counts') or {}),
            'health': health,
            'daemon': {
                'alive': daemon_alive,
                'tick_age_seconds': round(tick_age, 1) if tick_age is not None else None,
                'last_tick_human': status.get('last_tick_human'),
                'ticks': status.get('ticks'),
                'interval': interval,
                'phase': status.get('phase'),
                'pid': status.get('pid'),
            },
            'last_message': {
                'human': datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M:%S')
                         if last_ts else None,
                'sender': (last or {}).get('sender'),
                'age_seconds': round(now - last_ts, 1) if last_ts else None,
                'text_missing': bool((last or {}).get('text_missing')),
                'origin': 'live' if tail else 'bundle',
            },
            'ingest': {
                'cursor_human': (status.get('last_ingest') or {}).get('cursor_human')
                                or view.get('cursor'),
                'health': (status.get('last_ingest') or {}).get('health') or view.get('health'),
                'decrypt': view.get('decrypt') or {},
                'watchdog': view.get('watchdog') or {},
                'identity': view.get('identity') or {},
            },
            'blind_spots': blind,
        }
        ann_summary, ann_by_key, ann_moments, ann_progress = annotation_summary(self.live, now=now)
        snapshot['annotations'] = ann_summary
        return snapshot, bundle, tail, ann_by_key, ann_moments, ann_progress

    def norm(self, m, idx, origin):
        return {
            'i': idx,
            'key': F.msg_key_of(m),
            'sender': 'me' if m.get('sender') == 'me' else ('them' if m.get('sender') == 'them' else 'unknown'),
            'timestamp': m.get('timestamp'),
            'content': (m.get('content') or '')[:4000],
            'type': m.get('type'),
            'has_quote': bool(m.get('quote')),
            'quote_source': (m.get('quote') or {}).get('source'),
            'text_missing': bool(m.get('text_missing')),
            'origin': origin,
            'note': m.get('note'),
        }

    def refresh(self):
        try:
            snapshot, bundle, tail, ann_by_key, ann_moments, ann_progress = self.build()
        except Exception as exc:  # noqa: BLE001 - 读失败不该让服务崩掉
            snapshot = {'generated_at': time.time(), 'error': '%s: %s' % (type(exc).__name__, exc),
                        'health': [{'level': 'down', 'label': '读取 live 产物失败',
                                    'detail': str(exc)}],
                        'contact': {}, 'counts': {}, 'daemon': {}, 'last_message': {},
                        'ingest': {}, 'blind_spots': {}, 'annotations': {}}
            bundle, tail = [], []
            ann_by_key, ann_moments, ann_progress = {}, [], {}
        with self.lock:
            self._snapshot = snapshot
            self._annotations = ann_by_key
            self._moments = ann_moments
            self._progress = ann_progress
            sig = (len(bundle) + len(tail),
                   (tail[-1].get('msg_key') if tail else None),
                   (bundle[-1].get('local_id') if bundle else None))
            if sig != self._messages_sig:
                msgs = [self.norm(m, i, 'bundle') for i, m in enumerate(bundle)]
                base = len(bundle)
                msgs += [self.norm(m, base + i, 'live') for i, m in enumerate(tail)]
                self._messages = msgs
                self._messages_sig = sig

    def get_snapshot(self):
        with self.lock:
            return dict(self._snapshot)

    def get_messages(self):
        with self.lock:
            return list(self._messages), self._messages_sig

    def get_annotations(self):
        with self.lock:
            return dict(self._annotations), list(self._moments), dict(self._progress)


def live_daemon_status():
    return 'daemon_status.json'


def human_age(seconds):
    if seconds is None:
        return '未知'
    seconds = int(seconds)
    if seconds < 60:
        return '%d 秒' % seconds
    if seconds < 3600:
        return '%d 分钟' % (seconds // 60)
    if seconds < 86400:
        return '%d 小时' % (seconds // 3600)
    return '%d 天' % (seconds // 86400)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'she-love-me-live/0.1'
    snapshot = None  # 由 serve() 注入

    def log_message(self, *args):
        pass  # 别把每个轮询都打到终端

    def _send(self, body, ctype='application/json; charset=utf-8', code=200):
        payload = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self):
        path = self.path.split('?')[0]
        snap = self.snapshot
        if path in ('/', '/index.html'):
            self._send(live_ui.HTML, 'text/html; charset=utf-8')
        elif path == '/api/snapshot':
            self._send(json.dumps(snap.get_snapshot(), ensure_ascii=False))
        elif path == '/api/messages':
            msgs, sig = snap.get_messages()
            self._send(json.dumps({'messages': msgs, 'sig': list(sig or [])},
                                  ensure_ascii=False))
        elif path == '/api/annotations':
            by_key, moments, progress = snap.get_annotations()
            self._send(json.dumps({'annotations': by_key, 'key_moments': moments,
                                   'progress': progress},
                                  ensure_ascii=False))
        elif path == '/api/health':
            s = snap.get_snapshot()
            self._send(json.dumps({
                'daemon_alive': (s.get('daemon') or {}).get('alive'),
                'health': s.get('health'),
                'last_message_human': (s.get('last_message') or {}).get('human'),
                'counts': s.get('counts'),
            }, ensure_ascii=False))
        else:
            self._send(json.dumps({'error': 'not found'}, ensure_ascii=False), code=404)


def serve(bundle, live, host='127.0.0.1', port=8770, poll=1.0, quiet=False):
    snap = Snapshot(bundle, live)
    snap.refresh()

    stop = threading.Event()

    def poller():
        while not stop.is_set():
            stop.wait(poll)
            if stop.is_set():
                break
            snap.refresh()

    thread = threading.Thread(target=poller, daemon=True)
    thread.start()

    handler = type('BoundHandler', (Handler,), {'snapshot': snap})
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True
    if not quiet:
        print('[live_serve] http://%s:%d/  (bundle=%s live=%s)'
              % (host, port, os.path.abspath(bundle), os.path.abspath(live)), file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='实时仪表盘的只读服务')
    parser.add_argument('--bundle', default=os.path.join(REPO_ROOT, 'data'))
    parser.add_argument('--live', default=os.path.join(REPO_ROOT, 'data', 'live'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--poll', type=float, default=1.0)
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    return serve(args.bundle, args.live, args.host, args.port, args.poll, args.quiet)


if __name__ == '__main__':
    sys.exit(main())
