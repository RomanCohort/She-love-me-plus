# -*- coding: utf-8 -*-
"""
analysis_contract.py - analysis.json 的结构契约（纯标准库）

为什么需要它
------------
`data/analysis.json` 现在是模型自由生成的：207 个叶子字段里 114 个是自然语言段，
只有 5 个数字是机器可读的。后果不是理论风险，是**已经发生的静默掉格**：

  · 枚举槽被散文污染 → 渲染器精确匹配失败 → 悄悄落进灰色兜底
    （实测 4 处：relationship_trend / relationship_stage.stage /
     emotional_availability.level / hedging_density）
  · 包装对象被 escape_html 当标量 → dict 的 repr 直接进报告
    （实测 2 处：partner_attachment / advancement_path）
  · 字段重命名**不崩不报错**，只是静默输出空 —— 最危险的一类

这个模块的职责是让上面三类**在生成阶段就暴露**，而不是等读者自己发现。

约定（照抄 affect_schema.py 已确立的那套，不另立第二套）
------------------------------------------------------
  · 键永远存在，消费方判 `status` / `evidence_level`，不判键在不在
  · 只增不改：加字段不 bump 版本，改字段含义才 bump
  · 枚举值以**渲染器**为准（generate_html_report.py 里的字面量列表），
    不是以框架文档为准 —— 文档和代码漂移时，代码才是读者看到的东西

枚举槽的定义必须和渲染器同步。`check_renderer_sync()` 会去读渲染器源码比对，
漂移时报警 —— 这是防「文档说七段、代码认五段」这类问题的唯一手段。
"""

import json
import os
import re

# 1.0.0：初版。把「自由生成的 analysis.json」约束成有枚举、有类型、有锚点的结构。
SCHEMA_VERSION = "1.0.0"

# ---------------------------------------------------------------- 顶层键

TOP_KEYS = (
    "meta", "relationship_type", "relationship_label", "relationship_trend",
    "relationship_stage", "emotional_asymmetry", "personality_portrait",
    "language_patterns", "sternberg", "gottman", "personality",
    "danger_warnings", "strategist", "key_findings", "patriarch_wisdom",
    "simp_description", "love_description", "verdict",
    # 新增模块。按本模块的既有约定「键永远存在」，所以这四键必须存在——
    # **但值可以是 null / []**：渲染器对缺席与显式 null 一视同仁（内部 falsy 守卫），
    # 所以「不存在」用 null 表达，不是靠缺键表达。
    "affinity", "moments", "intent_profile", "reply_grades",
)

# 渲染器为 None 会直接 AttributeError 的容器（.get(k, {}) 的默认值对 key 缺失
# 有效，对显式 null 无效）。这四个必须有守卫，校验器也盯住它们。
CRASH_ON_NULL = ("sternberg", "gottman", "personality", "strategist")

# 直接进算术的槽：被包装成对象或置 null 都会 TypeError。
NUMERIC_SLOTS = (
    ("emotional_asymmetry", "symmetry_score"),
    ("gottman", "positive_negative_ratio"),
    ("sternberg", "passion"),
    ("sternberg", "intimacy"),
    ("sternberg", "commitment"),
    ("affinity", "total"),          # 渲染器插进 style="width:{}%"（先过 safe_int）
)

# 包装对象的合法 evidence_level。**只有这 4 个取值，不得新增第五个**——
# `insufficient` 被 report-schema.md 与本文件同时锚定，加值会让两处一起失效。
EVIDENCE_LEVELS = ("high", "medium", "low", "insufficient")

# ---------------------------------------------------------------- 枚举槽

# 以渲染器为准。generate_html_report.py 里：
#   STAGES  = render_relationship_stage 内的字面量列表
#   TRENDS  = trend_icon 的键
#   LEVELS  = ea_color / risk_color 的键
STAGES = ("初识试探期", "暧昧升温期", "拉锯确认期", "实名化前夜",
          "正式确认期", "关系维护期", "降温衰退期")
TRENDS = ("升温中", "平稳维持", "逐渐降温", "已经凉透")
RISK_LEVELS = ("高危", "中危", "低危")
STRUCT_LEVELS = ("高", "中", "低")
ATTACHMENTS = ("安全型", "焦虑型", "回避型", "恐惧型")

