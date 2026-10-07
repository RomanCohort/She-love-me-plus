"""
mediation.py - 三消息链中介分析

为什么不是「加协变量」
----------------------
把 M 塞进回归当控制变量，只能回答**「控制住 M 之后 X→Y 还在不在」**。
它回答不了**「X 是不是经由 M 影响到 Y」**——而这两件事完全不同：
X→Y 可能根本不经过 M，M 只是和 X 相关。

要区分「她迎合」和「他不接」，问的正是后一种，所以必须做中介（a 路径 × b 路径），
而不是控制。

时序约束决定了链的形状
----------------------
中介要在时间上成立，M 必须落在 X 之后、Y 之前。如果拿 `X = 我的消息`、
`Y = 她下一条`、`M = 她这条回复的方式`，那 M 和 Y **同处一条消息**——
X→M 有时序，M→Y 没有，间接效应不可识别。

所以用**三消息链**：

    X = A(t₀) 的效价  →  M = B(t₁ 首条回应) 的行为  →  Y = B(t₂ 后续消息) 的效价

并控制 `C = B(t₁) 自己的效价`（自回归）。这条控制不能省：否则 M 很可能只是
B(t₁) 情绪的代理，b 路径测到的还是「情绪延续」，不是「行为中介」。

⚠️ 仍然不是因果识别。三消息链解决了**时序**，但没有解决 M→Y 的未测混杂。
报告里只能说「中介模型一致/不一致」，不能说「证明了机制」。

两条通路是镜像的
----------------
- 我→她：X=我的效价，M=**她**首条回应的行为，Y=她的后续效价
- 她→我：X=她的效价，M=**我**首条回应的行为，Y=我的后续效价

「她迎合」预测前者的间接效应显著；「他不接」预测后者的间接效应里，
a 路径（她的情绪→我的行为）显著而 b 路径弱——即我**行为上变了但情绪没被带动**。

实测结论：**这份数据上识别策略不成立，不是欠 power**
----------------------------------------------------
四个中介 × 两个方向跑下来，没有一条间接效应成立。但真正的问题不是样本量：

**选择偏差是系统性的，不是随机缺失。** 三消息链要求 B 连发 ≥2 条，而「B 连不连发
第二条」本身就是四个中介想测的**投入度**维度。这等于**在选择变量上做了条件**——
被排除的那 295 个回合恰恰是低投入回应，而低投入回应正是「他不接」假设的核心证据
所在。把最该看的回合删掉，再在剩下的高投入回合里找中介，中介当然找不到：
中介变量的一半方差被截断了。

证据：链子样本 `c'=+0.159` 比全样本 within `+0.136` **还大**。这个反超大概率是选择
偏差的方向，不是精度。**所以链子样本的点估计本身有偏，不能拿它支持「效应为真」。**
全样本的 +0.136 是干净的——两者必须分开写，别混着说「点估计很稳」。

**判读规则（比「可能是假阳性」更硬，别再读错）**：`a` 显著而 `b` 为零 → **该中介被
排除**，不是「证据不足」。实测 `她→我 / question`：a=+0.182*、b=-0.052、c'=+0.007。
就算 a 为真，b 是零，中介也不成立——X 影响 M 而 M 不影响 Y，那 M 就不是中介。

想回答机制问题，用下面的 `moderation()`，不要继续在这里挖。

为什么这份数据上中介和调节都做不了——三条路都走死了
--------------------------------------------------
**下面写清楚每一遍死在哪。下一个人可以直接跳过这三遍。**

**第一遍 · 三消息链中介。** 为了让 M 落在 Y 之前，要求 B 连发 ≥2 条。
→ 死因：**在选择变量上做了条件**。「B 连不连发第二条」本身就是被中介的那个
  投入度维度；被排除的 295 个回合恰恰是低投入回应——而低投入回应正是「他不接」
  假设的核心证据所在。在剩下的高投入回合里，中介变量的一半方差已被截断。
  证据：链子样本 `c'=+0.159` **反而大于**全样本 within `+0.136`，这个反超
  就是偏差的方向，不是精度。

**第二遍 · 回到 583 回合做同期中介。** 想把样本翻倍，接受 M 与 Y 同期。
→ 死因：**M 和 Y 共享同一个文本载体**。长度/延迟/追问是那条消息的文本特征，
  Y 是那条消息的情绪投影，两者的相关里有相当一块是**测量共享方差**，不是因果路径。
  文献里的同期中介通常用于 M 和 Y 来自**不同测量工具**（问卷测态度、行为编码测
  行为）的情形。**样本量翻倍不等于识别变强。**

**第三遍 · 调节模型（不建因果链）。** 583 个回合全用，不受选择偏差影响。
→ 死因：**continuity 和效价共享嵌入几何**。两条消息嵌入越近，效价投影在几何上
  必然越近。正交化后约一半效应消失；剩下的那部分又因为 Gram-Schmidt 只保证数值
  正交、不保证语义正交，解释空间过大（见 `affect.orthogonalise`）。

机制排除清单（本数据上，可直接引用）
------------------------------------
- **三消息链（135 + 153 条）**：话题延续度、回应长度、回复延迟、是否追问——
  四条通路的 `a` 与 `b` 路径**均不显著**，无一条构成中介。
- **583 回合调节模型**：回应长度、回复延迟、是否追问、连发条数——交互项 `X*M`
  **全部不显著**；话题延续度正交化后 我→她 剩 `+0.087`（CI `[+0.018,+0.215]`，
  **未过 10 次检验的多重比较校正**），她→我 归零（CI 含 0）。

**结论：机制问题在这份数据上不可回答。** 唯一残存的那条，按判读规则应记为
「无结论」而不是「弱证据」——不是因为不够显著，是因为**残差子空间结构未知，
这个数的解释空间太大，追下去只会产出更多需要杀掉的结果**。

**缺的不是方法，是数据量和数据结构**——583 个回合、40 段对话、182 天里只有 30 天
有消息。继续在这份数据上换模型、调阈值，只会得到更多噪声。
"""
import re

