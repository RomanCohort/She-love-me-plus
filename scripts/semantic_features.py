"""
semantic_features.py - 语义特征层（可选，复用 embed_hf 的同一套嵌入）

定位
----
本项目的最终分析是 LLM 做的。**这一层的职责不是下结论，是给 LLM 更好的输入**——
把「嗯/哦/好 出现 47 次」换成「这段时间的情绪坐标、话题主导性、关心表达强度」。

这个门槛比「产出一个站得住的统计结论」低得多，而且它才是这一层真正的用途。
但门槛低不等于可以放松标注：**每项特征下面都写明了它验证到什么程度**，
没验证的直接说没验证，不靠措辞含糊蒙混。

轴的定义
--------
效价/唤醒来自 `affect_anchors.py`（维度情感模型，有实证基础）。
关心轴、依恋两维来自 `semantic_anchors.py`（**面向任务构造的对比轴，没有实证基础**，
效度层级低于效价/唤醒——别把它们当同等可靠的东西用）。

⚠️ **轴必须正交化，且顺序固定。** 实测五条轴正交化前非对角最大 |r| = 0.445，
出现在**效价 ↔ 焦虑**之间——也就是说「焦虑型分数高」有一大块只是「说得比较负面」。
正交化后归零，但正交化的**顺序会影响结果**（换顺序后焦虑轴余弦 0.877，不是可忽略
的量级）。所以下面 `AXIS_ORDER` 是冻结的，改它等于改轴的定义，必须 bump schema 版本。
"""
import numpy as np

import affect
from affect_anchors import (
    VALENCE_POSITIVE, VALENCE_NEGATIVE, AROUSAL_HIGH, AROUSAL_LOW,
)
from semantic_anchors import CARE_POSITIVE, CARE_NEGATIVE, ATTACHMENT_DIMENSIONS

# 冻结的正交化顺序。先放进去的轴保持原方向，后面的被修正成「扣掉前面各轴之后
# 剩下的方向」。效价/唤醒放最前——它们有实证基础，最不该被改。
AXIS_ORDER = ("valence", "arousal", "care", "anxiety", "avoidance")

AXIS_LABELS = {
    "valence": "效价（负↔正）",
    "arousal": "唤醒（低↔高）",
    "care": "关心表达（事务↔关心）",
    "anxiety": "依恋-焦虑（扣掉效价后）",
    "avoidance": "依恋-回避",
}


def build_axes(embed_fn):
    """构造并正交化全部语义轴，返回 `(axes, diagnostic)`。

    `diagnostic` 里两条信息都要看：

    - `before`：正交化前的相关矩阵。**它才是有信息量的那一个**——告诉你哪两条轴
      本来就在测同一件事。效价↔焦虑 -0.445 就是这么看出来的。
    - `after_max_offdiag`：正交化后（按构造必然是 0，只作自检）。

    ⚠️ 正交化之后，`anxiety` 的含义变成「**扣掉效价和唤醒之后剩下的焦虑成分**」。
    报出去时必须这么标注——直接叫「焦虑型」会让读者以为它没扣。
    """
    raw = [
        affect._axis(VALENCE_POSITIVE, VALENCE_NEGATIVE, embed_fn),
        affect._axis(AROUSAL_HIGH, AROUSAL_LOW, embed_fn),
        affect._axis(CARE_POSITIVE, CARE_NEGATIVE, embed_fn),
        affect._axis(ATTACHMENT_DIMENSIONS["anxiety"]["high"],
                     ATTACHMENT_DIMENSIONS["anxiety"]["low"], embed_fn),
        affect._axis(ATTACHMENT_DIMENSIONS["avoidance"]["high"],
                     ATTACHMENT_DIMENSIONS["avoidance"]["low"], embed_fn),
    ]
    before = affect.axis_correlation_matrix(raw, AXIS_ORDER)
    orth = affect.orthogonalise_axes(raw)
    return dict(zip(AXIS_ORDER, orth)), {
        "order": list(AXIS_ORDER),
        "labels": AXIS_LABELS,
        "before_orthogonalisation": before,
        "after_max_offdiag": affect.axis_correlation_matrix(orth, AXIS_ORDER)["max_offdiag"],
        "note": "before 矩阵显示轴间的原始污染；正交化后 anxiety 表示「扣掉效价与唤醒"
                "之后剩余的焦虑成分」，不得直接读作「焦虑型」。",
    }


# ------------------------------------------------------------------ 逐条特征


def project(texts, axes, embed_fn):
    """把文本投影到全部语义轴上，返回 (n, 5)，列序同 `AXIS_ORDER`。"""
    emb = embed_fn(texts)
    return np.column_stack([emb @ axes[k] for k in AXIS_ORDER])


