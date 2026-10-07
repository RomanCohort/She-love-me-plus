"""
affect.py - 情感计算层（可选，依赖 embed_hf 的句向量）

为什么单独做一层
----------------
现有的统计（`stats_analyzer.py` + `lexicon.py`）是**静态 + 单人**的词频统计：
数一个人的情绪词，再和另一个人的数并排放着比。这些量的共同点是——**没有一个
需要两个人同时参与才能算出来**：发起比、回复时延、正负词比，各自独立成立。

而关系恰恰只存在于那个「同时」里。所以词表层能说「你发得多、她回得快」，
说不出「**她的情绪在跟着你走，还是你在跟着她走**」。

情感是连续量，不是词袋
----------------------
轴的定义在 `affect_anchors.py`，方法是 semantic projection
（Grand et al. 2022, *Nature Human Behaviour*）：拿人能读懂的锚句求方向，把句子
投影上去。零新模型、可解释、可审计。分类模型给的是二值标签，在二值标签上算不出
有意义的相关。

时间粒度：为什么不是「按天」
----------------------------
**按天聚合在这类语料上是死的，别再试。** 实测这份数据：182 天里只有 27 天两人
都有消息，滞后相关在 lag=±5 时重叠样本只剩 6–7 个点。在那样的样本上，
「最佳滞后」挑出来的必然是最响的噪声——实测曲线在相邻两天之间就能从 +0.83 翻到
-0.85，而「取 |r| 最大」的规则会稳稳挑中那个 -5 天，输出一句完全虚假的
「她领先 5 天」。

根子是**密度**，不是方法：闲聊语料天生稀疏（一天可能 100 条，也可能 68 天 0 条）。

所以本模块用**对话内轮次**做单位：取相邻的异性回合对（默认 30 分钟内），
粒度落到消息级，可用样本量从 27 个变成 583 个。

同样地，「在日均效价序列上找变点」也不做——要让变点算法跑起来就得在 68 天的
空洞上插值，而插值出来的斜坡会淹没真正的转折。这类结构请回原文读，
别指望时间序列算法替你发现。

非独立性：回合对不是独立样本
----------------------------
几百个回合对散在几十段对话里，同一段对话内部的回合彼此相关，而且相邻回合对还
共用一条消息（`(me₁,them₁)` 与 `(them₁,me₂)` 共享 `them₁`）。直接跑普通相关，
标准误会低估、CI 会偏窄——这正是多层模型要处理的问题。

两处补救，都在 `affect_matching` 的输出里：
- **`ci95_cluster`**：按会话整块重抽的 bootstrap CI。**判定显著性看它**，
  `ci95_naive` 只作参考上界。
- **`within` / `between`**：把相关拆成组内与组间两层（这正是 RI-CLPM 的动机）。
  混合的 r 同时含两样东西——「有些对话整体就更正向，两边一起被抬高」和
  「同一段对话里一方偏离本段均值时另一方跟不跟」。前者不是情绪传染，后者才是。
  实测这两层可以把结论整个翻过来：某个方向混合 r=+0.036 看着像正效应，
  拆开却是组内 -0.014（毫无跟随）+ 组间 +0.148。

实测本语料上两种 CI 相差很小（[0.067,0.287] → [0.068,0.267]），说明效应在各段
对话之间是一致的、设计效应小。但别因此省掉这一步——换一份对话更少或更长的
语料，这个校正就会咬人。
"""
import numpy as np

from affect_anchors import (
    VALENCE_POSITIVE, VALENCE_NEGATIVE, AROUSAL_HIGH, AROUSAL_LOW,
)
# 骨架定义在纯标准库模块里——本模块 import numpy，缺依赖时导不进来，
# 而那时恰恰最需要能构造出骨架。
from affect_schema import shell as _shell

DAY = 86400
TURN_WINDOW = 30 * 60  # 超过 30 分钟不算同一轮对话


# ------------------------------------------------------------------ 轴与投影


def _axis(positive, negative, embed_fn):
    """由两侧锚句的均值差得到一条单位方向向量。"""
    pos = embed_fn(positive).mean(axis=0)
    neg = embed_fn(negative).mean(axis=0)
    direction = pos - neg
    return direction / max(np.linalg.norm(direction), 1e-9)


