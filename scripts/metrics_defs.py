# -*- coding: utf-8 -*-
"""
metrics_defs.py - 口径常量的唯一事实来源

为什么需要这个文件：这个项目历史上反复发生「同一个指标有两套口径，报告里写的
和代码算的差一个数」——`analysis.json` 的 `meta.corrections` 里为此写了三轮更正
（口径 A / 口径 B 之争）。那些更正是**方法论**，不是某次分析的结论，所以它们
不该住在会被重跑覆盖的 analysis.json 里，而应该固化在这里，随契约版本走。

把口径写进代码而不是写进散文，是为了让「报告里的数」和「代码里的数」不可能再分叉。

## 会话切分（session gap）—— 本项目最重要的口径

同一个问题有三套答案，全都是对的，只是定义不同：

| 口径 | 值 | 用途 |
|---|---|---|
| H3  | 3h  | 既有的 `stats_analyzer.NEW_CONVERSATION_GAP`，用于 initiative/bombing 等既有指标 |
| H6  | 6h  | 分析正文里用的「发起 39:7」 |
| H12 | 12h | 分析正文里用的「冷启动 34:4」 |

2026-10-07 实测，用 `data/messages.json` 的 1541 条复算：
    H3  → 他 49 / 她 8
    H6  → 他 39 / 她 7      ← 与人工结论一致
    H12 → 他 34 / 她 4      ← 与人工结论一致
所以三套**都要存**，每条指标注明用的是哪一套。只存一套就是下一次口径战争的种子。
"""

# ---------------------------------------------------------------- 会话切分

SESSION_GAP_H3 = 3 * 3600
SESSION_GAP_H6 = 6 * 3600
SESSION_GAP_H12 = 12 * 3600

# 会话切分口径的登记表：名字 -> 秒数。分析正文引用时要写明用的是哪一个。
SESSION_GAPS = {
    "h3": SESSION_GAP_H3,
    "h6": SESSION_GAP_H6,
    "h12": SESSION_GAP_H12,
}

# 默认口径：新增指标一律用 H6（与分析正文一致），既有指标维持 H3 不变。
SESSION_GAP_DEFAULT = SESSION_GAP_H6

# 3 小时内回完算「活跃回复」——用于区分「真在聊」和「隔天补一句」。
ACTIVE_REPLY_WINDOW = 5 * 60

# 超过这个间隔算冷战。
COLD_WAR_GAP = 24 * 3600

# ---------------------------------------------------------------- 置信度门槛

# 借自参考项目 cmu/shared/rules.ts 的具名常量。**只用于逐条标注层**
# （live-annotation-schema），不要用于整体结论——本项目的整体结论置信度是
# 「四轴命中数 + 硬上限」，不是标量，用这两个数会抹掉「轴4未定」的差别。
INSUFFICIENT = 0.35
AMBIGUOUS = 0.65

# 「明确且仍有效的边界」概率达到此值 → 关系判定封顶（cmu/affinity.ts 的 min(25, raw)）。
BOUNDARY_CAP = 0.8

# ---------------------------------------------------------------- F5 四轴

# analysis-framework.md:91-106。逐轴问，规则层数命中数。
F5_AXES = ("stopping", "recurrence", "specificity", "persistence")
F5_AXIS_LABELS = {
    "stopping": "停驻性",
    "recurrence": "复现性",
    "specificity": "指向性",
    "persistence": "悬置性",
}
F5_AXIS_CHOICES = ("强", "弱")
# 轴 4（悬置性）多一个「未定」；命中未定 → 强度上限压到 medium（:114）。
F5_PERSISTENCE_UNKNOWN = "未定"

# ---------------------------------------------------------------- evidence_level

# 有序，便于 min() 取最差（照 cmu/affinity.ts:106-110 的「三态取最差」）。
# 注意本项目是**四态**：`low` 在文档里有定义（report-schema.md:78/81），
# 但历史上从未被模型填出来过（模型倾向在 medium / insufficient 之间二选一）。
# 规则层会强制产生 low（F5 命中 1 轴），于是这一格第一次变得可达。
EVIDENCE_LEVELS = ("high", "medium", "low", "insufficient", "none")
LEVEL_ORDER = {name: i for i, name in enumerate(EVIDENCE_LEVELS)}

# F5 命中轴数 -> evidence_level。count 是「强」的轴数。
F5_HITS_TO_LEVEL = {
    3: "high",       # 注：轴 4 未定时会被 cap 压到 medium
    2: "medium",
    1: "low",
    0: "none",
}
# 「待观察」——只能进 observable_signals，不得单独下结论（analysis-framework.md:112）。
F5_OBSERVABLE_LEVELS = ("low",)


def worst(*levels):
    """取最差的 evidence_level。cap 用这个，不要写 if-else 分支。

    写成 min() 而不是分支，是因为将来一定会再加 cap（比如「样本量 <5 → 上限
    medium」），可叠加比可分支重要。
    """
    real = [lv for lv in levels if lv in LEVEL_ORDER]
    if not real:
        return "none"
    return max(real, key=lambda lv: LEVEL_ORDER[lv])


# ---------------------------------------------------------------- 派生量

def sessions(messages, gap):
    """把消息序列按 gap 秒切分成会话。

    与 stats_analyzer.detect_conversations 同构，但 gap 可传参 ——
    那里的模块常量写死成 3h，是造成口径分叉的根源。
    """
    if not messages:
        return []
    ordered = sorted(messages, key=lambda m: m.get("timestamp") or 0)
    conv = [[ordered[0]]]
    for m in ordered[1:]:
        if (m.get("timestamp") or 0) - (conv[-1][-1].get("timestamp") or 0) > gap:
            conv.append([m])
        else:
            conv[-1].append(m)
    return conv


def session_starts(messages, gap):
    """按 gap 口径统计「谁先开口」。返回 {me, them, total}。"""
    conv = sessions(messages, gap)
    me = sum(1 for c in conv if c[0].get("sender") == "me")
    them = sum(1 for c in conv if c[0].get("sender") == "them")
    return {"me": me, "them": them, "total": len(conv)}