def novelty(messages, embed_fn, k=5):
    """语义新颖度——每条消息相对**前 k 条**的语义距离。

    定义：`1 - mean(cos(e_i, e_{i-1..i-k}))`。分数高 = 引入了新东西。

    ⚠️ **它不能用来替代冷淡词表。** 我原本以为「敷衍 = 没有内容」，所以信息量
    能抓住它——**实测不成立**：

        敷衍组（嗯/嗯嗯/哦/好的）     novelty 均值 0.6177
        短但不敷衍（好呀好呀/加油…） novelty 均值 0.6070
        差 = -0.0107，方向还是反的

    根子：中文短句之间的余弦基线本来就高（实测语料 p50 ≈ 0.40），5 条上文和任何
    一条短回应之间的距离**都差不多**——短句本身不携带多少语义。

    真正的结论是：**敷衍是语用层面的，不是情绪层面、也不是信息层面的。** 它表达的是
    「我不想投入这段对话」——一个**关系信号**。情绪模型测心情，信息量测内容，
    而敷衍测的是「这句话在关系里干什么」。三个空间都不是它待的地方。

    所以这个函数有它自己的用途（找**话题转换点**、标记引入新信息的消息），
    但别拿它去抓敷衍。**冷淡检测就留在 `lexicon.DISMISSIVE_REPLIES` 白名单里**——
    实测命中 11 条、假阳性 0，而纯长度规则命中 106 条、假阳性 95 条（90%）。

    另一个注意点：绝对数值没有意义（受基线 0.40 支配），**只能看排序**。
    """
    if not messages:
        return []
    emb = embed_fn([m["content"] for m in messages])
    out = []
    for i in range(len(messages)):
        lo = max(0, i - k)
        if i == lo:
            out.append(None)          # 没有前文可比
            continue
        sims = emb[lo:i] @ emb[i]
        out.append(round(float(1.0 - sims.mean()), 4))
    return out


def topic_dominance(messages, values, embed_fn, window=3 * 3600):
    """话题主导性——每条消息与其所在对话窗口质心的距离，按人平均。

    ⚠️ **解释要留神，这个指标是双向的。** 距离大既可能是「把话题拉向新方向」，
    也可能是「说跑题了」或「发了个表情包」——这两种情况在数值上同号。
    所以它只能作为**给 LLM 的提示**，配合原文一起看，不能单独下「谁主导」的结论。
    """
    conv = affect._conv_ids(messages)
    emb = embed_fn([m["content"] for m in messages])
    dist = np.full(len(messages), np.nan)
    for c in np.unique(conv):
        idx = np.where(conv == c)[0]
        if len(idx) < 3:
            continue
        centroid = emb[idx].mean(axis=0)
        n = np.linalg.norm(centroid)
        if n < 1e-9:
            continue
        centroid = centroid / n
        dist[idx] = 1.0 - emb[idx] @ centroid

    out = {}
    for who in ("me", "them"):
        d = dist[[i for i, m in enumerate(messages) if m["sender"] == who]]
        d = d[~np.isnan(d)]
        out[who] = {"mean_distance": round(float(d.mean()), 4), "n": int(len(d))} if len(d) else None
    valid = [v["mean_distance"] for v in out.values() if v]
    out["note"] = ("距离大 = 偏离该段对话的语义中心。**可能是主导话题，也可能是跑题**，"
                   "两者在数值上同号，必须回原文分辨。")
    out["gap"] = round(valid[0] - valid[1], 4) if len(valid) == 2 else None
    return out


def care_intensity(messages, axes, embed_fn):
    """关心表达强度——每人消息在「关心轴」上的均值。

    这一项**本身不需要新代码**（就是投影到 care 轴取均值），放在这里是为了让
    「主动指数」的语义配重有个明确的取用点，而不是散落在别处。

    ⚠️ 语义轴只能作为**配重**，不能替代行为特征。一条「在吗？」和一条
    「我路过你上次说的那家店」在行为统计上完全一样（都是 1 条消息），
    但这正是语义层要补的那块——补的是**权重**，不是替代计数。
    """
    val = project([m["content"] for m in messages], axes, embed_fn)
    ci = AXIS_ORDER.index("care")
    out = {}
    for who in ("me", "them"):
        v = val[[i for i, m in enumerate(messages) if m["sender"] == who], ci]
        out[who] = {"mean": round(float(v.mean()), 4), "std": round(float(v.std()), 4),
                    "n": int(len(v))} if len(v) else None
    return out