# 点分路径 -> 允许值。值形如「逐渐降温（自 09-25…）」算越界：
# 渲染器认不出它，会静默掉进灰色兜底。散文应当搬进兄弟键。
ENUM_SLOTS = {
    "relationship_trend": TRENDS,
    "relationship_stage.stage": STAGES,
    "relationship_stage.advancement_path": None,   # 见 PAIRED_SLOTS
    "personality.emotional_availability.level": STRUCT_LEVELS,
    "gottman.risk_level": RISK_LEVELS,
    # 语言模式这三项渲染器也当枚举查配色（density_color / future_color），
    # 而当前数据里它们是整句散文 —— 于是永远落灰。
    "language_patterns.hedging_density": STRUCT_LEVELS,
    "language_patterns.conditional_density": STRUCT_LEVELS,
    "language_patterns.future_orientation": ("真实期待", "虚假承诺", "中性"),
}

# 既可以是裸标量、也可以是 {value, evidence_level, reason} 包装的槽。
# 两种形态渲染器现在都能处理；但**同一份文件里形态不一致**说明模型在自由发挥，
# 属于要报出来的信息（不是错误）。
PAIRED_SLOTS = (
    "personality.user_attachment",
    "personality.partner_attachment",
    "relationship_stage.advancement_path",
    "personality_portrait.user.core_fear",
    "personality_portrait.partner.core_fear",
)


# ------------------------------------------------- 新增模块的受控词表
# 语义定义见 references/intent-taxonomy.md 与 references/scoring-rubrics.md。
# 与上面的枚举一样**以渲染器为准**：这些字面量必须能在
# generate_html_report.py 里找到，check_renderer_sync() 会逐个比对。
#
# 词表是「推荐值域，不是硬校验」：越界只报 warn（渲染器会显示原文、走灰色兜底），
# 不像 STAGES 那样是静默掉格的成因。
INTENT_LABELS = (
    "分享近况", "回答问题", "告知信息", "询问信息", "确认理解", "解释说明",
    "学术讨论", "表达观点", "表示认同", "表达异议", "回应收到", "其他意图",
    "延续话题", "转移话题", "玩笑逗趣", "缓和气氛", "结束聊天",
    "倾诉烦恼", "寻求安慰", "寻求认同", "安慰支持", "表达关心", "请求帮助",
    "提供建议", "赞美欣赏", "表达感谢", "道歉修复",
    "寻求关注", "试探好感", "试探邀约", "发出邀约", "暧昧逗弄", "表达在意",
    "婉拒提议", "表达边界", "难以判断",
)
EMOTION_VOCAB = (
    "开心", "疑惑", "愤怒", "难过", "害羞", "关心",
    "调侃", "平静", "不耐烦", "惊讶", "失落", "难判断",
)
AFFINITY_DIMS = ("主动延续", "回应投入", "关心体贴", "自我开放", "亲密表达", "实际行动")
MOMENT_KINDS = (
    "拒绝 / 划清界限", "重新靠近", "邀约", "确认安排", "取消安排", "关心",
    "说出喜好", "敞开心扉", "表达心意", "提问", "澄清",
)
# 实时标注层写英文 kebab（boundary / reopen / …），批量层写中文标签。
# 两者都合法——渲染器做过归一，别把英文那套报成越界。
MOMENT_KIND_ALIASES = (
    "boundary", "reopen", "invitation", "confirmation", "cancellation", "care",
    "preference", "disclosure", "commitment", "question", "correction",
)
GRADES = ("SSS", "SS", "S", "A", "B", "C", "D")
# text 依据的维度只能取五档锚点值——给 72 这种数是假精确（scoring-rubrics.md 1.5）
LEVEL_ANCHORS = (0, 25, 50, 75, 100)


def _get(obj, dotted):
    """按点分路径取值，中途遇到非 dict 返回 (None, 断在哪一段)。"""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None, part
        if part not in cur:
            return None, part
        cur = cur[part]
    return cur, None


def _is_wrapped(v):
    return isinstance(v, dict) and (
        "value" in v or "evidence_level" in v or "reason" in v)


