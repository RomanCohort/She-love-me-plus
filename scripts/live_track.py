# -*- coding: utf-8 -*-
"""
live_track.py - 「她不一样」实时追踪层（确定性那一层）

一次调用做一件事：把 bundle 里最新的 messages.json 折算成 live_state.json。
设计成能被定时器反复调用 —— 没有新消息时它什么都不写，也不改任何东西。

它不做两件事，故意的：
  1. 不重新解密微信数据库。那是数据层，得先跑 decrypt_wechat.py + extract_messages.py。
  2. 不调用大模型。LLM 那一层读 live_state.json 里的 pending_llm 再决定要不要动。

用法（必须用带 torch 的解释器，通常是 C:/Program Files/Python313/python.exe）：
  python scripts/live_track.py --bundle data/contacts/<联系人>__<hash> --json
  python scripts/live_track.py --bundle ... --force          # 忽略指纹，强制重算
"""

import argparse
import importlib
import json
import os
import random
import subprocess
import sys
import time
import types
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_RL_ROOT = r'D:\she-love-me-rl'
STATE_FILE = 'live_state.json'
STATE_VERSION = 1


def read_json(path):
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def write_json(path, payload):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def die(message, code=2):
    print(json.dumps({'status': 'error', 'error': message}, ensure_ascii=False))
    sys.exit(code)


def fingerprint(messages):
    stamps = [m.get('timestamp') for m in messages if isinstance(m.get('timestamp'), (int, float))]
    if not stamps:
        return {'count': 0, 'first_ts': None, 'last_ts': None}
    return {'count': len(messages), 'first_ts': min(stamps), 'last_ts': max(stamps)}


# ---------------------------------------------------------------- RL 层

def load_rl(rl_root):
    # 按文件路径把 core/mdp.py 和 core/dqn.py 挂进来。
    # 不能直接 import core —— core/__init__.py 会把 transformers 那一串
    # （advanced_models / dialogue_generator / ...）全拖进来。
    # 这里造一个空的包壳，只让 dqn.py 里那行相对导入能解析。
    core_dir = os.path.join(rl_root, 'core')
    if not os.path.isdir(core_dir):
        die('找不到 RL 项目目录：' + core_dir)
    pkg_name = '_slmrl_core'
    if pkg_name not in sys.modules:
        shell = types.ModuleType(pkg_name)
        shell.__path__ = [core_dir]
        sys.modules[pkg_name] = shell
    try:
        mdp = importlib.import_module(pkg_name + '.mdp')
        dqn = importlib.import_module(pkg_name + '.dqn')
    except ImportError as exc:
        die('RL 模块导入失败：' + str(exc) + ' | 需要 numpy + torch。当前解释器：' + sys.executable)
    return mdp, dqn


def build_context(messages, now):
    # MDP 要的那几个上下文量。每个怎么来的都写清楚，别让它变成黑盒。
    last = messages[-1] if messages else {}
    last_ts = float(last.get('timestamp') or 0)
    last_sender = last.get('sender')
    last_content = str(last.get('content') or '')
    reply_pending = last_sender == 'them'
    pending = max(0.0, now - last_ts) if reply_pending else 0.0

    # message_importance 没有现成来源，下面是显式占位规则，不是从某处读来的。
    if '?' in last_content or '？' in last_content:
        importance = 0.7
    elif len(last_content.strip()) <= 3:
        importance = 0.3
    else:
        importance = 0.5

    return {
        'reply_pending': reply_pending,
        'pending_duration': pending,
        'hour': datetime.fromtimestamp(now).hour,
        'message_importance': importance,
        '_last_sender': last_sender,
        '_last_ts': last_ts,
    }


RL_POLICY_NOTE = (
    'agent.pth 是在 200 条模拟对话上训出来的（见 she-love-me-rl/README.md）。'
    '它给真人的建议没有经过验证 —— 看 q_spread：如果 9 个 Q 值挤在一起，'
    '说明这个状态对它来说是没见过的，那个动作等于抽签。'
)


class UntrainedWeights(RuntimeError):
    pass