import numpy as np

from affect import _conv_ids, TURN_WINDOW, _corr

QUESTION_WORDS = ("吗", "呢", "?", "？", "怎么", "为什么", "要不要", "好不好", "是不是")


# ------------------------------------------------------------------ 构造链条


def _bursts(messages, window):
    """找出每个「A 说完 → B 开始说」的切换点，并数出 B 连发了多少条。"""
    out = []
    i = 1
    while i < len(messages):
        a, b = messages[i - 1], messages[i]
        if a["sender"] == b["sender"] or (b["timestamp"] - a["timestamp"]) > window:
            i += 1
            continue
        who = b["sender"]
        j = i + 1
        while j < len(messages) and messages[j]["sender"] == who:
            if messages[j]["timestamp"] - messages[j - 1]["timestamp"] > window:
                break
            j += 1
        out.append({"a": i - 1, "b1": i, "b_last": j - 1, "n_burst": j - i, "who": who})
        i = j
    return out


def _topic_continuity(emb_a, emb_b):
    """B 的首条回应与 A 的消息的语义相似度——测「接没接这个话题」。

    ⚠️ 传进来的嵌入**必须先用 `affect.orthogonalise()` 处理过**。

    直接用原始嵌入算余弦，得到的相似度里混着**效价相似度**——因为效价就是同一批
    嵌入的线性泛函，两条消息嵌入越近、效价投影就必然越近。拿这种 continuity 去做
    「话题延续度调节情绪传递」，多半是把同一份几何算了两遍。
    实测：正交化前交互项 +0.171/+0.226，正交化后 +0.087/+0.148，**约一半是泄漏**。
    """
    return float(np.dot(emb_a, emb_b))