def axis_exemplars(messages, axes, embed_fn, top_n=8):
    """每条轴上最靠前的若干条消息**原文**（按发送者分开）。

    **这个比均值有用得多，是本层最重要的一项。**

    实测为什么：622 条消息取平均之后，关心轴我 -0.0212 / 她 -0.0306——**差 0.009**，
    LLM 拿着这个数什么也做不了。焦虑轴差 0.004，更没用。真正体现轴向的句子可能只有
    个位数，平均一下就被日常寒暄淹没。

    但「这条轴上最靠前的 8 条是这些**原句**」是 LLM 能直接读、直接判断的东西。
    小模型负责排序筛选，判断交回 LLM——这才是本项目架构该有的分工。

    ⚠️ 极值也是噪声最大的地方（个别句子可能恰好撞上锚句措辞）。所以给的是**原文**，
    不是分数——让 LLM 看到句子本身，而不是让它相信一个 0.42。
    """
    rows = project([m["content"] for m in messages], axes, embed_fn)
    out = {}
    for j, axis in enumerate(AXIS_ORDER):
        per = {}
        for who in ("me", "them"):
            idx = [i for i, m in enumerate(messages) if m["sender"] == who]
            if not idx:
                per[who] = []
                continue
            ranked = sorted(idx, key=lambda i: -rows[i, j])[:top_n]
            per[who] = [{"ts": messages[i]["timestamp"],
                         "content": messages[i]["content"],
                         "score": round(float(rows[i, j]), 4)} for i in ranked]
        out[axis] = {"label": AXIS_LABELS[axis], "top": per}
    out["note"] = ("给的是**原文**而不是分数——极值也是噪声最大的地方，"
                   "让 LLM 看到句子本身再判断，而不是相信一个数字。")
    return out


def analyze(messages, embed_fn, top_n=8):
    """语义层总入口——返回写进 `affect.semantics` 的对象。

    ⚠️ 这一层**不下任何结论**，只提供 LLM 读原文时的辅助坐标。所以里面每一项都
    带了它自己的局限说明，消费方（尤其是 LLM）必须连着局限一起读。
    """
    text = [m for m in messages
            if m.get("type") == "text" and m.get("content", "").strip()]
    if len(text) < 10:
        return {"degraded_reason": "文字消息不足 10 条"}
    axes, diag = build_axes(embed_fn)
    return {
        "axis_diagnostic": diag,
        "care_intensity": care_intensity(text, axes, embed_fn),
        "topic_dominance": topic_dominance(text, None, embed_fn),
        "attachment_hint": attachment_profile(text, axes, embed_fn),
        # 这一项是本层最重要的产出——极值和原文，而不是均值
        "axis_exemplars": axis_exemplars(text, axes, embed_fn, top_n=top_n),
        "note": "本层只供坐标，不下结论。各项局限见其自身 note/disclaimer 字段。",
    }


def attachment_profile(messages, axes, embed_fn):
    """依恋两维画像（焦虑 / 回避），各自扣掉效价与唤醒之后。

    ⚠️⚠️ **这一项没有外部效标，不能当诊断用。**

    效价轴至少有表面效度可以眼看（「恭喜恭喜」落在最正端）；「焦虑轴 0.34」
    我们**没有任何办法知道它是不是真的对应焦虑**。四组锚句能给出的只是
    「这个人的用词离哪一组锚句更近」——离「依恋类型」还差一次验证，而那次做不了。

    还有两条已知局限：

    1. **正交化后含义变了。** 原始焦虑轴与效价轴 r=-0.445，正交化后表示的是
       「扣掉说得正面/负面之后，剩下的那部分焦虑措辞倾向」。
    2. **依恋模式是低频、情境性的。** 622 条消息取平均会被日常寒暄主导——
       真正体现依恋的句子可能只有个位数，平均之后被稀释得几乎看不见。

    用途仅限于：**给 LLM 一个提示方向**。项目自己的铁律是「无证据不诊断」——
    LLM 拿到这两个数之后，仍必须回到原文引用具体句子才能下任何结论。
    """
    val = project([m["content"] for m in messages], axes, embed_fn)
    ai, vi = AXIS_ORDER.index("anxiety"), AXIS_ORDER.index("avoidance")
    out = {"disclaimer": "无外部效标，只能作为提示，不得作为依恋类型诊断；"
                         "任何结论必须回到原文引用具体句子。"}
    for who in ("me", "them"):
        idx = [i for i, m in enumerate(messages) if m["sender"] == who]
        if not idx:
            out[who] = None
            continue
        a = val[idx, ai]
        v = val[idx, vi]
        out[who] = {
            "anxiety_mean": round(float(a.mean()), 4),
            "avoidance_mean": round(float(v.mean()), 4),
            "n": len(idx),
            # 极值比均值更有信息量——依恋信号是低频的，平均值会被寒暄稀释
            "anxiety_p95": round(float(np.percentile(a, 95)), 4),
            "avoidance_p95": round(float(np.percentile(v, 95)), 4),
        }
    return out