def advise_rl(mdp_mod, dqn_mod, state, weights, epsilon_override):
    if not os.path.isfile(weights):
        die('找不到权重文件：' + weights)
    torch = dqn_mod.torch
    agent = dqn_mod.DQNAgent(state_dim=9, action_dim=9, hidden_dim=128, device='cpu')
    checkpoint = torch.load(weights, map_location='cpu', weights_only=False)
    missing, unexpected = agent.q_network.load_state_dict(checkpoint['q_network'], strict=False)
    agent.target_network.load_state_dict(checkpoint['target_network'], strict=False)
    if missing or unexpected:
        die('权重和网络结构对不上。缺失=' + str(list(missing)) + ' 多余=' + str(list(unexpected)))

    # 先看这张网到底训过没有。没有就抛出去 ——
    # 不能用一张空网给出的动作冒充「模型的建议」。
    steps = int(checkpoint.get('step_counter') or 0)
    optimizer_states = len((checkpoint.get('optimizer') or {}).get('state') or {})
    if steps <= 0 and optimizer_states == 0:
        raise UntrainedWeights(
            '权重是未训练的：step_counter=' + str(steps) + '，优化器 state=' + str(optimizer_states) + ' 条。'
            '这个文件是刚初始化就被保存下来的，它给出的动作等于随机抽。'
        )

    # DQNAgent.load() 只恢复网络和优化器，不恢复 epsilon。
    # 不补这一步，epsilon 会停在初始值 1.0 —— 那就是纯随机抽动作。
    trained_epsilon = float(checkpoint.get('epsilon', agent.epsilon_end))
    epsilon = trained_epsilon if epsilon_override is None else float(epsilon_override)

    state_vec = state.to_vector()
    with torch.no_grad():
        q_values = agent.q_network(torch.from_numpy(state_vec).unsqueeze(0), None)[0].tolist()

    if random.random() < epsilon:
        action_idx = random.randint(0, 8)
        picked = '探索：按 epsilon 抽的，不是模型选的'
    else:
        action_idx = max(range(len(q_values)), key=lambda i: q_values[i])
        picked = '贪心：取 Q 值最大'

    action = mdp_mod.ActionType(action_idx)
    meta = mdp_mod.ACTION_SPACE[action]
    ordered = sorted(q_values, reverse=True)
    spread = round(ordered[0] - ordered[-1], 4)
    return {
        'policy': 'rl:models/agent.pth',
        'policy_note': RL_POLICY_NOTE,
        'epsilon_used': epsilon,
        'epsilon_from_checkpoint': trained_epsilon,
        'epsilon_source': 'checkpoint' if epsilon_override is None else '--epsilon',
        'selection': picked,
        'action': action.name,
        'action_index': int(action_idx),
        'action_description': meta.description,
        'risk_level': meta.risk_level,
        'expected_outcome': meta.expected_outcome,
        'q_values': [round(float(q), 4) for q in q_values],
        'q_spread': spread,
        'q_spread_verdict': '有信号' if spread > 0.05 else '几乎无差别，此状态下建议等于抽签',
        'trained_steps': steps,
    }


def advise_rl_untrained(mdp_mod, dqn_mod, state, weights, epsilon_override):
    """
    只在 --allow-untrained 时走这里。
    取贪心动作，但 policy 字段必须写明「未训练」——不许让它看起来像正常建议。
    """
    result = _advise_rl_untrained(mdp_mod, dqn_mod, state, weights, epsilon_override)
    result['policy'] = 'rl:models/agent.pth(未训练)'
    result['policy_note'] = '这张网没训过（step_counter=0）。下面那个动作等于随机抽，不要当建议用。'
    result['q_spread_verdict'] = '无意义：网络未训练'
    return result


def _advise_rl_untrained(mdp_mod, dqn_mod, state, weights, epsilon_override):
    torch = dqn_mod.torch
    agent = dqn_mod.DQNAgent(state_dim=9, action_dim=9, hidden_dim=128, device='cpu')
    checkpoint = torch.load(weights, map_location='cpu', weights_only=False)
    agent.q_network.load_state_dict(checkpoint['q_network'], strict=False)
    agent.target_network.load_state_dict(checkpoint['target_network'], strict=False)
    trained_epsilon = float(checkpoint.get('epsilon', agent.epsilon_end))
    epsilon = trained_epsilon if epsilon_override is None else float(epsilon_override)
    with torch.no_grad():
        q_values = agent.q_network(torch.from_numpy(state.to_vector()).unsqueeze(0), None)[0].tolist()
    action_idx = max(range(len(q_values)), key=lambda i: q_values[i])
    action = mdp_mod.ActionType(action_idx)
    meta = mdp_mod.ACTION_SPACE[action]
    ordered = sorted(q_values, reverse=True)
    return {
        'policy': 'rl:models/agent.pth',
        'policy_note': RL_POLICY_NOTE,
        'epsilon_used': epsilon,
        'epsilon_from_checkpoint': trained_epsilon,
        'epsilon_source': 'checkpoint' if epsilon_override is None else '--epsilon',
        'selection': '贪心：取 Q 值最大（网络未训练，等于抽签）',
        'action': action.name,
        'action_index': int(action_idx),
        'action_description': meta.description,
        'risk_level': meta.risk_level,
        'expected_outcome': meta.expected_outcome,
        'q_values': [round(float(q), 4) for q in q_values],
        'q_spread': round(ordered[0] - ordered[-1], 4),
        'q_spread_verdict': '无意义：网络未训练',
        'trained_steps': 0,
    }


