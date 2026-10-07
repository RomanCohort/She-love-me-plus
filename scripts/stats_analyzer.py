"""
stats_analyzer.py - 统计分析引擎

输入: messages.json (extract_messages.py 的输出)
输出: stats.json (纯数字统计，供 Claude AI 分析和 HTML 报告使用)

统计维度:
  - 基础数据: 消息总量、双方占比、时间跨度
  - 主动性: 谁先发起对话
  - 回复速度: 双方平均回复时间
  - 消息长度: 双方平均字数
  - 轰炸检测: 连续发多条未回
  - 冷淡回复: "嗯" "哦" "好" 等单字/敷衍
  - 晚安分析: 谁先说晚安
  - 活跃时段分布
  - 语言学特征: 代词、模糊词、条件句、情绪词、撤回消息
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from lexicon import (
    dismissive_form,
    is_goodnight,
    count_hedging,
    count_conditional,
    count_positive,
    count_negative,
)
# 口径常量集中在 metrics_defs —— 历史教训：同一个指标两套口径会让报告里的数
# 和代码算的数分叉，而更正只能靠人手写进散文。三套会话口径都存，谁用哪套写明。
from metrics_defs import SESSION_GAPS, session_starts, COLD_WAR_GAP

# Windows 控制台 UTF-8 输出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 对话间隔阈值（秒）: 超过此时间视为新对话开始
NEW_CONVERSATION_GAP = 3 * 3600  # 3小时

def load_messages(path):
    # Accept UTF-8 with or without BOM because Windows editors often save JSON with BOM.
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    return data


def filter_text_messages(messages):
    """只保留文字类型消息用于内容分析"""
    return [m for m in messages if m["type"] == "text" and m["sender"] in ("me", "them")]


def detect_conversations(messages):
    """将消息序列切分为对话（session），返回每个对话的起始人"""
    if not messages:
        return []

    conversations = []
    current = [messages[0]]

    for msg in messages[1:]:
        gap = msg["timestamp"] - current[-1]["timestamp"]
        if gap > NEW_CONVERSATION_GAP:
            conversations.append(current)
            current = [msg]
        else:
            current.append(msg)

    if current:
        conversations.append(current)

    return conversations


def analyze_reply_times(messages):
    """计算双方平均回复时间（秒）"""
    my_reply_times = []
    their_reply_times = []
    last_sender = None
    last_time = None

    for msg in messages:
        if last_sender is not None and msg["sender"] != last_sender:
            gap = msg["timestamp"] - last_time
            if 10 <= gap <= 86400:  # 忽略太短（同时发）和太长（隔天）
                if msg["sender"] == "me":
                    my_reply_times.append(gap)
                else:
                    their_reply_times.append(gap)
        last_sender = msg["sender"]
        last_time = msg["timestamp"]

    avg_my = sum(my_reply_times) / len(my_reply_times) if my_reply_times else 0
    avg_their = sum(their_reply_times) / len(their_reply_times) if their_reply_times else 0
    return avg_my, avg_their


def detect_bombing(messages):
    """检测连续发送多条未回的情况（轰炸）"""
    my_bombs = 0
    their_bombs = 0
    my_consecutive = 0
    their_consecutive = 0
    my_max_consecutive = 0
    their_max_consecutive = 0

    last_sender = None
    for msg in messages:
        sender = msg["sender"]
        if sender == last_sender:
            if sender == "me":
                my_consecutive += 1
            else:
                their_consecutive += 1
        else:
            if last_sender == "me" and my_consecutive >= 3:
                my_bombs += 1
            if last_sender == "them" and their_consecutive >= 3:
                their_bombs += 1
            my_max_consecutive = max(my_max_consecutive, my_consecutive)
            their_max_consecutive = max(their_max_consecutive, their_consecutive)
            if sender == "me":
                my_consecutive = 1
                their_consecutive = 0
            else:
                their_consecutive = 1
                my_consecutive = 0
        last_sender = sender

    return {
        "my_bomb_count": my_bombs,
        "their_bomb_count": their_bombs,
        "my_max_consecutive": my_max_consecutive,
        "their_max_consecutive": their_max_consecutive,
    }


def detect_cold_replies(text_messages):
    """检测冷淡回复"""
    my_cold = 0
    their_cold = 0
    cold_word_count = defaultdict(int)

    for msg in text_messages:
        content = msg["content"].strip()
        # 归一化后整条等于白名单才算敷衍——不再拿长度兜底
        form = dismissive_form(content)
        if form is not None:
            cold_word_count[form] += 1
            if msg["sender"] == "me":
                my_cold += 1
            else:
                their_cold += 1

    return {
        "my_cold_count": my_cold,
        "their_cold_count": their_cold,
        "cold_words": dict(sorted(cold_word_count.items(), key=lambda x: -x[1])[:10]),
    }


def detect_unanswered(messages):
    """检测一方发消息后对方长时间未回（>2小时）"""
    my_unanswered = 0
    their_unanswered = 0
    last_sender = None
    last_time = None
    pending_sender = None

    for msg in messages:
        if last_sender is not None and msg["sender"] != last_sender:
            gap = msg["timestamp"] - last_time
            if gap > 7200 and pending_sender:  # >2小时
                if pending_sender == "me":
                    my_unanswered += 1
                else:
                    their_unanswered += 1
            pending_sender = None
        else:
            pending_sender = msg["sender"]

        last_sender = msg["sender"]
        last_time = msg["timestamp"]

    return {"my_unanswered": my_unanswered, "their_unanswered": their_unanswered}


def detect_goodnight(text_messages):
    """检测晚安/早安，判断谁先说"""
    my_goodnight = 0
    their_goodnight = 0

    for msg in text_messages:
        if is_goodnight(msg["content"]):
            if msg["sender"] == "me":
                my_goodnight += 1
            else:
                their_goodnight += 1

    return {"my_goodnight": my_goodnight, "their_goodnight": their_goodnight}


def detect_turn_response(text_messages, gap=3 * 3600):
    """burst 对 burst 的「回了多少」。

    把「一个人连续说的若干条」当一个单位（burst），看对方**整段**回了多少字。
    相比逐条统计，这样「连发 5 条 → 回 1 条」算**一个**样本，不会被拆成 5 个
    （拆开的话其中 4 个会被判成应答，数字完全是假的）。

    ⚠️ 它是一个**代理**，不是「投入度」本身。实测它和人工标注的「投入 / 应答」
    高度共线（AUC 0.893）——所以它和那个标签分享同样的方差，**不能用它去验证
    标签**，只能当一条独立的描述性指标。

    另一条实测结论：**对方说了多少，对本方回多少几乎没有预测力**（对方侧特征
    AUC 0.46–0.55）。所以两个方向可以直接比——不存在「他发的废话多、所以她回得
    少」那种分母问题。
    """
    if not text_messages:
        return None
    # burst：同人、相邻两条间隔 <= gap
    bursts = []
    i = 0
    while i < len(text_messages):
        j = i
        while (j + 1 < len(text_messages)
               and text_messages[j + 1]["sender"] == text_messages[i]["sender"]
               and text_messages[j + 1]["timestamp"] - text_messages[j]["timestamp"] <= gap):
            j += 1
        bursts.append((i, j, text_messages[i]["sender"], text_messages[i]["timestamp"]))
        i = j + 1

    out = {}
    for who, label in (("me", "them_to_me"), ("them", "me_to_them")):
        # A 是 who，统计 B 那一段的总字数
        lens = []
        for k in range(len(bursts) - 1):
            a, b = bursts[k], bursts[k + 1]
            if a[2] != who or b[2] == who:
                continue
            # 同一轮对话（间隔不超过 gap）才算回应
            if text_messages[b[0]]["timestamp"] - text_messages[a[1]]["timestamp"] > gap:
                continue
            lens.append(sum(len(text_messages[t]["content"]) for t in range(b[0], b[1] + 1)))
        if not lens:
            out[label] = None
            continue
        s = sorted(lens)
        out[label] = {
            "n": len(s),
            "median_chars": s[len(s) // 2],
            "mean_chars": round(sum(s) / len(s), 1),
            "p25_chars": s[len(s) // 4],
            "p75_chars": s[(3 * len(s)) // 4],
        }
    out["definition"] = ("burst 对 burst：A 连续若干条 → B 连续若干条，统计 B 那一段的总字数。"
                         "「连发 N 条 → 回 1 条」算一个样本。")
    out["caveat"] = ("这是**代理**不是「投入度」。它与人工标注的投入/应答高度共线"
                     "（AUC 0.893），因此不能用来验证那个标签。跨方向可直接比——"
                     "实测对方说了多少对本方回多少几乎无预测力。")
    return out


def analyze_linguistics(text_messages, all_messages):
    """分析语言学特征：代词、模糊词、条件句、情绪词、撤回消息"""
    my_we = 0
    their_we = 0
    my_i = 0
    their_i = 0
    my_hedging = 0
    their_hedging = 0
    my_conditional = 0
    their_conditional = 0
    my_positive = 0
    their_positive = 0
    my_negative = 0
    their_negative = 0

    for msg in text_messages:
        content = msg["content"]
        sender = msg["sender"]

        # 代词统计
        we_count = content.count("我们") + content.count("咱们") + content.count("咱")
        i_count = content.count("我") - we_count * 2  # 排除"我们"中的"我"
        i_count = max(i_count, 0)

        hedging_count = count_hedging(content)
        conditional_count = count_conditional(content)
        pos_count = count_positive(content)
        neg_count = count_negative(content)

        if sender == "me":
            my_we += we_count
            my_i += i_count
            my_hedging += hedging_count
            my_conditional += conditional_count
            my_positive += pos_count
            my_negative += neg_count
        else:
            their_we += we_count
            their_i += i_count
            their_hedging += hedging_count
            their_conditional += conditional_count
            their_positive += pos_count
            their_negative += neg_count

    # 撤回消息统计
    my_revoke = sum(1 for m in all_messages if m["sender"] == "me" and m["type"] == "revoke")
    their_revoke = sum(1 for m in all_messages if m["sender"] == "them" and m["type"] == "revoke")

    # 计算情绪正向比率（正向词 / 总情绪词）
    my_total_emotion = my_positive + my_negative
    their_total_emotion = their_positive + their_negative
    # 分母为 0 时输出 None（JSON null），不再兜底 0.5——0.5 是一个从未被测量过的
    # 「中性」，会被下游当成真实观测值去推导 Gottman 比值。
    my_pos_ratio = round(my_positive / my_total_emotion, 3) if my_total_emotion > 0 else None
    their_pos_ratio = round(their_positive / their_total_emotion, 3) if their_total_emotion > 0 else None

    return {
        "pronoun_we_count": {"me": my_we, "them": their_we},
        "pronoun_i_count": {"me": my_i, "them": their_i},
        "hedging_words_count": {"me": my_hedging, "them": their_hedging},
        "conditional_count": {"me": my_conditional, "them": their_conditional},
        "positive_emotion_count": {"me": my_positive, "them": their_positive},
        "negative_emotion_count": {"me": my_negative, "them": their_negative},
        "positive_emotion_ratio": {"me": my_pos_ratio, "them": their_pos_ratio},
        "revoke_count": {"me": my_revoke, "them": their_revoke},
    }


def compute_scores(stats):
    """计算主动指数、被爱指数、冷淡指数（0-100）"""
    basic = stats["basic"]
    initiative = stats["initiative"]
    reply = stats["reply_speed"]
    bombing = stats["bombing"]
    unanswered = stats["unanswered"]
    goodnight = stats["goodnight"]
    cold = stats["cold_response"]

    total = basic["total_messages"]
    my_total = basic["my_messages"]
    their_total = basic["their_messages"]

    # === 主动指数 ===
    simp_score = 0.0

    # 消息占比（你发超过60%加分）
    my_ratio = my_total / total if total > 0 else 0.5
    simp_score += 20 * min(my_ratio / 0.7, 1.0)

    # 主动发起占比
    total_starts = initiative["my_starts"] + initiative["their_starts"]
    my_start_ratio = initiative["my_starts"] / total_starts if total_starts > 0 else 0.5
    simp_score += 25 * min(my_start_ratio / 0.75, 1.0)

    # 回复速度差（你比对方快多少）
    my_speed = reply["my_avg_seconds"]
    their_speed = reply["their_avg_seconds"]
    if their_speed > 0 and my_speed > 0:
        speed_ratio = their_speed / my_speed
        simp_score += 20 * min(speed_ratio / 10, 1.0)
    elif my_speed > 0 and their_speed == 0:
        simp_score += 20

    # 连续轰炸
    bomb_ratio = bombing["my_bomb_count"] / max(total_starts, 1)
    simp_score += 15 * min(bomb_ratio / 0.3, 1.0)

    # 晚安主动率
    total_goodnight = goodnight["my_goodnight"] + goodnight["their_goodnight"]
    if total_goodnight > 0:
        my_gn_ratio = goodnight["my_goodnight"] / total_goodnight
        simp_score += 10 * min(my_gn_ratio / 0.8, 1.0)

    # 已读不回忍受
    if unanswered["my_unanswered"] > 5:
        simp_score += 10

    simp_index = min(int(simp_score), 100)

    # === 被爱指数 ===
    loved_score = 0.0

    # 对方消息占比
    their_ratio = their_total / total if total > 0 else 0.5
    loved_score += 20 * min(their_ratio / 0.5, 1.0)

    # 对方主动发起
    loved_score += 25 * min(initiative["their_starts"] / max(total_starts * 0.4, 1), 1.0)

    # 对方回复速度快
    if their_speed > 0 and my_speed > 0:
        their_responsiveness = my_speed / their_speed
        loved_score += 20 * min(their_responsiveness / 3, 1.0)

    # 对方消息长（用心）
    my_len = stats["message_length"]["my_avg_chars"]
    their_len = stats["message_length"]["their_avg_chars"]
    if my_len > 0:
        len_ratio = their_len / my_len
        loved_score += 15 * min(len_ratio / 1.0, 1.0)

    # 对方说晚安主动
    if total_goodnight > 0:
        their_gn_ratio = goodnight["their_goodnight"] / total_goodnight
        loved_score += 10 * min(their_gn_ratio / 0.6, 1.0)

    # 对方不敷衍（冷淡少）
    their_cold_ratio = cold["their_cold_count"] / max(their_total, 1)
    loved_score += 10 * (1 - min(their_cold_ratio / 0.3, 1.0))

    loved_index = min(int(loved_score), 100)

    # === 冷淡指数（对方对你的冷淡程度）===
    cold_score = 0.0
    cold_ratio = cold["their_cold_count"] / max(their_total, 1)
    cold_score += 40 * min(cold_ratio / 0.3, 1.0)
    if their_speed > 0 and my_speed > 0 and their_speed > my_speed * 5:
        cold_score += 30
    if their_total > 0 and my_total / their_total > 2:
        cold_score += 30
    cold_index = min(int(cold_score), 100)

    return {
        "simp_index": simp_index,
        "loved_index": loved_index,
        "cold_index": cold_index,
    }


def fmt_duration(seconds):
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{int(seconds / 60)} 分钟"
    return f"{seconds / 3600:.1f} 小时"


# ---------------------------------------------------------------- 情感计算层

AFFECT_MIN_MESSAGES = 50

# 只用作「还没导入 embed_hf 时」的占位标签。真正的模型标识一律取自
# `embed_hf.DEFAULT_MODEL`（单一来源），不要在这里另立一份——曾经把显示标签
# 当成仓库 id 传进 `cached()`，导致它在磁盘上找一个不存在的目录、一路判成
# 「无缓存」。那个 bug 只在 `--no-download` 路径上暴露；环境里有缓存的机器
# 永远发现不了它。
AFFECT_FALLBACK_MODEL = "onnx-community/bge-small-zh-v1.5-ONNX"


def build_affect(messages, args):
    """构建 stats.json 的 `affect` 段。定义见 `references/affect-schema.md`。

    **这个 key 永远存在**，不可用时也只是 `status != "ok"`——消费方判 status，
    不判 key 在不在。

    **任何降级路径都返回一个 dict，绝不抛异常、绝不 sys.exit。** 情感层是可选增强，
    不能因为环境缺件就中断主分析；所有路径都必须 exit 0。

    status 语义：`disabled` = 一开始就没打算算；`degraded` = 试了但没成。
    区别只在诊断价值——`degraded` 说明环境本来是够的，值得看一眼。
    """
    import affect_schema

    # ⚠️ 本函数统一返回 `(affect 段, 逐条坐标或 None)`。**所有** return 都必须
    # 走这两个包装——曾经只改了最后那个 return，于是 `--affect off` 这条路径返回
    # 裸 dict，调用处解包时炸成 "too many values to unpack"。那条路径在改动之后
    # 再没被测过，一直到手动跑一次才暴露。
    def shell(reason, status="disabled", model=AFFECT_FALLBACK_MODEL):
        return affect_schema.shell(status=status, reason=reason, model=model), None

    if args.affect == "off":
        return shell("未启用（--affect off）")

    try:
        import embed_hf
    except Exception as exc:  # 连模块都进不来（缺 numpy 等）
        return shell(f"无法导入 embed_hf：{type(exc).__name__}: {exc}")

    # 到这里起，模型标识只有一个来源
    model_id = args.affect_model or embed_hf.DEFAULT_MODEL

    def stop(reason, status="disabled"):
        return shell(reason, status=status, model=model_id)

    text_msgs = [m for m in messages
                 if m.get("type") == "text" and m.get("content", "").strip()]
    if len(text_msgs) < AFFECT_MIN_MESSAGES:
        return stop(f"文字消息不足 {AFFECT_MIN_MESSAGES} 条（实际 {len(text_msgs)}）")

    ok, why = embed_hf.available()
    if not ok:
        return stop(why)

    cache = args.model_cache or embed_hf.DEFAULT_CACHE_DIR
    if not embed_hf.cached(model_id, cache_dir=cache) and args.no_download:
        return stop(f"本地无模型缓存（{cache}），且指定了 --no-download")

    try:
        session, tokenizer = embed_hf.load(
            model_id, cache_dir=cache, endpoint=args.hf_endpoint,
            allow_download=not args.no_download)
        import affect
        import semantic_features

        def embed_fn(texts):
            return embed_hf._embed(texts, session, tokenizer)

        result = affect.analyze(messages, embed_fn, model=model_id, return_points=True)
        if result.get("status") == "ok":
            # 语义层复用同一个 session，不重复加载模型
            try:
                result["semantics"] = semantic_features.analyze(messages, embed_fn)
            except Exception as exc:
                result["semantics"] = {
                    "degraded_reason": f"{type(exc).__name__}: {exc}"}
        # 逐条坐标不进 stats.json（1105 条太大），取出来由 main() 写旁车文件
        points = result.pop("_points", None)
        return result, points
    except Exception as exc:
        # 注意 `stop()` 自己已经返回 `(段, None)` 了——这里**不要**再补一个 `, None`，
        # 否则会变成嵌套元组，写进 JSON 就是 "[{...}, null]" 这种列表。
        return stop(f"模型加载失败：{type(exc).__name__}: {exc}", status="degraded")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--affect", choices=("off", "auto"), default="off",
        help="情感计算层（默认 off：零依赖、零联网）。"
             "auto = 尝试启用；任何环节不可用则降级，且始终 exit 0。"
             "降级表见 references/affect-schema.md。")
    parser.add_argument("--affect-model", default=None,
                        help=f"覆盖默认句向量模型（默认 {AFFECT_FALLBACK_MODEL}）")
    parser.add_argument("--model-cache", default=None,
                        help="模型缓存目录（默认 vendor/hf_cache，已 gitignore）")
    parser.add_argument("--no-download", action="store_true",
                        help="只用本地缓存的模型，绝不联网")
    parser.add_argument("--hf-endpoint", default=None,
                        help="自定义模型下载源。默认官方 HF，不可达时自动回退 "
                             "hf-mirror.com。⚠️ 非官方镜像的权重可信度由使用者自负。")
    args = parser.parse_args()

    data = load_messages(args.input)
    messages = data.get("messages", [])
    contact_display = data.get("contact_display", "对方")

    if not messages:
        print(json.dumps({"error": "没有消息"}))
        sys.exit(1)

    # 只分析 me/them 的消息
    valid = [m for m in messages if m["sender"] in ("me", "them")]
    text_msgs = filter_text_messages(valid)

    my_msgs = [m for m in valid if m["sender"] == "me"]
    their_msgs = [m for m in valid if m["sender"] == "them"]
    total = len(valid)

    # 时间范围
    timestamps = [m["timestamp"] for m in valid]
    date_start = datetime.fromtimestamp(min(timestamps)).strftime("%Y-%m-%d")
    date_end = datetime.fromtimestamp(max(timestamps)).strftime("%Y-%m-%d")
    total_days = max((max(timestamps) - min(timestamps)) // 86400, 1)

    # 消息长度（只算文字）
    my_text = [m for m in text_msgs if m["sender"] == "me"]
    their_text = [m for m in text_msgs if m["sender"] == "them"]
    my_avg_len = sum(len(m["content"]) for m in my_text) / len(my_text) if my_text else 0
    their_avg_len = sum(len(m["content"]) for m in their_text) / len(their_text) if their_text else 0

    # 对话发起统计
    conversations = detect_conversations(valid)
    my_starts = sum(1 for c in conversations if c[0]["sender"] == "me")
    their_starts = sum(1 for c in conversations if c[0]["sender"] == "them")

    # 回复速度
    avg_my_reply, avg_their_reply = analyze_reply_times(valid)

    # 轰炸
    bombing = detect_bombing(valid)

    # 冷淡
    cold = detect_cold_replies(text_msgs)

    # 未回复
    unanswered = detect_unanswered(valid)

    # 晚安
    goodnight = detect_goodnight(text_msgs)

    # 语言学特征
    linguistics = analyze_linguistics(text_msgs, valid)

    # 轮次级回应的字数（零依赖，与 affect 层无关）
    turn_response = detect_turn_response(text_msgs)

    # 活跃时段
    active_hours = defaultdict(int)
    for m in valid:
        hour = datetime.fromtimestamp(m["timestamp"]).hour
        active_hours[str(hour)] += 1

    # 每日趋势（最近90天）
    daily_counts = defaultdict(int)
    for m in valid:
        day = datetime.fromtimestamp(m["timestamp"]).strftime("%Y-%m-%d")
        daily_counts[day] += 1
    daily_trend = [{"date": k, "count": v}
                   for k, v in sorted(daily_counts.items())[-90:]]

    # 消息类型统计
    type_counts = defaultdict(lambda: {"me": 0, "them": 0})
    for m in valid:
        type_counts[m["type"]][m["sender"]] += 1

    # ---- C 模块的量化条件（risk-signals.md 双阈值里「量化」的那一半）
    # 只增不改：既有指标一律不动。
    # 每项都带来源 / 可用性。宁可显式标 unavailable，也不要算一个和人工结论冲突
    # 的数 ——「撤回」那项就是这么坑的：代码按 local_type 数，结构性恒为 0，
    # 而人工口径是「他 7 / 她 3」。让规则层能看出「这个数不能用」。
    # 消息基数也是一个口径：`valid` 只含 sender ∈ {me, them}（1531 条），
    # 全部消息含 system/unknown（1541 条）。删掉系统消息会让会话切得更碎 ——
    # 实测 h6 在 valid 上是 他40/她7，在 all 上是 他39/她7。
    # 人工结论用的是 all。两套都存，别挑一个装作只有一个。
    _bases = {"valid": valid, "all": messages}
    c_session = {}
    for _name, _gap in SESSION_GAPS.items():
        _per_basis = {}
        for _bname, _msgs in _bases.items():
            _s = session_starts(_msgs, _gap)
            _per_basis[_bname] = {
                "me_starts": _s["me"],
                "them_starts": _s["them"],
                "total_sessions": _s["total"],
                "source": "会话切分 gap=%ds，消息基数=%s" % (_gap, _bname),
            }
        # 顶层键保留 all 口径（与人工结论一致），valid 放旁边备查。
        c_session[_name] = dict(_per_basis["all"])
        c_session[_name]["by_basis"] = _per_basis

    _ts = sorted(m["timestamp"] for m in valid if m.get("timestamp"))
    _cold_war = sum(1 for i in range(1, len(_ts)) if _ts[i] - _ts[i - 1] > COLD_WAR_GAP)
    _last30 = (_ts[-1] - 30 * 86400) if _ts else 0
    _s30 = session_starts([m for m in valid if (m.get("timestamp") or 0) >= _last30],
                          SESSION_GAPS["h6"])

    c_metrics = {
        "session_gaps": c_session,
        "cold_war_count": {
            "value": _cold_war,
            "source": "相邻消息间隔 > %ds" % COLD_WAR_GAP,
        },
        "starts_30d": {
            "me": _s30["me"],
            "them": _s30["them"],
            "source": "近 30 天，会话切分 gap=h6",
        },
        # 以下三项 stats.json 拿不到 —— 显式标 unavailable，
        # 让规则层宁可不出结论，也不要拿一个 0 当真值。
        "revoke_by_sender": {
            "available": False,
            "reason": "linguistic.revoke_count 按 type=='revoke' 统计，而该类型早已"
                      "不产生、结构性恒为 0；真实撤回要从 sysmsg 解析",
        },
        "future_statements": {
            "available": False,
            "reason": "conditional_count 是条件句，不是未来指向句",
        },
        "gottman_by_phase": {
            "available": False,
            "reason": "正负比只有全程总值，没有分期",
        },
    }

    stats = {
        "contact": contact_display,
        "basic": {
            "total_messages": total,
            "my_messages": len(my_msgs),
            "their_messages": len(their_msgs),
            "my_ratio": round(len(my_msgs) / total, 3) if total > 0 else 0,
            "their_ratio": round(len(their_msgs) / total, 3) if total > 0 else 0,
            "date_range": [date_start, date_end],
            "total_days": total_days,
            "avg_daily": round(total / total_days, 1),
        },
        "initiative": {
            "my_starts": my_starts,
            "their_starts": their_starts,
            "my_start_ratio": round(my_starts / max(my_starts + their_starts, 1), 3),
        },
        # 只增不改：上面的 initiative 保持原口径（h3），c_metrics 里另存三套。
        "c_metrics": c_metrics,
        "reply_speed": {
            "my_avg_seconds": round(avg_my_reply),
            "their_avg_seconds": round(avg_their_reply),
            "my_avg_human": fmt_duration(avg_my_reply),
            "their_avg_human": fmt_duration(avg_their_reply),
            "speed_ratio": round(avg_their_reply / max(avg_my_reply, 1), 1),
        },
        "message_length": {
            "my_avg_chars": round(my_avg_len, 1),
            "their_avg_chars": round(their_avg_len, 1),
        },
        "bombing": bombing,
        "cold_response": cold,
        "unanswered": unanswered,
        "goodnight": goodnight,
        "active_hours": dict(active_hours),
        "daily_trend": daily_trend,
        "message_types": {k: dict(v) for k, v in type_counts.items()},
        "linguistic": linguistics,
        "turn_response": turn_response,
    }

    stats["scores"] = compute_scores(stats)

    # 修复发起统计（>24h 沉默后谁先说话）
    repair_gap = 24 * 3600
    me_repair = 0
    them_repair = 0
    prev_ts = valid[0]["timestamp"] if valid else 0
    prev_sender = None
    for m in valid:
        gap = m["timestamp"] - prev_ts
        if gap >= repair_gap and prev_sender is not None:
            if m["sender"] == "me":
                me_repair += 1
            elif m["sender"] == "them":
                them_repair += 1
        prev_ts = m["timestamp"]
        prev_sender = m["sender"]
    stats["repair"] = {
        "me_repair_count": me_repair,
        "them_repair_count": them_repair,
    }

    # 近 30 天子统计（供 C3/C6 双阈值验证）
    last_ts = max(timestamps) if timestamps else 0
    cutoff_30d = last_ts - 30 * 86400
    valid_30d = [m for m in valid if m["timestamp"] >= cutoff_30d]
    if valid_30d:
        me_30d = sum(1 for m in valid_30d if m["sender"] == "me")
        them_30d = sum(1 for m in valid_30d if m["sender"] == "them")
        total_30d = len(valid_30d)
        # 对话发起（近 30 天）
        conv_30d = detect_conversations(valid_30d)
        me_starts_30d = sum(1 for c in conv_30d if c[0]["sender"] == "me")
        them_starts_30d = sum(1 for c in conv_30d if c[0]["sender"] == "them")
        # 修复发起（近 30 天）
        me_repair_30d = 0
        them_repair_30d = 0
        prev_ts_30d = valid_30d[0]["timestamp"]
        for m in valid_30d[1:]:
            if m["timestamp"] - prev_ts_30d >= repair_gap:
                if m["sender"] == "me":
                    me_repair_30d += 1
                elif m["sender"] == "them":
                    them_repair_30d += 1
            prev_ts_30d = m["timestamp"]
        # 对方消息密度方差系数（按天聚合）
        import math
        them_daily = defaultdict(int)
        for m in valid_30d:
            if m["sender"] == "them":
                day = datetime.fromtimestamp(m["timestamp"]).strftime("%Y-%m-%d")
                them_daily[day] += 1
        daily_vals = list(them_daily.values())
        if len(daily_vals) >= 2:
            mean_d = sum(daily_vals) / len(daily_vals)
            variance = sum((v - mean_d) ** 2 for v in daily_vals) / len(daily_vals)
            cv = round(math.sqrt(variance) / mean_d, 3) if mean_d > 0 else 0.0
        else:
            cv = 0.0
        stats["recent_30d"] = {
            "me_messages": me_30d,
            "them_messages": them_30d,
            "total_messages": total_30d,
            "me_initiation_ratio": round(me_starts_30d / max(me_starts_30d + them_starts_30d, 1), 3),
            "them_initiation_ratio": round(them_starts_30d / max(me_starts_30d + them_starts_30d, 1), 3),
            "me_repair_count": me_repair_30d,
            "them_repair_count": them_repair_30d,
            "them_message_density_cv": cv,
        }
    else:
        stats["recent_30d"] = None

    # 情感计算层（可选）。始终写入 `affect` 这个 key——不可用时 status="disabled"。
    affect_section, affect_points = build_affect(messages, args)
    stats["affect"] = affect_section

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    # 逐条情绪坐标写旁车文件——`build_chat_history.py` 读它给每行挂坐标，
    # 这样那一步不用自己再加载一次模型（保持零依赖）。
    if affect_points:
        with open(os.path.join(out_dir, "affect_messages.json"), "w", encoding="utf-8") as f:
            json.dump({"version": affect_section.get("version"),
                       "model": affect_section.get("model"),
                       "points": affect_points}, f, ensure_ascii=False)

    s = stats["scores"]
    print(f"[+] 分析完成: 主动={s['simp_index']} 被爱={s['loved_index']} 冷淡={s['cold_index']}", file=sys.stderr)
    print(json.dumps({"status": "ok", "scores": stats["scores"]}))


if __name__ == "__main__":
    main()