def build_chains(messages, values, embeddings, window=TURN_WINDOW, min_burst=2):
    """构造三消息链，返回等长的 X(前一条效价) / M(中介) / Y(后续效价) / C(首条效价)。

    `min_burst=2` 表示 B 至少连发两条，才有 t₁ 和 t₂。
    """
    rows = []
    for s in _bursts(messages, window):
        if s["n_burst"] < min_burst:
            continue
        i0, i1, i2 = s["a"], s["b1"], s["b1"] + 1
        m1 = messages[i1]
        rows.append({
            "X": values[i0, 0],
            "C": values[i1, 0],
            "Y": values[i2, 0],
            "who": s["who"],
            "conv": None,  # 稍后填
            "i1": i1,
            "latency": float(m1["timestamp"] - messages[i0]["timestamp"]),
            "length": float(len(m1["content"])),
            "question": 1.0 if any(w in m1["content"] for w in QUESTION_WORDS) else 0.0,
            "continuity": _topic_continuity(embeddings[i0], embeddings[i1]),
        })
    return rows


MEDIATORS = ("continuity", "length", "latency", "question")


# ------------------------------------------------------------------ 估计


def _ols(y, cols):
    """最小二乘，返回 [截距, 系数...]。"""
    A = np.column_stack([np.ones(len(y))] + list(cols))
    beta, *_ = np.linalg.lstsq(A, y, rcond=None)
    return beta


def _center(v, clusters):
    """会话内中心化 + 全局标准化。

    中心化去掉「有些对话整体就更 X」的组间成分（RI-CLPM 的动机）；
    标准化让 X / M / Y 的系数可跨中介比较。
    """
    w = np.zeros_like(v, dtype=float)
    for k in np.unique(clusters):
        m = clusters == k
        if m.sum() < 3:
            continue
        w[m] = v[m] - v[m].mean()
    sd = w.std()
    return w / sd if sd > 1e-9 else w


def _paths(X, M, Y, C):
    """返回 (a, b, c_prime)。a: X→M；b/c': Y ~ X + M + C。"""
    a = _ols(M, [X])[1]
    beta = _ols(Y, [X, M, C])
    return a, beta[2], beta[1]


def _cluster_boot_paths(X, M, Y, C, clusters, n_boot=4000, seed=0):
    """按会话整块重抽，给出 a / b / c' / a*b 各自的 bootstrap CI。

    回合链同样不独立（同一段对话内相关，且相邻链共用消息），所以必须按会话重抽，
    不能用解析的 Sobel 标准误。

    单看 a*b 的区间不够：间接效应不显著可能是 a 弱、也可能是 b 弱，两者含义完全不同。
    把 a 和 b 的区间一并给出，才看得出是哪一段断的。
    """
    uniq = np.unique(clusters)
    empty = [None, None]
    if len(uniq) < 5:
        return {"a": empty, "b": empty, "c_prime": empty, "indirect_ab": empty}
    idxs = {k: np.where(clusters == k)[0] for k in uniq}
    rng = np.random.default_rng(seed)
    acc = {"a": [], "b": [], "c_prime": [], "indirect_ab": []}
    for _ in range(n_boot):
        idx = np.concatenate([idxs[k] for k in rng.choice(uniq, size=len(uniq), replace=True)])
        try:
            a, b, cp = _paths(X[idx], M[idx], Y[idx], C[idx])
        except np.linalg.LinAlgError:
            continue
        acc["a"].append(a)
        acc["b"].append(b)
        acc["c_prime"].append(cp)
        acc["indirect_ab"].append(a * b)
    f = lambda v: ([round(float(x), 4) for x in np.percentile(v, [2.5, 97.5])]
                   if v else empty)
    return {k: f(v) for k, v in acc.items()}


def _excludes_zero(ci):
    """CI 是否不含 0（两端同号）。"""
    return ci[0] is not None and (ci[0] > 0) == (ci[1] > 0)