def build_axes(embed_fn):
    """构造 (效价轴, 唤醒轴)。

    `embed_fn(list[str]) -> ndarray[n, dim]`，需返回**已 L2 归一化**的向量。
    """
    return {
        "valence": _axis(VALENCE_POSITIVE, VALENCE_NEGATIVE, embed_fn),
        "arousal": _axis(AROUSAL_HIGH, AROUSAL_LOW, embed_fn),
    }


def project(texts, axes, embed_fn):
    """把文本投影到情感平面，返回 (n, 2) 的 [效价, 唤醒]，落在 [-1, 1]。"""
    emb = embed_fn(texts)
    return np.column_stack([emb @ axes["valence"], emb @ axes["arousal"]])


def axis_diagnostic(axes):
    """检查两条轴是否互相污染。

    唤醒锚句是正负配平的，理论上与效价轴接近正交。|r| 逼近 1 说明配平被破坏
    （多半是往 AROUSAL_* 里加了单极性的句子），此时「唤醒」退化成第二个「效价」，
    所有基于唤醒的结论都不成立。
    """
    r = float(np.dot(axes["valence"], axes["arousal"]))
    if abs(r) > 0.6:
        verdict = "⚠️ 轴污染：检查 affect_anchors.py 的唤醒锚句是否正负配平被破坏"
    elif abs(r) > 0.35:
        verdict = "轻微相关，可用但需留意"
    else:
        verdict = "OK，接近正交"
    return {"axis_correlation": round(r, 3), "verdict": verdict}


def orthogonalise(embeddings, axes):
    """把效价轴与唤醒轴的方向从嵌入中减掉，返回残差嵌入（已重新归一化）。

    **凡是要用嵌入余弦衡量「两条消息语义有多近」的地方，都必须先过这一步。**

    理由：continuity 这类指标是嵌入余弦，而**效价本身就是同一批嵌入的线性泛函**。
    两条消息的嵌入越接近，它们的效价投影在**几何上就必然越接近**。于是任何
    「话题延续度调节情绪传递」的结论，都可能是把同一份几何算了两遍，而不是发现了
    情绪传递——这跟「中介变量和结果变量共享同一个文本载体」是同一个病。

    实测（本语料，`Y ~ X + M + X*M`，M = continuity）：
      - 不做正交化：交互项 +0.171 / +0.226，两个方向都「显著」；
      - 做完之后：掉到 +0.087 / +0.148，其中一个方向直接不显著。
    **大约一半是泄漏。**

    注意两条轴本身相关（|r|≈0.13），所以**必须先 Gram-Schmidt 正交化再投影**：
    按 v 再按 a 依次减会把 v 的分量重新引回来（实测残留 0.041；正交化后 <1e-7）。
    0.041 看着不大，但它足以让结果看起来「还剩一半效应」——那一半可能也是泄漏。

    ⚠️ **Gram-Schmidt 只保证数值正交（<1e-7），不保证语义正交。**
    减掉这两条指定轴之后，残差子空间里**仍可能存在其它与情绪相关的方向**——
    情绪强度、极性强度之类。它们与效价轴的关系不是线性的，投影减不掉。

    所以正交化之后残存的那点相关，只能读作：
    **「在减掉两条指定轴之后，残差里还有这么多相关，来源不明」。**
    它**不能**读作「去掉几何泄漏后还剩一点真效应」——那个说法预设了残差是干净的，
    而这一点并没有被验证。

    实测后果：正交化后 我→她 仍剩 `+0.087`（CI 下界 +0.018）。这个数的解释空间
    至少包括三种——真的情绪传递、第三条没被减掉的情绪相关轴、纯噪声——
    在当前样本量（583 回合 / 40 段对话）下**分不开**。
    """
    residual = embeddings.copy()
    for u in orthogonalise_axes(list(axes.values()) if isinstance(axes, dict) else list(axes)):
        residual = residual - np.outer(residual @ u, u)
    return residual / np.maximum(np.linalg.norm(residual, axis=1, keepdims=True), 1e-9)


