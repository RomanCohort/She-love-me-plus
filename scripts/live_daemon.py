# -*- coding: utf-8 -*-
"""
live_daemon.py - 实时链路的常驻进程

一次 tick：探测（只看 session.db）→ 变了就摄取（FTS 增量）→ 写心跳。

它是**唯一**有权解密和写的进程。live_serve.py 只读，Claude 只写标注。
一个文件只有一个写者 —— 这就是全部的并发策略，不需要锁。

三条不做：
  1. 不扫密钥、不跑 Frida。scripts/tmp/RUNBOOK.md 写明这台机器是
     WeChat 4.1.13.65，取密钥需要停机断网的 Frida hook —— 那是有账号风险的
     管理员级操作，不能由一个后台仪表盘偷偷做。密钥没了就报红，不自己动手。
  2. 不调 LLM。语义层是 Claude Code 会话的事，见 live_annotate.py。
  3. 不写 messages.json。实时尾巴只进 data/live/live_messages.jsonl。

静默陈旧是这个系统最可能的失败方式 —— 「看起来还活着，其实停在旧数据」。
所以每个 tick 都写 daemon_status.json（含心跳），仪表盘据此判断守护进程是不是
已经死了；探测到变化但连续取不到内容时报 ingest_stalled，而不是健康的空闲态。

用法：
  python scripts/live_daemon.py --bundle data --live data/live
  python scripts/live_daemon.py --once          # 跑一次就退出（测试用）
  python scripts/live_daemon.py --interval 5    # 轮询间隔秒（默认 5）
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import live_ingest  # noqa: E402
import live_probe  # noqa: E402

DEFAULT_VENDOR = os.path.join(REPO_ROOT, 'vendor', 'wechat-decrypt')
STATUS_NAME = 'daemon_status.json'
# 30ms（vendor monitor_web.py 用的值）在这里没有意义，只会白白扩大
# 22MB 文件的撕裂窗口。2-5 秒足够 —— 用户不可能比这更快地对一条消息作出反应。
DEFAULT_INTERVAL = 5.0


def write_status(live_dir, payload):
    live_ingest.write_json(os.path.join(live_dir, STATUS_NAME), payload)


class Daemon:
    def __init__(self, bundle, live, vendor, interval, once=False):
        self.bundle = os.path.abspath(bundle)
        self.live = os.path.abspath(live)
        self.vendor = os.path.abspath(vendor)
        self.interval = max(0.5, float(interval))
        self.once = once
        self.stop = False
        self.started_at = time.time()
        self.ticks = 0
        self.errors = []
        self.last_probe = None
        self.last_ingest = None

    def request_stop(self, *_args):
        self.stop = True

    def status(self, phase, extra=None):
        payload = {
            'schema_version': '1.0.0',
            'pid': os.getpid(),
            'started_at': self.started_at,
            'started_at_human': datetime.fromtimestamp(self.started_at)
                                         .strftime('%Y-%m-%d %H:%M:%S'),
            'last_tick_at': time.time(),
            'last_tick_human': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'ticks': self.ticks,
            'interval': self.interval,
            'phase': phase,
            'bundle': self.bundle,
            'live': self.live,
            'last_probe': self.last_probe,
            'last_ingest': self.last_ingest,
            'errors': self.errors[-5:],
            'health': 'stopping' if self.stop else 'ok',
        }
        if extra:
            payload.update(extra)
        write_status(self.live, payload)
        return payload

    def tick(self):
        self.ticks += 1
        # ---- 探测：只解 session.db（475KB），最便宜的一层
        try:
            probe = live_probe.probe(self.bundle, self.vendor,
                                     dst_dir=os.path.join(self.live, 'decrypted'))
        except Exception as exc:  # noqa: BLE001 - 单次失败不该杀掉守护进程
            self.errors.append({'at': time.time(), 'stage': 'probe',
                                'error': '%s: %s' % (type(exc).__name__, exc)})
            self.status('probe_failed')
            return
        self.last_probe = {
            'changed': probe.get('changed'),
            'reason': probe.get('reason'),
            'last_message_human': probe.get('last_message_human'),
            'age_seconds': probe.get('age_seconds'),
            'decrypt_mode': probe.get('decrypt_mode'),
            'decrypt_seconds': probe.get('decrypt_seconds'),
        }

        # ---- 摄取：会话变了才做；第一次总做一次建立基线
        first = self.last_ingest is None
        should_ingest = first or probe.get('changed')
        if not should_ingest:
            self.status('idle')
            return

        self.status('ingesting')
        try:
            # session_row 交给 ingest，让它在「会话前进了但 FTS 取不到」时
            # 合成一条事件记录 —— 非文本消息在 FTS 里根本不存在
            result = live_ingest.ingest(
                self.bundle, self.live, self.vendor, session_row=probe)
        except Exception as exc:  # noqa: BLE001
            self.errors.append({'at': time.time(), 'stage': 'ingest',
                                'error': '%s: %s' % (type(exc).__name__, exc)})
            self.status('ingest_failed')
            return
        self.last_ingest = {
            'added': result.get('added'),
            'scanned': result.get('scanned'),
            'cursor_human': result.get('cursor_human'),
            'health': result.get('health'),
            'synthesized': result.get('synthesized'),
            'elapsed': result.get('elapsed'),
        }
        self.status('ok')

    def run(self):
        signal.signal(signal.SIGINT, self.request_stop)
        try:
            signal.signal(signal.SIGTERM, self.request_stop)
        except (AttributeError, ValueError):
            pass

        os.makedirs(self.live, exist_ok=True)
        print('[live_daemon] 启动 pid=%d bundle=%s live=%s interval=%.1fs'
              % (os.getpid(), self.bundle, self.live, self.interval), file=sys.stderr)
        while not self.stop:
            started = time.time()
            self.tick()
            if self.once:
                break
            # 睡到下一个 tick；期间可被信号打断
            remaining = self.interval - (time.time() - started)
            while remaining > 0 and not self.stop:
                time.sleep(min(0.25, remaining))
                remaining -= 0.25

        final = self.status('stopped', {'health': 'stopped'})
        print('[live_daemon] 停止，共 %d 个 tick，%d 个错误'
              % (final['ticks'], len(self.errors)), file=sys.stderr)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='实时链路守护进程（探测 + 摄取 + 心跳）')
    parser.add_argument('--bundle', default=os.path.join(REPO_ROOT, 'data'))
    parser.add_argument('--live', default=os.path.join(REPO_ROOT, 'data', 'live'))
    parser.add_argument('--vendor', default=DEFAULT_VENDOR)
    parser.add_argument('--interval', type=float, default=DEFAULT_INTERVAL)
    parser.add_argument('--once', action='store_true', help='只跑一个 tick')
    parser.add_argument('--json', action='store_true', help='把最后一个 tick 的结果打到 stdout')
    args = parser.parse_args(argv)

    daemon = Daemon(args.bundle, args.live, args.vendor, args.interval, once=args.once)
    code = daemon.run()
    if args.json:
        print(json.dumps({'ticks': daemon.ticks, 'errors': daemon.errors,
                          'last_probe': daemon.last_probe,
                          'last_ingest': daemon.last_ingest},
                         ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