RULE_POLICY_NOTE = (
    '这是显式的保守规则策略，不是 she-love-me-rl 里训练出来的那个。'
    '只有加了 --allow-rule-policy 才会用它。'
)


def advise_rule(mdp_mod, state):
    cold = state.last_cold_score
    loved = state.loved_index
    simp = state.simp_index
    stage = state.conversation_stage
    pending_hours = state.pending_duration / 3600.0

    if state.reply_pending and pending_hours <= 2:
        reason = '对方最后说话且不到 2 小时 —— 回'
        action = mdp_mod.ActionType.REPLY_NOW
    elif cold >= 40:
        reason = '冷淡指数 ' + str(cold) + ' 偏高 —— 先退一步'
        action = mdp_mod.ActionType.TAKE_SPACE
    elif simp >= 70 and loved < 50:
        reason = '主动 ' + str(simp) + ' / 被爱 ' + str(loved) + ' —— 单向投入，别再压'
        action = mdp_mod.ActionType.TAKE_SPACE
    elif loved >= 60 and stage >= 3:
        reason = '被爱 ' + str(loved) + ' 且阶段 ' + str(stage) + ' —— 可以推进'
        action = mdp_mod.ActionType.SUGGEST_MEETING
    else:
        reason = '没有触发任何一条规则'
        action = mdp_mod.ActionType.NO_ACTION

    meta = mdp_mod.ACTION_SPACE[action]
    return {
        'policy': 'rule:conservative-v1',
        'policy_note': RULE_POLICY_NOTE,
        'selection': reason,
        'action': action.name,
        'action_index': int(action.value),
        'action_description': meta.description,
        'risk_level': meta.risk_level,
        'expected_outcome': meta.expected_outcome,
        'q_values': None,
        'q_spread': None,
        'q_spread_verdict': '规则策略没有 Q 值',
    }


# ---------------------------------------------------------------- LLM 输入包

PLACEHOLDERS = {
    'image': '[图片]',
    'voice': '[语音]',
    'video': '[视频]',
    'emoji': '[表情]',
    'file': '[文件]',
    'card': '[名片]',
    'location': '[位置]',
    'call': '[通话]',
    'system': '[系统消息]',
}


def render_line(message):
    kind = message.get('type') or 'text'
    if kind == 'text':
        body = str(message.get('content') or '').strip()
    else:
        body = PLACEHOLDERS.get(kind, '[' + str(kind) + ']')
    who = '我' if message.get('sender') == 'me' else '她'
    stamp = datetime.fromtimestamp(float(message.get('timestamp') or 0)).strftime('%Y-%m-%d %H:%M')
    return stamp + '  ' + who + ': ' + body


def pick_window(messages, min_window, lead, analysis_mtime):
    """
    决定送哪一段给 LLM。

    规则：起点 = 第一条比 analysis.json 新的消息，再往前带 lead 条；
          但不管怎样至少给 min_window 条。
    所以：
        - 结论很旧（断档 101 天）→ 把整段都带上，不会只给最后 60 条接不上
        - 结论很新（十分钟前更新的）→ 至少 60 条，不会只剩两条没法判断
    """
    start = None
    if analysis_mtime:
        for index, message in enumerate(messages):
            if (message.get('timestamp') or 0) > analysis_mtime:
                start = index
                break
    if start is None:
        start = max(0, len(messages) - min_window)
    else:
        start = max(0, start - lead)
        start = min(start, max(0, len(messages) - min_window))
    return messages[start:], start