def orthogonalise_axes(axes):
    """对一组方向向量做 Gram-Schmidt，返回互相正交的单位向量。

    为什么必须做：**任意两条语义轴都不会天然正交。** 实测效价与唤醒 r=-0.13——
    那已经算相当干净了。轴一多会串得更厉害：四条依恋/任务轴不做正交化，得到的
    「焦虑高、回避低」可能只是「这两条轴本身相关」的投影，而不是两个独立事实。

    ⚠️ **顺序有意义。** 先放进来的轴保持原方向，后面的轴被修正成「扣掉前面所有轴
    之后剩下的方向」。所以要用 `axis_correlation_matrix()` 先看污染情况再决定顺序，
    或者把最信任的轴放前面。
    """
    out = []
    for v in axes:
        w = np.asarray(v, dtype=float).copy()
        for u in out:
            w = w - float(w @ u) * u
        n = np.linalg.norm(w)
        if n < 1e-9:
            raise ValueError("轴之间线性相关，正交化后退化为零向量")
        out.append(w / n)
    return out


def axis_correlation_matrix(axes, names=None):
    """N×N 相关矩阵——让每条轴自己暴露它跟别人串得多厉害。

    对角线上永远是 1，**看非对角**：|r| 越大，这两条轴越是在测同一件事。
    超过 0.6 的那一对本质上是一条轴，不该当成两个独立指标报出去。

    （依恋四型里最可疑的是恐惧型 vs 回避型——它们的锚句在措辞上高度重合。）
    """
    mat = np.vstack([np.asarray(a, dtype=float) for a in axes])
    C = mat @ mat.T
    off = [abs(float(C[i, j])) for i in range(len(axes)) for j in range(len(axes)) if i != j]
    return {
        "names": list(names) if names else [f"axis{i}" for i in range(len(axes))],
        "matrix": [[round(float(x), 3) for x in row] for row in C],
        "max_offdiag": round(max(off), 3) if off else 0.0,
    }


# ------------------------------------------------------------------ 显著性


def _corr(x, y):
    """皮尔逊相关；样本不足或无波动时返回 None。"""
    if len(x) < 5 or np.std(x) < 1e-9 or np.std(y) < 1e-9:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _pearson(x, y):
    """皮尔逊相关；返回 (r, n)。"""
    return _corr(x, y), len(x)


def _fisher_ci(r, n, z=1.96):
    """相关系数的 Fisher z 置信区间。

    ⚠️ 这假设样本**独立**。回合对不满足——见 `_cluster_bootstrap_ci`。
    只把它当参考上界，判定显著性要看聚类版本。
    """
    if r is None or n < 4:
        return [None, None]
    zr = np.arctanh(r)
    se = 1.0 / np.sqrt(n - 3)
    return [round(float(np.tanh(zr - z * se)), 3), round(float(np.tanh(zr + z * se)), 3)]


def _conv_ids(messages, gap=3 * 3600):
    """按 3 小时空档切会话，返回每条消息的会话号。"""
    ids = np.zeros(len(messages), dtype=int)
    c = 0
    for i in range(1, len(messages)):
        if messages[i]["timestamp"] - messages[i - 1]["timestamp"] > gap:
            c += 1
        ids[i] = c
    return ids


def _cluster_bootstrap_ci(x, y, clusters, n_boot=4000, seed=0):
    """按会话整块重抽的 bootstrap 置信区间。

    回合对**不是独立样本**：同一段对话里的回合彼此相关，而且相邻回合对还共用
    一条消息（`(me₁,them₁)` 与 `(them₁,me₂)` 共享 `them₁`）。朴素 Fisher CI
    因此偏窄。按会话整块重抽，能把这份相关性放回区间宽度里。

    实测这份语料上两者几乎相同（[0.067,0.287] → [0.068,0.267]）——说明效应在
    各段对话之间是一致的，设计效应很小。但**不能因此省掉这一步**：换成对话更少
    或更长的语料，这个校正就会咬人。
    """
    uniq = np.unique(clusters)
    if len(uniq) < 5:
        return [None, None]
    idxs = {k: np.where(clusters == k)[0] for k in uniq}
    rng = np.random.default_rng(seed)
    rs = []
    for _ in range(n_boot):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([idxs[k] for k in pick])
        r = _corr(x[idx], y[idx])
        if r is not None:
            rs.append(r)
    if not rs:
        return [None, None]
    lo, hi = np.percentile(rs, [2.5, 97.5])
    return [round(float(lo), 3), round(float(hi), 3)]