def mediation(messages, values, embeddings, *, window=TURN_WINDOW, min_burst=2,
              n_boot=4000):
    """对每条通路、每个中介变量，跑一遍中介模型。

    输出每个组合的：a 路径、b 路径、直接效应 c'、间接效应 a*b 及其**会话聚类
    bootstrap CI**。间接效应的 CI 不含 0 才算中介成立。
    """
    conv = _conv_ids(messages)
    chains = build_chains(messages, values, embeddings, window, min_burst)
    if not chains:
        return {"degraded_reason": "没有可用的三消息链", "chains": 0}

    who = np.array([c["who"] for c in chains])
    cl = np.array([conv[c["i1"]] for c in chains])
    X0 = np.array([c["X"] for c in chains])
    C0 = np.array([c["C"] for c in chains])
    Y0 = np.array([c["Y"] for c in chains])

    out = {"chains": len(chains), "conversations": int(len(np.unique(cl))),
           "min_burst": min_burst, "note":
           "三消息链：X=A(t0) 效价 → M=B(t1 首条回应) 行为 → Y=B(t2) 效价，控制 B(t1) 效价。"
           "间接效应 CI 按会话整块重抽。这不是因果识别，只说明中介模型是否与数据一致。"}
    for label, sel_who in (("me_to_them", "them"), ("them_to_me", "me")):
        sel = who == sel_who
        if sel.sum() < 30:
            out[label] = None
            continue
        # X 永远是「先说话那一方」的效价；中心化按会话做，不按发送者分
        Xc = _center(X0[sel], cl[sel])
        Cc = _center(C0[sel], cl[sel])
        Yc = _center(Y0[sel], cl[sel])
        sub = [chains[i] for i in np.where(sel)[0]]
        sub_cl = cl[sel]

        per_mediator = {}
        for med in MEDIATORS:
            M0 = np.array([c[med] for c in sub], dtype=float)
            if M0.std() < 1e-9:
                continue
            Mc = _center(M0, sub_cl)
            a, b, cp = _paths(Xc, Mc, Yc, Cc)
            ci = _cluster_boot_paths(Xc, Mc, Yc, Cc, sub_cl, n_boot=n_boot)
            per_mediator[med] = {
                "a_X_to_M": round(float(a), 4),
                "a_ci": ci["a"],
                "b_M_to_Y": round(float(b), 4),
                "b_ci": ci["b"],
                "direct_c_prime": round(float(cp), 4),
                "direct_ci": ci["c_prime"],
                "indirect_ab": round(float(a * b), 4),
                "indirect_ci": ci["indirect_ab"],
                # 三段各自是否显著——拼起来才看得出断在哪一段
                "a_sig": _excludes_zero(ci["a"]),
                "b_sig": _excludes_zero(ci["b"]),
                "direct_sig": _excludes_zero(ci["c_prime"]),
                "mediated": _excludes_zero(ci["indirect_ab"]),
            }
        out[label] = {"n": int(sel.sum()),
                      "n_conversations": int(len(np.unique(sub_cl))),
                      "mediators": per_mediator}
    return out


# ------------------------------------------------------------------ 调节（替代方案）


def _burst_len(messages, i, window):
    """从 i 开始，同一人连发了多少条。"""
    who = messages[i]["sender"]
    j = i + 1
    while (j < len(messages) and messages[j]["sender"] == who
           and messages[j]["timestamp"] - messages[j - 1]["timestamp"] <= window):
        j += 1
    return j - i


def _responder_features(messages, embeddings, i_a, i_b, window):
    """回应方（B）这条消息的行为特征。`burst` 就是三消息链的那个选择变量本身。"""
    m = messages[i_b]
    return {
        "continuity": _topic_continuity(embeddings[i_a], embeddings[i_b]),
        "length": float(len(m["content"])),
        "latency": float(m["timestamp"] - messages[i_a]["timestamp"]),
        "question": 1.0 if any(w in m["content"] for w in QUESTION_WORDS) else 0.0,
        "burst": float(_burst_len(messages, i_b, window)),
    }


def _interaction(X, M, Y):
    """Y ~ 1 + X + M + X*M，返回 (b_X, b_M, b_XM)。"""
    beta = _ols(Y, [X, M, X * M])
    return float(beta[1]), float(beta[2]), float(beta[3])


def _boot_interaction(X, M, Y, clusters, n_boot=4000, seed=0):
    uniq = np.unique(clusters)
    empty = [None, None]
    if len(uniq) < 5:
        return {"X": empty, "M": empty, "XM": empty}
    idxs = {k: np.where(clusters == k)[0] for k in uniq}
    rng = np.random.default_rng(seed)
    acc = {"X": [], "M": [], "XM": []}
    for _ in range(n_boot):
        idx = np.concatenate([idxs[k] for k in rng.choice(uniq, size=len(uniq), replace=True)])
        try:
            bx, bm, bxm = _interaction(X[idx], M[idx], Y[idx])
        except np.linalg.LinAlgError:
            continue
        acc["X"].append(bx); acc["M"].append(bm); acc["XM"].append(bxm)
    return {k: ([round(float(x), 4) for x in np.percentile(v, [2.5, 97.5])] if v else empty)
            for k, v in acc.items()}