def build_llm_input(messages, window, lead, analysis_path, new_since_ts, analysis_mtime):
    """
    把 LLM 那一步要的东西一次性装好：该看的那一段 + 背景分析 + 增量说明。
    调用方（agent 或脚本）不需要再自己去翻 messages.json。
    """
    tail, start_index = pick_window(messages, window, lead, analysis_mtime)
    rendered = [render_line(m) for m in tail]

    background = None
    if os.path.isfile(analysis_path):
        try:
            background = read_json(analysis_path)
        except Exception as exc:
            background = {'_read_error': type(exc).__name__ + ': ' + str(exc)}

    delta = None
    if new_since_ts is not None:
        delta = {
            'since_ts': new_since_ts,
            'count': sum(1 for m in messages if (m.get('timestamp') or 0) > new_since_ts),
        }

    return {
        'min_window': window,
        'lead': lead,
        'start_index': start_index,
        'message_count': len(tail),
        'covered_count': sum(1 for m in tail if analysis_mtime and (m.get('timestamp') or 0) > analysis_mtime),
        'messages': rendered,
        'rendered': chr(10).join(rendered),
        'background_analysis': background,
        'background_analysis_path': analysis_path,
        'delta': delta,
        'note': (
            '给 LLM 那一步的输入包：' + str(len(tail)) + ' 条'
            '（其中 ' + str(sum(1 for m in tail if analysis_mtime and (m.get('timestamp') or 0) > analysis_mtime))
            + ' 条产生于现有结论之后）+ 背景分析。'
            '不要把它塞进全量 chat_history —— 那是旧流程。'
        ),
    }


# ---------------------------------------------------------------- stats