def _within_between(x, y, clusters, min_per_cluster=3):
    """把相关拆成「组内」与「组间」两层（RI-CLPM 的核心动机）。

    混合在一起的 r 同时含两种东西：
      - **组间**：有些对话整体就更正向，两边都被抬高——这**不是**情绪传染；
      - **组内**：同一段对话里，一方比本段平均更正向时，另一方是否也跟着偏正向。
    真正想问的是后者。落在两层的比例不同，解释完全不同。
    """
    xw, yw = np.zeros_like(x), np.zeros_like(y)
    keep = np.zeros(len(x), dtype=bool)
    xb, yb = [], []
    for k in np.unique(clusters):
        m = clusters == k
        if m.sum() < min_per_cluster:
            continue
        xw[m] = x[m] - x[m].mean()
        yw[m] = y[m] - y[m].mean()
        keep[m] = True
        xb.append(x[m].mean())
        yb.append(y[m].mean())
    return {
        "within": {"r": _round(_corr(xw[keep], yw[keep])), "n": int(keep.sum())},
        "between": {"r": _round(_corr(np.array(xb), np.array(yb))),
                    "n_clusters": len(xb)},
        "interpretation": {
            "within": "组内：同一段对话内，一方偏离本段均值时另一方跟不跟。"
                      "**只有这一项可以解释为「回合级跟随」。**",
            "between": "组间：不同对话之间整体效价的相关。**不得解释为情绪传递。**"
                       "它反映的是对话整体氛围的差异（有些对话两边都更正向），"
                       "把两边一起抬高，其中不含任何一方的跟随。",
            "warning": "只报一个数时必须报 within。between 的数值通常更大"
                       "（实测 0.329 vs 0.136），单独出现极易被读成「跟得更紧」——"
                       "那是误读。",
        },
    }


def _round(v):
    return None if v is None else round(float(v), 3)


# ------------------------------------------------------------------ 轮次配对


def _turns(messages, values, window=TURN_WINDOW):
    """相邻的**异性**回合对：a 说完、b 在下一条回。

    返回 [(sender_a, val_a, val_b, gap_sec, index_b)]。

    注意这里是 `sender == continue`（跳过同人相邻），不是 `!=`。写反了的话
    拿到的会是「自己接自己」的连续两条，算出来的相关毫无意义——而它**不会报错**，
    只会安静地给出一个像模像样的数。
    """
    out = []
    for i in range(1, len(messages)):
        a, b = messages[i - 1], messages[i]
        if a["sender"] == b["sender"]:
            continue
        gap = b["timestamp"] - a["timestamp"]
        if gap <= window:
            out.append((a["sender"], values[i - 1, 0], values[i, 0], gap, i))
    return out


def affect_matching(messages, values, window=TURN_WINDOW, strong=0.10):
    """情绪匹配的**方向性**——这是我方情绪预测对方下一条，还是反过来。

    对每个方向算 r(本方这条的效价, 对方下一条的效价)：
      - r 高：对方跟着本方的情绪走（情绪传染 / 迎合）
      - r 低：对方不回接，按自己的轨道走

    **必须看置信区间，不能只看 r 谁大。** 两份 n≈300 的 r 相差 0.14 时，
    差别还落在噪声里——实测本语料就是这样（z≈1.7，p≈0.09，边缘）。
    """
    turns = _turns(messages, values, window)
    conv = _conv_ids(messages)
    res = {"window_minutes": window // 60, "strong_threshold": strong,
           "total_turns": len(turns), "conversations": int(conv.max() + 1)}

    for who, label in (("me", "me_to_them"), ("them", "them_to_me")):
        sub = [t for t in turns if t[0] == who]
        if not sub:
            res[label] = None
            continue
        x = np.array([t[1] for t in sub])
        y = np.array([t[2] for t in sub])
        cl = np.array([conv[t[4]] for t in sub])
        r = _corr(x, y)

        st = [t for t in sub if abs(t[1]) > strong]
        r_st = _corr(np.array([t[1] for t in st]), np.array([t[2] for t in st])) if len(st) >= 5 else None

        res[label] = {
            "r": _round(r),
            "n": len(x),
            # 朴素 CI 只作参考——它假设样本独立，而回合对不独立
            "ci95_naive": _fisher_ci(r, len(x)),
            # 判定显著性看这个：按会话整块重抽
            "ci95_cluster": _cluster_bootstrap_ci(x, y, cl),
            "n_conversations": int(len(np.unique(cl))),
            "r_strong_only": _round(r_st),
            "n_strong": len(st),
            **_within_between(x, y, cl),
        }
    return res