def validate(analysis):
    """校验一份 analysis.json。返回 issue 列表，每项 {level, where, what}。

    level:
      error —— 渲染器会崩，或字段缺失
      warn  —— 渲染器不会崩，但会静默掉格 / 显示错误内容
      info  —— 形态不一致之类，值得知道但不算错
    """
    issues = []

    def add(level, where, what):
        issues.append({"level": level, "where": where, "what": what})

    if not isinstance(analysis, dict):
        add("error", "<root>", "顶层不是 dict，是 %s" % type(analysis).__name__)
        return issues

    # 1. 顶层键
    for k in TOP_KEYS:
        if k not in analysis:
            add("error", k, "缺键 —— 渲染器会走默认值，静默输出空")
    for k in analysis:
        if k not in TOP_KEYS and not k.startswith("_"):
            add("info", k, "不在契约里的顶层键（加字段合法，只是报一声）")

    # 2. 会崩的容器
    for k in CRASH_ON_NULL:
        if analysis.get(k, {}) is None:
            add("error", k, "显式 null —— `.get(k, {})` 的默认值挡不住 null，会 AttributeError")

    # 3. 进算术的数字
    for parent, field in NUMERIC_SLOTS:
        val, _ = _get(analysis, "%s.%s" % (parent, field))
        if val is None:
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            add("error", "%s.%s" % (parent, field),
                "不是数字（%s）—— 会直接进算术，TypeError" % type(val).__name__)

    # 4. 枚举槽
    for path, allowed in ENUM_SLOTS.items():
        if allowed is None:
            continue
        val, missing = _get(analysis, path)
        if val is None:
            continue
        if not isinstance(val, str):
            add("warn", path, "不是字符串（%s）" % type(val).__name__)
        elif val not in allowed:
            add("warn", path,
                "越界：%r 不在 %s 里 —— 渲染器会静默掉进灰色兜底" % (val[:34], list(allowed)))

    # 5. 包装对象形态一致性
    wrapped = []
    for path in PAIRED_SLOTS:
        val, _ = _get(analysis, path)
        if val is None:
            continue
        wrapped.append((path, _is_wrapped(val)))
    if wrapped:
        forms = {w for _, w in wrapped}
        if len(forms) > 1:
            add("info", "*.{value,evidence_level}",
                "同一份文件里包装形态不一致：" +
                "、".join("%s=%s" % (p, "包装" if w else "裸值") for p, w in wrapped))
        for p, w in wrapped:
            if w:
                # 校验包装内部必需有 evidence_level —— 「没结论但有观察」全靠它。
                lv, _ = _get(analysis, p + ".evidence_level")
                if lv not in EVIDENCE_LEVELS:
                    add("warn", p + ".evidence_level",
                        "包装对象缺 evidence_level 或值非法（%r）" % lv)

    # 6. 自由文本的**条数与要素**（不校验逐字 —— 引文多是重构的叙述）
    for path, want, fields in (
        ("key_findings", 5, ("title", "quote", "analysis")),
        ("strategist.stop_doing", 3, ("action", "reason")),
        ("strategist.start_doing", 3, ("action", "timing")),
        ("patriarch_wisdom.advance_tactics", 3, ("title", "logic", "action")),
    ):
        arr, _ = _get(analysis, path)
        if arr is None:
            continue
        if not isinstance(arr, list):
            add("warn", path, "不是数组")
            continue
        if arr and len(arr) != want:
            add("warn", path, "应有 %d 条，实际 %d 条" % (want, len(arr)))
        for i, item in enumerate(arr):
            if not isinstance(item, dict):
                add("warn", "%s[%d]" % (path, i), "不是对象")
                continue
            for f in fields:
                if not item.get(f):
                    add("warn", "%s[%d].%s" % (path, i, f), "必备要素为空")

    # 7. 新增模块：affinity / moments / intent_profile / reply_grades
    #    这些是**变长**列表，不能套上面第 6 节的定长 `want` 循环。
    _check_affinity(analysis, add)
    _check_moments(analysis, add)
    _check_intent_profile(analysis, add)
    _check_reply_grades(analysis, add)

    return issues


# ------------------------------------------------------- 新增模块各自的校验