def run_stats(messages_path, stats_path):
    script = os.path.join(SCRIPT_DIR, 'stats_analyzer.py')
    if not os.path.isfile(script):
        die('找不到 stats_analyzer.py：' + script)
    proc = subprocess.run(
        [sys.executable, '-X', 'utf8', script, '--input', messages_path, '--output', stats_path],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    return {
        'returncode': proc.returncode,
        'stdout_tail': (proc.stdout or '').strip().splitlines()[-1:],
        'stderr_tail': (proc.stderr or '').strip().splitlines()[-3:],
    }


def main():
    parser = argparse.ArgumentParser(description='实时追踪一层：messages.json -> live_state.json')
    parser.add_argument('--bundle', required=True, help='联系人 bundle 目录')
    parser.add_argument('--contact', help='联系人显示名，默认读 messages.json')
    parser.add_argument('--rl-root', default=DEFAULT_RL_ROOT)
    parser.add_argument('--weights', help='默认 <rl-root>/models/agent.pth')
    parser.add_argument('--epsilon', type=float, help='覆盖权重里存的 epsilon；给 0 就是纯贪心')
    parser.add_argument('--advice', choices=['auto', 'rl', 'rule', 'none'], default='auto',
                        help='auto=有训练好的权重就用，否则报错；rl=强制用权重；rule=显式规则；none=不产出建议')
    parser.add_argument('--allow-untrained', action='store_true', help='允许用未训练的权重（仅调试用，结果会标注）')
    parser.add_argument('--no-stats', action='store_true', help='不重跑 stats_analyzer')
    parser.add_argument('--force', action='store_true', help='忽略指纹，强制重算')
    parser.add_argument('--llm-window', type=int, default=60, help='LLM 输入包最少带多少条，默认 60')
    parser.add_argument('--llm-lead', type=int, default=20, help='起点再往前带几条做上下文，默认 20')
    parser.add_argument('--json', action='store_true', help='把 live_state 打到 stdout')
    args = parser.parse_args()

    bundle = os.path.abspath(args.bundle)
    if not os.path.isdir(bundle):
        die('bundle 目录不存在：' + bundle)
    messages_path = os.path.join(bundle, 'messages.json')
    if not os.path.isfile(messages_path):
        die('缺 messages.json：' + messages_path)
    stats_path = os.path.join(bundle, 'stats.json')
    state_path = os.path.join(bundle, STATE_FILE)

    payload = read_json(messages_path)
    messages = payload.get('messages') or []
    if not messages:
        die('messages.json 里没有消息')
    messages = sorted(messages, key=lambda m: m.get('timestamp') or 0)

    now = time.time()
    print_fp = fingerprint(messages)

    previous = None
    if os.path.isfile(state_path):
        try:
            previous = read_json(state_path)
        except Exception:
            previous = None

    if previous and not args.force and previous.get('fingerprint') == print_fp:
        print(json.dumps({
            'status': 'ok',
            'changed': False,
            'bundle_dir': bundle,
            'fingerprint': print_fp,
            'state_path': state_path,
            'data_age_seconds': round(now - (print_fp['last_ts'] or now)),
        }, ensure_ascii=False))
        return

    stats_info = None
    if not args.no_stats or not os.path.isfile(stats_path):
        stats_info = run_stats(messages_path, stats_path)
        if stats_info['returncode'] != 0 or not os.path.isfile(stats_path):
            die('stats_analyzer 失败：' + json.dumps(stats_info, ensure_ascii=False))
    stats = read_json(stats_path)

    mdp_mod, dqn_mod = load_rl(args.rl_root)
    context = build_context(messages, now)
    state = mdp_mod.RelationshipMDP().get_state(stats, context)

    weights = args.weights or os.path.join(args.rl_root, 'models', 'agent.pth')
    advice = None
    if args.advice == 'none':
        advice = {'policy': 'none', 'note': '按 --advice none 跳过建议层，只出状态向量和 LLM 输入包。'}
    elif args.advice == 'rule':
        advice = advise_rule(mdp_mod, state)
    else:
        try:
            advice = advise_rl(mdp_mod, dqn_mod, state, weights, args.epsilon)
        except UntrainedWeights as exc:
            if args.advice == 'auto' and args.allow_untrained:
                advice = advise_rl_untrained(mdp_mod, dqn_mod, state, weights, args.epsilon)
            elif args.advice == 'rl' and args.allow_untrained:
                advice = advise_rl_untrained(mdp_mod, dqn_mod, state, weights, args.epsilon)
            elif args.advice == 'rl':
                die('RL 权重未训练，拒绝输出。' + str(exc) + ' | 要强行用加 --allow-untrained，要规则策略用 --advice rule。')
            else:
                die(
                    str(exc) + ' | 所以 --advice auto 拒绝了。'
                    '选一个：--advice rule（显式保守规则）或 --advice none（只出状态向量）。'
                )

    analysis_path = os.path.join(bundle, 'analysis.json')
    analysis_exists = os.path.isfile(analysis_path)
    previous_analysis_mtime = os.path.getmtime(analysis_path) if analysis_exists else None
    new_since = previous.get('fingerprint', {}).get('last_ts') if previous else None

    # 「要不要重跑 LLM」不看追踪层自己写过什么，只看一件事：
    # messages.json 里有没有比 analysis.json 更新的消息。这样它不会因为
    # 追踪层多跑了一次就假装自己是最新的。
    last_ts = print_fp['last_ts']
    if not analysis_exists:
        needs_llm = True
        analysis_lag_seconds = None
    else:
        analysis_lag_seconds = round((last_ts or 0) - previous_analysis_mtime, 1)
        needs_llm = analysis_lag_seconds > 0

    live = {
        'version': STATE_VERSION,
        'contact': args.contact or payload.get('contact_display') or os.path.basename(bundle),
        'bundle_dir': bundle,
        'generated_at': now,
        'generated_at_human': datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S'),
        'fingerprint': print_fp,
        'data_age_seconds': round(now - (print_fp['last_ts'] or now)),
        'last_message_human': datetime.fromtimestamp(print_fp['last_ts']).strftime('%Y-%m-%d %H:%M:%S') if print_fp['last_ts'] else None,
        'stats_source': stats_path,
        'stats_summary': {
            'total_messages': (stats.get('basic') or {}).get('total_messages'),
            'date_range': (stats.get('basic') or {}).get('date_range'),
            'my_ratio': (stats.get('basic') or {}).get('my_ratio'),
            'scores': stats.get('scores'),
            'initiative': stats.get('initiative'),
            'reply_speed': stats.get('reply_speed'),
            'repair': stats.get('repair'),
        },
        'context': context,
        'state_vector': json.loads(json.dumps(state.__dict__, default=float)),
        'advice': advice,
        'llm_input': build_llm_input(messages, args.llm_window, args.llm_lead, analysis_path, new_since,
                                      previous_analysis_mtime),
        'pending_llm': {
            'needs_update': needs_llm,
            'reason': ('analysis.json 不存在' if not analysis_exists
                       else ('最新消息比 analysis.json 新 ' + str(analysis_lag_seconds) + ' 秒' if needs_llm
                             else 'analysis.json 不比最新消息旧')),
            'analysis_path': analysis_path,
            'analysis_mtime': previous_analysis_mtime,
            'analysis_lag_seconds': analysis_lag_seconds,
            'last_message_ts': last_ts,
            'new_since_ts': new_since,
        },
    }
    write_json(state_path, live)

    if args.json:
        result = {'status': 'ok', 'changed': True, 'live_state': live}
    else:
        result = {
            'status': 'ok',
            'changed': True,
            'bundle_dir': bundle,
            'state_path': state_path,
            'fingerprint': print_fp,
            'advice': {
                'policy': advice.get('policy'),
                'action': advice.get('action'),
                'description': advice.get('action_description'),
                'note': advice.get('note'),
            },
            'scores': live['stats_summary']['scores'],
            'pending_llm': live['pending_llm']['needs_update'],
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        die(type(exc).__name__ + ': ' + str(exc), 3)