def carryover(messages, values, window=TURN_WINDOW):
    """自我情绪延续（self-contagion）——同一人连着两条之间效价的相关。

    高 = 情绪起来了下不去；低 = 每条都重新开始。这是「情绪惯性」在消息粒度上的
    等价物，比按天算的自相关样本量大一个量级。

    这里的 `ci95` 是**朴素** Fisher 区间，没有做聚类校正。对**零结果**而言这是
    偏保守的方向：校正只会把区间撑宽，只会让它更不显著。所以「两人都不存在情绪
    延续」这个结论不会因为补上校正而翻掉。（反过来说，这里若跑出显著结果，
    必须先补校正再信。）
    """
    out = {}
    for who in ("me", "them"):
        xs, ys = [], []
        for i in range(1, len(messages)):
            a, b = messages[i - 1], messages[i]
            if a["sender"] != who or b["sender"] != who:
                continue
            if b["timestamp"] - a["timestamp"] <= window:
                xs.append(values[i - 1, 0])
                ys.append(values[i, 0])
        r, n = _pearson(np.array(xs), np.array(ys))
        out[who] = {"r": None if r is None else round(r, 3), "n": n,
                    "ci95": _fisher_ci(r, n)}
    return out


def _daily_valence(text_msgs, values):
    """逐日效价均值，**覆盖从第一天到最后一天的每一个日期**（没消息的天为 null）。

    为什么要把空白日也列出来：报告是折线图，横轴按**索引**均匀排。如果只列
    「有消息的日子」，那 182 天里的 68 天空洞会被压成图上一小步，看着像「一直在
    聊」——**把最该看见的那个断层藏起来**。补上空白日后，那段空洞会占掉大半个
    横轴，一眼就看得见。

    给报告画曲线用。**只放效价值，不放 within_r**：`within_r = 0.136` 是给分析者
    看的，报告读者要的是「这段时间你们俩的情绪怎么走的」，一个相关系数不回答
    那个问题，还容易被误读（见 `within_between.interpretation` 那段）。
    """
    import datetime

    days = {}
    for m, v in zip(text_msgs, values):
        d = datetime.date.fromtimestamp(m["timestamp"])
        days.setdefault(d, {"me": [], "them": []})
        days[d][m["sender"]].append(float(v[0]))
    if not days:
        return []

    out = []
    cur, last = min(days), max(days)
    while cur <= last:
        x = days.get(cur)
        out.append({"date": cur.isoformat(),
                    "me": round(sum(x["me"]) / len(x["me"]), 4) if x and x["me"] else None,
                    "them": round(sum(x["them"]) / len(x["them"]), 4) if x and x["them"] else None})
        cur += datetime.timedelta(days=1)
    return out


