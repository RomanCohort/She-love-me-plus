"""
affect_schema.py - `affect` 段的 schema 常量与骨架（纯标准库，零第三方依赖）

为什么单独成一个模块
--------------------
骨架必须在**任何环境下**都能构造出来，包括没装 numpy / onnxruntime 的环境。
否则「affect 不可用时也要有 key、只是 status=disabled」这条契约，会刚好在
最需要它的那种情况下失效——而那种情况正是它存在的理由。

所以这里**一个第三方 import 都不能有**，也不要去 import `affect`。

字段定义与兼容性规则见
`.agents/skills/she-love-me/references/affect-schema.md`。
"""

# 1.0.0 -> 1.1.0：新增可选子对象 `semantics`（语义特征层）。只增不改，消费方
# 应当忽略不认识的键，所以旧消费方不受影响。
# 1.1.0 -> 1.2.0：新增 `daily_valence`（逐日效价，给报告画曲线）。
SCHEMA_VERSION = "1.2.0"

# 已知在这类数据上走不通的分析路径。报告生成器可直接引用，不必硬编码。
# 每一条的详细死因见 `mediation.py` 顶部与 affect-schema.md 的对应小节。
NEGATIVE_RESULTS = [
    "daily_timeseries",
    "semantic_nearest_neighbour",
    "mediation",
    "moderation",
    # 原 Phase 4b。删掉的理由写进 references/affect-schema.md 与 affect-limits.md，
    # 核心是：讽刺是语用现象（跟「敷衍」同类），且它没有可验证路径。
    "sarcasm_recall",
]


def shell(status="disabled", reason=None, model=None, anchors=None):
    """`affect` 段的骨架。

    **这个 key 永远存在。** 不可用时也只是 `status != "ok"`，不会是缺键——
    消费方判 `status`，不判 key 在不在：

        affect = stats.get("affect", {})
        if affect.get("status") == "ok":
            ...   # 用
        else:
            ...   # 跳过，或显示 affect.get("reason")

    `status` 三态：`ok` / `degraded`（试了但没成）/ `disabled`（一开始就没打算算）。
    `status != "ok"` 时，除 version / status / reason / model / anchors /
    negative_results 外的字段一律为 `None`——消费方不要假设它们在。
    """
    return {
        "version": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "model": model,
        "anchors": anchors,
        "per_speaker": None,
        "within_between": None,
        "interpretation": None,
        "orthogonalisation": None,
        "axis_diagnostic": None,
        "coverage": None,
        "carryover": None,
        # 这两项只在 status == "ok" 时才有值，但**键必须一直在**——
        # 骨架的字段集合要跟 ok 路径完全一致，否则消费方在 disabled 时
        # 拿到的是「缺键」而不是 null，`affect.get(...)` 与 `affect[...]`
        # 两种写法会走出不同结果。
        "daily_valence": None,
        "semantics": None,
        "negative_results": list(NEGATIVE_RESULTS),
        "degraded_reason": None,
    }
