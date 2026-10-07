"""
build_chat_history.py - 分层采样聊天记录，供 AI 深度分析使用

两种运行模式：

1. 预扫描（--preview）
   输出 JSON，包含时间范围选项和推荐值，供 Skill 向用户展示。

2. 生成（--output + --since）
   在用户选定的时间范围内做分层采样，输出 chat_history.txt。
   四个关键窗口：起源 / 高冲突区间 / 最近30天 / 修复时刻

用法：
  python scripts/build_chat_history.py --input data/messages.json --preview
  python scripts/build_chat_history.py --input data/messages.json \\
    --output data/chat_history.txt --since 2026-01-13
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from lexicon import count_conflict

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

GAP_THRESHOLD = 24 * 3600  # 24 小时沉默 = 修复时刻边界


def load_messages(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ts_to_dt(ts):
    return datetime.fromtimestamp(ts)


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def is_text(msg):
    return msg.get("type") == "text" and msg.get("content", "").strip()


# ──────────────────────────────────────────────
# PREVIEW MODE
# ──────────────────────────────────────────────

def build_preview(data):
    msgs = data.get("messages", [])
    if not msgs:
        print(json.dumps({"error": "消息列表为空"}, ensure_ascii=False))
        sys.exit(1)

    total = len(msgs)
    first_ts = msgs[0]["timestamp"]
    last_ts = msgs[-1]["timestamp"]
    first_dt = ts_to_dt(first_ts)
    last_dt = ts_to_dt(last_ts)
    span_days = (last_dt - first_dt).days + 1

    cutoffs = [
        ("最近 1 个月", 30),
        ("最近 3 个月", 90),
        ("最近半年", 180),
        ("最近 1 年", 365),
    ]

    suggestions = []
    for label, days in cutoffs:
        since_dt = last_dt - timedelta(days=days)
        since_ts = since_dt.timestamp()
        count = sum(1 for m in msgs if m["timestamp"] >= since_ts)
        if days > span_days and count == total:
            continue  # 范围超过实际跨度，跳过
        suggestions.append({
            "label": label,
            "count": count,
            "date_from": since_dt.strftime("%Y-%m-%d"),
        })

    # 全量选项始终添加
    suggestions.append({
        "label": "全量",
        "count": total,
        "date_from": first_dt.strftime("%Y-%m-%d"),
    })

    # 推荐逻辑
    recommended_idx = len(suggestions) - 1  # 默认全量
    recommended_reason = "消息量较少，建议全量分析以获得最完整视角"

    if total >= 5000:
        # 找 3 个月
        for i, s in enumerate(suggestions):
            if "3 个月" in s["label"]:
                recommended_idx = i
                recommended_reason = "消息量较大，3 个月覆盖近期主要互动模式，兼顾深度与精度"
                break
    elif total >= 2000:
        for i, s in enumerate(suggestions):
            if "3 个月" in s["label"]:
                recommended_idx = i
                recommended_reason = "消息量适中，3 个月采样可覆盖主要行为模式"
                break
    elif total >= 500:
        # 推荐 3 个月或全量，取消息更多的
        for i, s in enumerate(suggestions):
            if "3 个月" in s["label"] and s["count"] >= 300:
                recommended_idx = i
                recommended_reason = "3 个月内消息密度合理，推荐此范围"
                break

    for i, s in enumerate(suggestions):
        s["recommended"] = (i == recommended_idx)

    result = {
        "total": total,
        "date_range": [first_dt.strftime("%Y-%m-%d"), last_dt.strftime("%Y-%m-%d")],
        "span_days": span_days,
        "me_count": sum(1 for m in msgs if m.get("sender") == "me"),
        "them_count": sum(1 for m in msgs if m.get("sender") == "them"),
        "suggestions": suggestions,
        "recommended_reason": recommended_reason,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────
# GENERATE MODE
# ──────────────────────────────────────────────

def filter_by_since(msgs, since_str):
    """筛选 since 日期之后的消息"""
    if not since_str:
        return msgs
    since_dt = datetime.strptime(since_str, "%Y-%m-%d")
    since_ts = since_dt.timestamp()
    return [m for m in msgs if m["timestamp"] >= since_ts]


def find_conflict_window(text_msgs, window=100):
    """找负向情绪词密度最高的连续 window 条文本消息窗口"""
    if len(text_msgs) <= window:
        return text_msgs
    best_score = -1
    best_start = 0
    for i in range(len(text_msgs) - window + 1):
        score = sum(count_conflict(m["content"]) for m in text_msgs[i:i + window])
        if score > best_score:
            best_score = score
            best_start = i
    return text_msgs[best_start:best_start + window]


def find_repair_moments(msgs, gap=GAP_THRESHOLD, post_count=20):
    """找每次 >gap 秒沉默后的前 post_count 条消息"""
    result = []
    prev_ts = msgs[0]["timestamp"] if msgs else 0
    i = 0
    while i < len(msgs):
        ts = msgs[i]["timestamp"]
        if ts - prev_ts >= gap and i > 0:
            # 取沉默后最多 post_count 条
            chunk = msgs[i:i + post_count]
            result.append(chunk)
        prev_ts = ts
        i += 1
    return result


def format_msg(m, annot=None):
    """`annot` 是 `{local_id: (valence, arousal)}`；有就在行尾挂情绪坐标。

    坐标由 `stats_analyzer.py --affect auto` 算出并写在旁车文件
    `affect_messages.json` 里——本脚本**不自己加载模型**，保持零依赖。
    没有旁车文件时原样输出，不报错、不降级提示。
    """
    sender = "我" if m.get("sender") == "me" else "TA"
    line = f"[{fmt_ts(m['timestamp'])}] {sender}: {m.get('content', '').strip()}"
    if annot:
        pt = annot.get(m.get("local_id"))
        if pt:
            line += f"  ⟨{pt[0]:+.2f}/{pt[1]:+.2f}⟩"

    # 引用回复：把被引的那条也带出来。没有它，一问一答会看起来像答非所问。
    quote = m.get("quote")
    if quote:
        who = {"me": "我", "them": "TA"}.get(quote.get("sender"), "?")
        body = (quote.get("content") or "").strip()
        if quote.get("source") == "refermsg":
            # 来自 message_*.db 的 appmsg XML，是原文
            nick = quote.get("display_name")
            nick_txt = f"，微信昵称「{nick}」" if nick else ""
            line += f"\n{' ' * 6}↳ 引用 {who}{nick_txt}：{body}"
        else:
            # FTS 不存被引原文，这里是按相邻消息重建的——必须标出来
            gap = quote.get("gap_seconds")
            gap_txt = f"，间隔 {gap // 60} 分钟" if isinstance(gap, int) else ""
            line += f"\n{' ' * 6}↳ 〔推断〕引用 {who}{gap_txt}：{body}"
    return line


def write_window(f, title, msgs, annot=None):
    f.write(f"\n{'='*60}\n")
    f.write(f"=== {title} ===\n")
    f.write(f"{'='*60}\n")
    for m in msgs:
        f.write(format_msg(m, annot) + "\n")
    f.write(f"\n（共 {len(msgs)} 条）\n")


def load_annotations(input_path):
    """读旁车文件 `affect_messages.json`（与 messages.json 同目录）。

    没有就返回 None——**不报错、不提示、不降级显示**。情感层是可选的，
    缺了只是少一个坐标，不是错误。
    """
    sidecar = os.path.join(os.path.dirname(os.path.abspath(input_path)),
                           "affect_messages.json")
    if not os.path.exists(sidecar):
        return None
    try:
        with open(sidecar, encoding="utf-8") as f:
            pts = json.load(f).get("points", [])
        return {p["local_id"]: (p["v"], p["a"]) for p in pts}
    except (OSError, ValueError, KeyError):
        return None


def build_generate(data, since_str, output_path, annot=None):
    msgs = data.get("messages", [])
    if not msgs:
        print(json.dumps({"error": "消息列表为空"}, ensure_ascii=False))
        sys.exit(1)

    scoped = filter_by_since(msgs, since_str)
    if not scoped:
        print(json.dumps({"error": f"时间范围 {since_str} 之后无消息"}, ensure_ascii=False))
        sys.exit(1)

    text_msgs = [m for m in scoped if is_text(m)]
    all_msgs = scoped  # 窗口 4 用全类型（含撤回、语音等也有时间信息）

    total = len(scoped)
    first_ts = scoped[0]["timestamp"]
    last_ts = scoped[-1]["timestamp"]
    me_count = sum(1 for m in scoped if m.get("sender") == "me")
    them_count = sum(1 for m in scoped if m.get("sender") == "them")

    # 最近 30 天（取窗口内**最新**的 200 条；用 [:200] 会取到最旧的 200 条，
    # 一旦窗口内文字消息 >200，最近的部分就被整段切掉——窗口名叫「最近 30 天」，
    # 却系统性漏掉最近一周。见 memory: project-she-love-me-stats-fixes）
    recent_cutoff = last_ts - 30 * 86400
    recent_msgs = [m for m in text_msgs if m["timestamp"] >= recent_cutoff][-200:]

    # 修复时刻
    repair_chunks = find_repair_moments(all_msgs)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        # 概览头
        f.write("=" * 60 + "\n")
        f.write("=== 聊天记录分析范围概览 ===\n")
        f.write("=" * 60 + "\n")
        f.write(f"时间范围: {fmt_ts(first_ts)} ~ {fmt_ts(last_ts)}\n")
        f.write(f"总消息数: {total} 条（含非文字消息）\n")
        f.write(f"文字消息: {len(text_msgs)} 条\n")
        f.write(f"我方发送: {me_count} 条 | 对方发送: {them_count} 条\n")
        f.write(f"发起占比: 我方 {me_count/total*100:.1f}% | 对方 {them_count/total*100:.1f}%\n")
        f.write("\n⚠️ 以下为分层采样关键窗口，不代表全量记录。\n")
        f.write("   统计层全量数据请参见 data/stats.json。\n")
        if any(m.get("quote") for m in scoped):
            f.write("\n带 ↳ 的行是被引用的原消息（引用回复）。两种来源，可信度不同：\n")
            f.write("  ↳ 引用 …（无标记）  来自 message_*.db 的 appmsg XML，**是原文**\n")
            f.write("  ↳ 〔推断〕引用 …    FTS 不存被引原文（message_1.db 无密钥），"
                    "按「最近一条对方消息」重建，\n"
                    "                      **是推断不是事实**，间隔越大越不可信，"
                    "引用时须自行核对\n")
        if annot:
            f.write("\n每行结尾的 ⟨效价/唤醒⟩ 由情感计算层给出，取值 [-1, 1]：\n")
            f.write("  效价  −1 极负 ←→ +1 极正      唤醒  −1 极平静 ←→ +1 极激动\n")
            f.write("  ⚠️ 单条坐标只是一个坐标，不是判断。已知弱点：疲惫类表达偏中性"
                    "（「好累啊」≈0），这类请以词面为准。\n")

        # 窗口 1：起源（最早 100 条文本）
        window1 = text_msgs[:100]
        write_window(f, "窗口 1：关系起源（最早 100 条文字消息）", window1, annot)

        # 窗口 2：高冲突区间
        window2 = find_conflict_window(text_msgs, 100)
        w2_start = fmt_ts(window2[0]["timestamp"]) if window2 else "N/A"
        write_window(f, f"窗口 2：高冲突区间（从 {w2_start} 起的 100 条）", window2, annot)

        # 窗口 3：最近 30 天
        write_window(f, f"窗口 3：最近 30 天（最多 200 条文字消息）", recent_msgs, annot)

        # 窗口 4：修复时刻
        if repair_chunks:
            f.write("\n" + "=" * 60 + "\n")
            f.write("=== 窗口 4：修复时刻（每次 >24h 沉默后的对话恢复） ===\n")
            f.write("=" * 60 + "\n")
            for idx, chunk in enumerate(repair_chunks[:10], 1):  # 最多 10 次
                gap_start = fmt_ts(chunk[0]["timestamp"])
                f.write(f"\n--- 修复时刻 {idx}（{gap_start} 沉默后恢复）---\n")
                for m in chunk:
                    f.write(format_msg(m, annot) + "\n")
            total_gaps = len(repair_chunks)
            if total_gaps > 10:
                f.write(f"\n（共识别 {total_gaps} 次沉默-修复事件，展示前 10 次）\n")
        else:
            f.write("\n" + "=" * 60 + "\n")
            f.write("=== 窗口 4：修复时刻 ===\n")
            f.write("=" * 60 + "\n")
            f.write("未检测到超过 24 小时的沉默间隔。\n")

    result = {
        "status": "ok",
        "output": output_path,
        "scope_total": total,
        "text_count": len(text_msgs),
        "windows": {
            "origin": len(window1),
            "conflict": len(window2),
            "recent_30d": len(recent_msgs),
            "repair_moments": len(repair_chunks),
        }
    }
    print(json.dumps(result, ensure_ascii=False))
    print(f"[+] 已生成分层聊天记录 -> {output_path}", file=sys.stderr)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="分层采样聊天记录")
    parser.add_argument("--input", required=True, help="messages.json 路径")
    parser.add_argument("--preview", action="store_true", help="仅输出时间范围建议 JSON，不生成文件")
    parser.add_argument("--output", help="输出 chat_history.txt 路径（生成模式）")
    parser.add_argument("--since", help="起始日期 YYYY-MM-DD（生成模式，不传则用全量）")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(json.dumps({"error": f"找不到 {args.input}，请先运行 extract_messages.py"}, ensure_ascii=False))
        sys.exit(1)

    data = load_messages(args.input)

    if args.preview:
        build_preview(data)
    elif args.output:
        build_generate(data, args.since, args.output, load_annotations(args.input))
    else:
        parser.error("请指定 --preview 或 --output")


if __name__ == "__main__":
    main()