def _density(messages):
    """把数据密度摆到明面上——所有「为什么没给时间序列指标」的答案都在这。"""
    days = {}
    for m in messages:
        days.setdefault(int(m["timestamp"] // DAY), set()).add(m["sender"])
    both = sum(1 for s in days.values() if len(s) == 2)
    span = (max(days) - min(days) + 1) if days else 0
    return {
        "span_days": span,
        "days_any_message": len(days),
        "days_both_senders": both,
        "note": "按天做时间序列分析至少需要几十个两方都有数据的连续日；"
                "本项低于该门槛时不输出日粒度指标。",
    }


# ------------------------------------------------------------------ 入口


def shell(status="disabled", reason=None, model=None):
    """本模块的 `shell()` —— 补上锚句数量后转发给 `affect_schema.shell()`。

    真正的骨架定义在 `affect_schema.py`（纯标准库），因为这个模块 import numpy，
    在没装 numpy 的环境里根本导不进来——而那种环境恰恰最需要能构造出骨架。
    """
    return _shell(status=status, reason=reason, model=model,
                  anchors={"valence_pairs": len(VALENCE_POSITIVE),
                           "arousal_pairs": len(AROUSAL_HIGH)})


def analyze(messages, embed_fn, *, window=TURN_WINDOW, strong=0.10, model=None,
            return_points=False):
    """跑完整的情感计算层，返回**冻结的 `affect` 段**（见 `references/affect-schema.md`）。

    任何失败都只降级、不抛出。返回值的 `status` 只可能是 `ok` 或 `degraded`——
    `disabled` 由调用方判定（未启用 / 缺依赖 / 数据太少），走 `shell()`。
    """
    base = shell(status="ok", reason=None, model=model)
    try:
        axes = build_axes(embed_fn)
        diag = axis_diagnostic(axes)

        text_msgs = [m for m in messages
                     if m.get("type") == "text" and m.get("content", "").strip()]
        if len(text_msgs) < 10:
            return {**base, "status": "degraded",
                    "degraded_reason": "文字消息不足 10 条", "axis_diagnostic": diag}

        values = project([m["content"] for m in text_msgs], axes, embed_fn)

        per_speaker = {}
        for who in ("me", "them"):
            v = values[[i for i, m in enumerate(text_msgs) if m["sender"] == who]]
            if len(v) == 0:
                per_speaker[who] = None
                continue
            per_speaker[who] = {
                "messages": int(len(v)),
                "valence_mean": round(float(v[:, 0].mean()), 4),
                "valence_std": round(float(v[:, 0].std()), 4),
                "arousal_mean": round(float(v[:, 1].mean()), 4),
                "arousal_std": round(float(v[:, 1].std()), 4),
            }

        # 把 affect_matching 的嵌套结果拍平成 schema 要求的 within_between
        am = affect_matching(text_msgs, values, window, strong)
        within_between, interpretation = {}, None
        for label in ("me_to_them", "them_to_me"):
            v = am.get(label)
            if not v:
                within_between[label] = None
                continue
            within_between[label] = {
                "within_r": v["within"]["r"],
                "between_r": v["between"]["r"],
                "n": v["n"],
                "n_conversations": v["n_conversations"],
                "ci95_cluster": v["ci95_cluster"],
            }
            if interpretation is None:
                interpretation = v.get("interpretation")

        # `_points` 是给调用方写旁车文件用的，**不进 stats.json**（1105 条塞进去太大）。
        # 调用方取走后要 pop 掉。不取的话它就是个普通键，不影响 schema 契约。
        points = ([{"local_id": m["local_id"], "v": round(float(v[0]), 4),
                    "a": round(float(v[1]), 4)}
                   for m, v in zip(text_msgs, values)] if return_points else None)

        out = {
            **base,
            "degraded_reason": None,
            "axis_diagnostic": diag,
            "coverage": {"messages": len(text_msgs), **_density(text_msgs)},
            "per_speaker": per_speaker,
            "within_between": within_between,
            "interpretation": interpretation,
            # within/between 只用效价投影，不碰嵌入余弦，所以这一路没有泄漏通道，
            # 无需正交化。用 `reason` 写明，免得消费方以为是漏做了。
            "orthogonalisation": {
                "applied": False,
                "residual_norm": None,
                "reason": "within_between 只用效价投影，不涉及嵌入余弦，无泄漏通道",
            },
            "carryover": carryover(text_msgs, values, window),
            # 逐日效价，给报告画曲线。刻意**不含** within_r / between_r——
            # 那些是给分析者的，报告读者拿到只会误读。
            "daily_valence": _daily_valence(text_msgs, values),
        }
        if points is not None:
            out["_points"] = points
        return out
    except Exception as exc:
        return {**base, "status": "degraded",
                "degraded_reason": f"{type(exc).__name__}: {exc}"}