def _check_affinity(analysis, add):
    """六维好感度。核心是盯住「假精确」与「没数据被写成 0 分」。"""
    aff = analysis.get("affinity")
    if aff is None:
        return
    if not isinstance(aff, dict):
        add("warn", "affinity", "不是对象")
        return

    dims = aff.get("dimensions")
    if dims is None:
        add("error", "affinity.dimensions", "缺键 —— 渲染器按规范顺序遍历六维，缺了整块空白")
        return
    if not isinstance(dims, list):
        add("warn", "affinity.dimensions", "不是数组")
        return

    seen = set()
    for i, d in enumerate(dims):
        where = "affinity.dimensions[%d]" % i
        if not isinstance(d, dict):
            add("warn", where, "不是对象")
            continue
        key = d.get("key")
        if key in AFFINITY_DIMS:
            seen.add(key)
        else:
            add("info", where + ".key",
                "维度名 %r 不在规范集合里 —— 渲染器按 AFFINITY_DIMS 顺序渲染，这一维不会出现" % (key,))

        score, basis, el = d.get("score"), d.get("basis"), d.get("evidence_level")
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                add("error", where + ".score", "不是数字（%s）" % type(score).__name__)
            elif basis == "text" and score not in LEVEL_ANCHORS:
                add("warn", where + ".score",
                    "basis=text 只能取 %s，给 %r 是假精确（scoring-rubrics.md 1.5）"
                    % (list(LEVEL_ANCHORS), score))
        elif el != "insufficient":
            add("warn", where,
                "score 为 null 时 evidence_level 应为 insufficient（当前 %r）—— "
                "「没数据」与「0 分」是两件事" % (el,))

        if el is not None and el not in EVIDENCE_LEVELS:
            add("warn", where + ".evidence_level", "非法取值 %r" % (el,))
        if basis == "quant" and not d.get("derivation"):
            add("warn", where + ".derivation",
                "basis=quant 必须写明字段与口径（scoring-rubrics.md 1.4）")

    total = aff.get("total")
    if total is not None and len(seen) < 4:
        add("warn", "affinity.total",
            "只 %d 个规范维度有分却给了总分 —— 出分要求 ≥4 维（scoring-rubrics.md 1.6）" % len(seen))
    if aff.get("boundary_applied"):
        if not aff.get("boundary_reason"):
            add("warn", "affinity.boundary_reason", "封顶已触发却没写原因 —— 没有原文锚点不得封顶")
        # 封顶是**分析者的义务**：渲染器只显示 total，不会替它压到 25。
        # 写了 boundary_applied 却没压，报告就会显示一个自相矛盾的总分。
        if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 25:
            add("warn", "affinity.total",
                "boundary_applied=true 但 total=%r > 25 —— 封顶要在写盘前压到 25，"
                "渲染器不会替你压（scoring-rubrics.md 1.6）" % (total,))


def _check_moments(analysis, add):
    arr = analysis.get("moments")
    if arr is None:
        return
    if not isinstance(arr, list):
        add("warn", "moments", "不是数组")
        return
    for i, m in enumerate(arr):
        where = "moments[%d]" % i
        if not isinstance(m, dict):
            add("warn", where, "不是对象")
            continue
        kind = m.get("kind")
        if kind not in MOMENT_KINDS and kind not in MOMENT_KIND_ALIASES:
            add("warn", where + ".kind", "越界：%r 不在词表里 —— 渲染器会显示原文但无配色" % (kind,))
        conf = m.get("confidence")
        if isinstance(conf, (int, float)) and not isinstance(conf, bool) and conf < 0.65:
            add("warn", where + ".confidence", "%.2f < 0.65，按约定不该写入（宁可漏不可凑）" % conf)
        for f in ("date", "sender"):
            if not m.get(f):
                add("warn", "%s.%s" % (where, f), "必备要素为空")


def _check_intent_profile(analysis, add):
    ip = analysis.get("intent_profile")
    if ip is None:
        return
    if not isinstance(ip, dict):
        add("warn", "intent_profile", "不是对象")
        return
    for f in ("coverage", "caveat"):
        if not ip.get(f):
            add("warn", "intent_profile." + f,
                "必备要素为空 —— 覆盖率低不是失败，隐瞒覆盖率才是")
    cats = ip.get("categories")
    if cats is None:
        return
    if not isinstance(cats, list):
        add("warn", "intent_profile.categories", "不是数组")
        return
    for i, c in enumerate(cats):
        where = "intent_profile.categories[%d]" % i
        if not isinstance(c, dict):
            add("warn", where, "不是对象")
            continue
        if c.get("intent") not in INTENT_LABELS:
            add("info", where + ".intent", "词表外的意图 %r（合法，只是无配色）" % (c.get("intent"),))
        for f in ("me", "them"):
            v = c.get(f)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                add("error", "%s.%s" % (where, f), "不是数字（%s）" % type(v).__name__)


