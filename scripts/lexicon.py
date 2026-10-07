"""
lexicon.py - 中文词表与匹配函数的唯一来源

背景
----
这些词表原先分散在 stats_analyzer.py 和 build_chat_history.py 两处，而且两份
内容并不一致：后者多出 7 个关系冲突专用标记，注释却自称「与 stats_analyzer.py
保持一致」。同一个概念两份副本，改一处漏一处只是时间问题。

本模块是纯标准库实现（零第三方依赖），是这些词表的唯一来源。

将来若把词表匹配换成模型，只需要改这里的 count_* / is_* 实现，两个调用方零改动
——这才是把它抽出来的真正价值，不是单纯为了少写几行。

匹配语义
--------
count_* 系列统一是「最长优先 + 不重叠」，而且只数**出现了哪些词**，不数出现次数。
原因：词表里同时存在「累」/「好累」/「心累」和「伤」/「伤心」这类互相包含的词，
朴素的 `sum(1 for w in words if w in text)` 会让一条「好累」同时命中「累」和
「好累」被计 2 次。按长度倒序拼 alternation 后，re 在同一位置优先匹配更长的分支，
findall 又保证不重叠，重复计数自然消失。
"""
import re

# ------------------------------------------------------------------ 敷衍回复

# 必须是「整条消息归一化后恰好等于」的白名单，不能再用长度兜底：中文里长度 ≤2
# 的消息绝大多数是标点、表情、拟声词和正常短回应。用 `len(content) <= 2` 会把
# 「？」「加油」「哈哈哈哈」统统判成冷淡。
#
# 历史教训：旧实现的判定是 `content in COLD_WORDS or len(content) <= 2`，
# 而旧 COLD_WORDS 的 19 个词**全部 ≤2 字**——集合判断被长度分支完全覆盖，
# 从来没有真正生效过，是长度规则在独立决定一切。
DISMISSIVE_REPLIES = {
    "嗯", "哦", "好", "行", "好的", "好吧", "随便", "可以",
    "知道了", "哦哦", "嗯嗯", "行吧", "都行", "你定",
}

# 判断整条消息是否只是敷衍回应时，允许剥掉的尾部语气标点。
# 刻意不含「！」「？」——它们承载语气（「好」和「好！」不是一回事）。
REPLY_TRIM = " \t　。.~～…·"

# ------------------------------------------------------------------ 晚安

GOODNIGHT_WORDS = {"晚安", "good night", "goodnight", "晚安啦", "晚安嗷", "晚安哦"}

# ------------------------------------------------------------------ 语言学

# 模糊词：密度越高 → 越不确定 / 安全感越低
HEDGING_WORDS = ["也许", "可能", "感觉", "好像", "大概", "应该", "似乎", "觉得", "不确定", "说不定"]

# 条件句标记：在设定条件和门槛
CONDITIONAL_MARKERS = ["如果", "要是", "假如", "万一", "要不然", "若是", "倘若"]

# 情绪词：正负比用于推导 Gottman 比值（见 references/report-schema.md）
POSITIVE_EMOTION_WORDS = [
    "开心", "高兴", "快乐", "幸福", "爱", "喜欢", "想你", "好想", "哈哈", "嘻嘻",
    "棒", "好棒", "厉害", "可爱", "美", "帅", "甜", "暖", "温柔", "期待",
    "谢谢", "感谢", "宝贝", "亲爱", "老公", "老婆", "宝宝"
]
NEGATIVE_EMOTION_WORDS = [
    "烦", "累", "难过", "伤心", "痛苦", "委屈", "生气", "愤怒", "失望", "绝望",
    "算了", "无所谓", "随便", "不想", "放弃", "好累", "心累", "难受", "伤", "哭"
]

# ------------------------------------------------------------------ 冲突标记

# 这不是上面的情绪词表。它包含 NEGATIVE_EMOTION_WORDS 全部 20 个词条，另外多出
# 「不合适 / 不想说了 / 随便你 / 冷漠 / 消失 / 已读不回 / 不理」这 7 个关系冲突
# 专用标记，用途也不同：这里是给候选采样窗口打分（找出「高冲突区间」），
# 那里是算情绪比值。两表**不应合并**。
CONFLICT_MARKERS = [
    "烦", "累", "难过", "伤心", "痛苦", "委屈", "生气", "愤怒", "失望", "绝望",
    "算了", "无所谓", "随便", "不想", "放弃", "好累", "心累", "难受", "伤", "哭",
    "不合适", "不想说了", "随便你", "冷漠", "消失", "已读不回", "不理"
]


def _compile(words):
    """把词表编译成「最长优先、不重叠」的匹配式。"""
    ordered = sorted(words, key=len, reverse=True)
    return re.compile("|".join(re.escape(w) for w in ordered))


_HEDGING_RE = _compile(HEDGING_WORDS)
_CONDITIONAL_RE = _compile(CONDITIONAL_MARKERS)
_POSITIVE_RE = _compile(POSITIVE_EMOTION_WORDS)
_NEGATIVE_RE = _compile(NEGATIVE_EMOTION_WORDS)


# ------------------------------------------------------------------ 匹配函数


def dismissive_form(content):
    """整条消息是敷衍回应时返回归一化形式，否则返回 None。

    返回归一化形式（而非布尔）是为了让调用方能直接拿它当聚合用的 key——
    这样「哦哦」和「哦哦~」会归到同一个桶里。
    """
    normalized = content.strip().strip(REPLY_TRIM)
    return normalized if normalized in DISMISSIVE_REPLIES else None


def is_goodnight(content):
    """消息中是否含晚安关键词（大小写不敏感）。"""
    text = content.lower().strip()
    return any(w in text for w in GOODNIGHT_WORDS)


def count_hedging(content):
    return len(set(_HEDGING_RE.findall(content)))


def count_conditional(content):
    return len(set(_CONDITIONAL_RE.findall(content)))


def count_positive(content):
    return len(set(_POSITIVE_RE.findall(content)))


def count_negative(content):
    return len(set(_NEGATIVE_RE.findall(content)))


def count_conflict(content):
    """冲突标记计数——用于 `build_chat_history.py` 给候选窗口打分。

    ⚠️ 刻意保持朴素的 `sum(w in content)` 而不做最长优先去重，与上面的
    count_* 不一致。原因：这个分数只用来在候选窗口之间**排序**，而所有窗口
    用的是同一套打分，统一的双计只是权重差异，不改变相对次序；改成去重会让
    窗口选择静默变化（chat_history.txt 跟着变），那不是这次重构想动的东西。
    已知不一致，记在这里。
    """
    return sum(1 for w in CONFLICT_MARKERS if w in content)