def moderation(messages, values, embeddings, *, window=TURN_WINDOW, n_boot=4000):
    """调节检验 `Y ~ X + M + X*M`——「她迎合 vs 他不接」的可检验版本。

    为什么不建中介，而建调节：

    1. **不要求时序。** 调节只需要 M 和 X 一起预测 Y，不需要 M 在 Y 之前。
       所以不必用三消息链，**583 个回合全用上**——选择偏差随之消失。
       这正是中介版本栽掉的地方：三消息链要求 B 连发 ≥2 条，而「连不连发」
       本身就是被测的行为维度，等于在选择变量上做了条件。
    2. **不说因果。** 中介声称「X 经由 M 影响 Y」；调节只说「X 对 Y 的作用强度
       取决于 M」。后者更弱，但**它是这份数据真能支撑的强度**。

    `X*M` 的系数就是答案：显著则说明「B 的情绪跟随 A 的强度，取决于 B 的回应方式」；
    不显著就写明机制在本数据上不可回答，不要继续挖。

    仍然做会话内中心化，去掉「有些对话整体就更正向」的组间成分。

    读结果时的两个陷阱
    ------------------
    1. **点估计排序 ≠ 效应强度排序。** 实测 她→我 的 `b_XM=+0.148` 比 我→她 的
       `+0.087` **还大**，但区间宽一倍（SE 0.086 vs 0.050）——那个大点估计是少数
       几段对话撑起来的。**必须同时报点估计和区间宽度**：区间宽度反映的是段间
       异质性，只报点估计会让读者以为「她→我 更强」，那是反的。
    2. **不要把「正交化后还剩一点」读成「去掉泄漏后仍有真效应」。** 见
       `affect.orthogonalise`：Gram-Schmidt 只保证数值正交，残差子空间里可能还有
       没被减掉的情绪相关方向。剩下那点相关只能说「来源不明」。
    """
    from affect import _turns

    conv = _conv_ids(messages)
    turns = _turns(messages, values, window)
    out = {"turns": len(turns), "conversations": int(conv.max() + 1), "n_boot": n_boot,
           "note": "Y~X+M+X*M，会话内中心化，X*M 的 CI 按会话整块重抽。"
                   "X*M 显著 = 跟随强度依赖回应方式；不显著 = 机制不可回答。"}

    for who, label in (("me", "me_to_them"), ("them", "them_to_me")):
        sub = [t for t in turns if t[0] == who]
        if len(sub) < 30:
            out[label] = None
            continue
        i_a = [t[4] - 1 for t in sub]
        i_b = [t[4] for t in sub]
        X0 = np.array([t[1] for t in sub])
        Y0 = np.array([t[2] for t in sub])
        cl = np.array([conv[i] for i in i_b])

        Xc = _center(X0, cl)
        Yc = _center(Y0, cl)
        feats = [_responder_features(messages, embeddings, a, b, window)
                 for a, b in zip(i_a, i_b)]

        per_feature = {}
        for name in ("continuity", "length", "latency", "question", "burst"):
            M0 = np.array([f[name] for f in feats], dtype=float)
            if M0.std() < 1e-9:
                continue
            Mc = _center(M0, cl)
            bx, bm, bxm = _interaction(Xc, Mc, Yc)
            ci = _boot_interaction(Xc, Mc, Yc, cl, n_boot=n_boot)
            per_feature[name] = {
                "b_X": round(bx, 4),
                "b_M": round(bm, 4),
                "b_XM": round(bxm, 4),
                "b_XM_ci": ci["XM"],
                "moderates": _excludes_zero(ci["XM"]),
            }
        out[label] = {"n": len(sub),
                      "n_conversations": int(len(np.unique(cl))),
                      "features": per_feature}
    return out