def _check_reply_grades(analysis, add):
    arr = analysis.get("reply_grades")
    if arr is None:
        return
    if not isinstance(arr, list):
        add("warn", "reply_grades", "不是数组")
        return
    for i, g in enumerate(arr):
        where = "reply_grades[%d]" % i
        if not isinstance(g, dict):
            add("warn", where, "不是对象")
            continue
        if g.get("grade") not in GRADES:
            add("warn", where + ".grade", "越界：%r 不在 SSS–D 里" % (g.get("grade"),))
        if not g.get("context"):
            add("warn", where + ".context",
                "缺语境 —— 同一句「哦」在不同语境里分数差极大，无 context 的评分视为无效")
        for f in ("quote", "reason"):
            if not g.get(f):
                add("warn", "%s.%s" % (where, f), "必备要素为空")
        alts = g.get("alternatives")
        if not isinstance(alts, list) or not alts:
            add("warn", where + ".alternatives",
                "没有替代表达 ——「不回复」也是合法选项，但不能空")


def summarize(issues):
    """把 issue 列表折成一行计数，便于 CLI/日志。"""
    n = {"error": 0, "warn": 0, "info": 0}
    for it in issues:
        n[it["level"]] = n.get(it["level"], 0) + 1
    return n


# ---------------------------------------------------------------- 与渲染器同步

def check_renderer_sync(report_path=None):
    """确认契约里的枚举和渲染器里的字面量一致。

    漂移的后果是「契约说合法、渲染器认不出」——正是那 4 处静默掉格的成因。
    这里直接读渲染器源码比对；读不到就返回空（不阻断调用方）。
    """
    if report_path is None:
        report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "generate_html_report.py")
    try:
        with open(report_path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return []
    drift = []
    for name, allowed in (("STAGES", STAGES), ("TRENDS", TRENDS),
                          ("RISK_LEVELS", RISK_LEVELS),
                          # 新增模块的受控词表。渲染器里这些常量刻意写成模块级
                          # 双引号字面量，就是为了能被这里搜到。
                          ("INTENT_LABELS", INTENT_LABELS),
                          ("EMOTION_VOCAB", EMOTION_VOCAB),
                          ("AFFINITY_DIMS", AFFINITY_DIMS),
                          ("MOMENT_KINDS", MOMENT_KINDS),
                          ("MOMENT_KIND_ALIASES", MOMENT_KIND_ALIASES),
                          ("GRADES", GRADES)):
        for v in allowed:
            if ('"%s"' % v) not in src and ("'%s'" % v) not in src:
                drift.append({"level": "warn", "where": name,
                              "what": "契约里的 %r 在渲染器源码里找不到 —— 两边漂移了" % v})
    return drift


def shell(status="ok", reason=None):
    """judgments 侧的骨架。键永远存在，消费方判 status。

    status 三态：`ok`（拿到了完整判断）/ `partial`（部分块可判、部分证据不足）
    / `insufficient`（整体证据不足，只有观察没有结论）。
    """
    return {
        "version": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "anchors": [],
        "alternatives": [],
        "caps": [],
        "judgments": {},
    }


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="校验 analysis.json 的结构契约")
    parser.add_argument("path", nargs="?", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "analysis.json"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    with open(args.path, encoding="utf-8") as f:
        analysis = json.load(f)
    issues = validate(analysis) + check_renderer_sync()

    if args.json:
        print(json.dumps({"version": SCHEMA_VERSION,
                          "summary": summarize(issues),
                          "issues": issues}, ensure_ascii=False, indent=2))
    else:
        for it in issues:
            mark = {"error": "✗", "warn": "!", "info": "·"}[it["level"]]
            print("%s %-46s %s" % (mark, it["where"], it["what"]))
        s = summarize(issues)
        print("\n%d error / %d warn / %d info" % (s["error"], s["warn"], s["info"]))
    return 1 if any(i["level"] == "error" for i in issues) else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
