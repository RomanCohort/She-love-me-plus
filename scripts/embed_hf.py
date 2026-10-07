"""
embed_hf.py - 可选的句向量后端（默认关闭）

职责
----
主分析路径（`stats_analyzer.py` + `lexicon.py`）是零第三方依赖的纯词表实现。
本模块是**可选加速器**：当用户显式要求、且本机具备 onnxruntime 时，提供中文
句向量。上层消费它的是 `affect.py`（情感计算层）。

它不参与任何判定，只**供数**。

为什么走 ONNX 而不是 transformers
--------------------------------
`onnxruntime` 是 ~40MB 的轮子，`torch` 是 2GB 起步。这个项目的卖点之一是核心
路径零依赖，可选路径也应当尽量轻。模型用 `bge-small-zh-v1.5` 的 ONNX 社区版
（512 维、4 层、fp32 约 90MB），CPU 上跑一千多条消息是秒级。

隐私
----
只把模型权重**下载**到 `vendor/hf_cache/`，聊天内容不出本机。

设计约束
--------
- **绝不 `die()`**：依赖缺失、下载失败、模型损坏，一律返回带 `degraded_reason`
  的结果，让主分析链继续。
- **默认关闭**：只有调用方显式开启才会加载模型、才会联网。

踩过的坑（都已在本文件里修掉，注释留在对应位置）
------------------------------------------------
1. `huggingface_hub.constants.ENDPOINT` 在 **import 时**固化，事后改 `os.environ`
   完全无效 → 必须把 endpoint 显式传给 `hf_hub_download`；
2. ONNX 的外部权重文件 `<file>_data` **不会**被自动带上 → 必须显式下载；
3. huggingface_hub 默认的 blobs/snapshots 软链布局会被 onnxruntime 的
   「外部数据不得逃逸出模型目录」安全校验拒掉 → 改用 `local_dir` 铺成实文件。
"""
import os
import sys

# ------------------------------------------------------------------ 可选依赖

def _load_ort():
    """导入 onnxruntime。

    抽成函数是为了让**降级路径可自动化测试**。「本机没装 onnxruntime」这条路径
    在开发机上永远走不到——开发机通常早就装好了——于是它在某次重构里悄悄坏掉
    而没人发现。所以要有一个不需要真的卸载依赖的模拟开关：

        AFFECT_FORCE_NO_ONNX=1 python scripts/stats_analyzer.py --affect auto ...

    测试里也可以直接 monkeypatch 本函数抛 ImportError。
    """
    if os.environ.get("AFFECT_FORCE_NO_ONNX"):
        raise ImportError("simulated: AFFECT_FORCE_NO_ONNX=1")
    import onnxruntime as ort_
    return ort_


HAS_ONNX = False
_IMPORT_ERROR = ""

try:
    import numpy as np
    from tokenizers import Tokenizer
    import huggingface_hub

    # constants 是懒加载子模块，`import huggingface_hub` 不会带上它，
    # 必须显式导入才能读到 ENDPOINT。
    from huggingface_hub import constants as hf_constants

    ort = _load_ort()

    HAS_ONNX = True
except ImportError as exc:  # 缺任何一个都不影响主路径
    _IMPORT_ERROR = str(exc)

# ------------------------------------------------------------------ 默认值

DEFAULT_MODEL = "onnx-community/bge-small-zh-v1.5-ONNX"
DEFAULT_ONNX_FILE = "onnx/model.onnx"
DEFAULT_ENDPOINT_FALLBACK = "https://hf-mirror.com"

# 与 analysis-framework.md F5「轴 2 复现性」对齐：同一主题 7 天内 ≥2 次算强信号，
# 所以默认跨天（1 天）起算，抓的是「隔了几天又说一遍同一件事」。
DEFAULT_MIN_SIMILARITY = 0.75
DEFAULT_MIN_GAP_DAYS = 1.0
DEFAULT_SURFACE_MAX = 0.45
DEFAULT_MAX_PAIRS = 40

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CACHE_DIR = os.path.join(_REPO_ROOT, "vendor", "hf_cache")


def available():
    """返回 (是否可用, 不可用原因)。不联网、不加载模型。"""
    if not HAS_ONNX:
        return False, f"缺少可选依赖（onnxruntime / tokenizers / huggingface_hub）：{_IMPORT_ERROR}"
    return True, ""


def _log(msg):
    print(f"[sentiment] {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ 模型下载


def _endpoint_candidates(explicit):
    """产出要依次尝试的 endpoint 列表（None 表示官方默认）。

    优先级：显式参数 > 用户已有的 HF_ENDPOINT > 官方站（不通则自动回退镜像）。

    刻意不硬编码 HF_ENDPOINT 环境变量：`huggingface_hub.constants.ENDPOINT` 在
    **import 时**就固化了，之后再改 `os.environ` 根本不起作用——早先的版本正是
    这么错的：日志打着「回退镜像」，请求却仍然打向 huggingface.co。
    正确做法是把 endpoint 显式传给 `hf_hub_download`（它接受这个参数）。
    """
    if explicit:
        return [explicit]
    env = os.environ.get("HF_ENDPOINT")
    if env:
        return [env]
    return [None, DEFAULT_ENDPOINT_FALLBACK]


def _base_url(endpoint):
    return (endpoint or hf_constants.ENDPOINT).rstrip("/")


_PROBE_CACHE = {}


def _reachable(endpoint, timeout=6):
    """快速探活，避免源不可达时白等 huggingface_hub 的五轮指数退避。

    有 HTTP 响应（哪怕 404）就算通；连接失败 / SSL 失败才算不通。
    """
    import urllib.error
    import urllib.request

    base = _base_url(endpoint)
    if base in _PROBE_CACHE:
        return _PROBE_CACHE[base]
    try:
        req = urllib.request.Request(base + "/api/models", method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout):
            ok = True
    except urllib.error.HTTPError:
        ok = True  # 服务端答了话，说明链路是通的
    except Exception:
        ok = False
    _PROBE_CACHE[base] = ok
    return ok


def _model_dir(cache_dir, repo_id):
    return os.path.join(cache_dir, repo_id.replace("/", "--"))


def _download(repo_id, filename, cache_dir, endpoint, allow_download):
    """取单个文件到本地，返回路径。全部失败时抛异常，由上层 `analyze` 降级。

    用 `local_dir` 而不是默认的 cache_dir，是为了绕开 huggingface_hub 的
    blobs/snapshots 软链布局：那种布局下 `model.onnx` 是指向 blobs 的软链，
    而 `model.onnx_data` 留在 snapshots 里，onnxruntime 加载时会做「外部权重
    不得逃逸出模型目录」的安全校验，直接拒绝加载。铺成实文件就没这问题。

    `allow_download=False`（`--no-download`）时只查本地，绝不联网。
    """
    dest = os.path.join(_model_dir(cache_dir, repo_id), *filename.split("/"))

    # 1) 命中本地就直接用——完全不联网，也保证「源不可达但已下过」时仍能跑
    if os.path.exists(dest):
        return dest

    if not allow_download:
        raise RuntimeError(f"本地无 {filename}，且已指定 --no-download（不联网）")

    # 2) 逐个 endpoint 尝试，不可达的直接跳过
    last = None
    for ep in _endpoint_candidates(endpoint):
        base = _base_url(ep)
        if not _reachable(ep):
            _log(f"{base} 不可达，跳过")
            continue
        try:
            path = huggingface_hub.hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                local_dir=_model_dir(cache_dir, repo_id),
                endpoint=ep,
            )
            return path if os.path.exists(path) else dest
        except Exception as exc:
            last = exc
            _log(f"{base} 取 {filename} 失败：{type(exc).__name__}: {exc}")

    raise last or RuntimeError(f"所有 endpoint 均不可达，且本地无 {filename}")


def cached(model_id=DEFAULT_MODEL, onnx_file=DEFAULT_ONNX_FILE,
           cache_dir=DEFAULT_CACHE_DIR):
    """模型是否已落在本地——**纯本地探测，绝不联网**。

    降级表靠它区分两件完全不同的事：

    - 本地无缓存 + `--no-download` → `disabled`（一开始就没打算算）
    - 有缓存但加载/下载失败        → `degraded`（试了但没成）

    只探 tokenizer 与 ONNX 本体；外部权重文件 `_data` 缺席会让加载阶段失败，
    那属于 `degraded`，不在这里判。
    """
    d = _model_dir(cache_dir, model_id)
    return all(os.path.exists(os.path.join(d, *f.split("/")))
               for f in ("tokenizer.json", onnx_file))


def load(model_id=DEFAULT_MODEL, onnx_file=DEFAULT_ONNX_FILE,
         cache_dir=DEFAULT_CACHE_DIR, endpoint=None, allow_download=True):
    """取（必要时下载）模型并建好 `(session, tokenizer)`。

    **任何失败都抛异常——由调用方决定算 `disabled` 还是 `degraded`。**
    本函数不持有策略，只负责把「能不能用」这件事问清楚。
    """
    # 不依赖调用方先查 available()——否则缺依赖时会炸出 NameError 而不是 ImportError，
    # 调用方按异常类型分派的逻辑就失效了。
    if not HAS_ONNX:
        raise ImportError(_IMPORT_ERROR or "缺少可选依赖（onnxruntime / tokenizers / huggingface_hub）")

    tok_path = _download(model_id, "tokenizer.json", cache_dir, endpoint, allow_download)
    onnx_path = _download_onnx(model_id, onnx_file, cache_dir, endpoint, allow_download)
    tokenizer = Tokenizer.from_file(tok_path)
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding()
    return _load_session(onnx_path), tokenizer


def _download_onnx(repo_id, onnx_file, cache_dir, endpoint, allow_download):
    """取 ONNX 模型本体，以及它的外部权重文件。

    超过 ~2GB 的 ONNX 会把权重拆到同名的 `<file>_data` 里，而这个文件
    `hf_hub_download` **不会**自动带上——只取点名的那一个。漏了它，模型加载会报
    「External data path does not exist」。小模型没有这个文件，取不到是正常的。
    """
    path = _download(repo_id, onnx_file, cache_dir, endpoint, allow_download)
    try:
        _download(repo_id, onnx_file + "_data", cache_dir, endpoint, allow_download)
    except Exception as exc:
        _log(f"无外部权重文件（小模型属正常）：{type(exc).__name__}")
    return path


# ------------------------------------------------------------------ 句向量


def _load_session(onnx_path):
    opts = ort.SessionOptions()
    opts.log_severity_level = 3  # 只保留 error，别把 onnxruntime 的 info 刷屏
    return ort.InferenceSession(onnx_path, sess_options=opts, providers=["CPUExecutionProvider"])


def _embed(texts, sess, tokenizer, batch_size=16):
    """对文本列表求句向量（CLS 池化 + L2 归一化）。

    BGE 系列用 CLS 而不是 mean pooling；onnx-community 导出的图里不含池化层，
    所以要自己取 last_hidden_state 的第 0 个 token。
    """
    input_names = {i.name for i in sess.get_inputs()}
    output_names = [o.name for o in sess.get_outputs()]
    out = []
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        encs = [tokenizer.encode(t) for t in chunk]
        maxlen = max(len(e.ids) for e in encs)
        ids = np.zeros((len(encs), maxlen), dtype=np.int64)
        mask = np.zeros((len(encs), maxlen), dtype=np.int64)
        for row, e in enumerate(encs):
            ids[row, :len(e.ids)] = e.ids
            mask[row, :len(e.ids)] = e.attention_mask

        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in input_names:
            feed["token_type_ids"] = np.zeros_like(ids)

        result = sess.run(None, feed)
        if "sentence_embedding" in output_names:
            # 导出图里已含池化层，直接用它的输出，别再自己池化一遍
            vec = result[output_names.index("sentence_embedding")]
        else:
            # 裸 BertModel：自己做 CLS 池化（BGE 系列用 CLS，不是 mean pooling）
            vec = result[0][:, 0]
        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        out.append(vec / np.maximum(norm, 1e-9))   # L2 归一化 → 点积即余弦
    return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)


# ------------------------------------------------------------------ 近邻检索


def _surface_overlap(a, b):
    """字面重合度（0–1）。

    用来把「原话复述」和「换个说法」分开：字面重合高的是复读（词表也能找），
    字面重合低而语义相似度高的才是词表原理上找不到的那类。
    """
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def _recurrent_pairs(messages, embeddings, *, min_similarity, min_gap_days,
                     surface_max, max_pairs):
    """找出「跨天 + 语义相近 + 字面不像」的消息对。

    ⚠️ 实测提示（别对它在闲聊语料上抱期待）：在 1105 条中文日常聊天上，本函数
    几乎**不产出结果**——高相似对绝大多数是原话复读，被 surface_ratio 滤掉；
    真正「字面不同、语义相同、还跨了天」的对基本不存在（阈值 0.70 时只剩 3 对，
    其中一条还是两段 ASCII 报错碎片撞出来的假阳性）。它对长文本 / 讨论型语料
    可能有用，但在日常闲聊上这条路是死的。
    """
    n = len(messages)
    if n < 2:
        return []

    sims = embeddings @ embeddings.T
    np.fill_diagonal(sims, -1.0)

    ts = np.array([m["timestamp"] for m in messages], dtype=np.float64)
    gap_days = np.abs(ts[:, None] - ts[None, :]) / 86400.0

    ii, jj = np.where((sims >= min_similarity) & (gap_days >= min_gap_days))
    # 只保留 i<j，避免同一对出现两次
    keep = ii < jj
    ii, jj = ii[keep], jj[keep]
    if len(ii) == 0:
        return []

    # 先按相似度降序粗排，再逐个算字面重合——字面比对比矩阵运算贵得多，
    # 提前截断可以避免在长语料上退化成 O(n²) 次 SequenceMatcher。
    order = np.argsort(-sims[ii, jj])[:max_pairs * 8]
    ii, jj = ii[order], jj[order]

    pairs = []
    for a, b in zip(ii, jj):
        ma, mb = messages[int(a)], messages[int(b)]
        ratio = _surface_overlap(ma["content"], mb["content"])
        if ratio >= surface_max:
            continue  # 复读，不是「换个说法」
        pairs.append({
            "a": {"ts": ma["timestamp"], "sender": ma["sender"], "content": ma["content"]},
            "b": {"ts": mb["timestamp"], "sender": mb["sender"], "content": mb["content"]},
            "similarity": round(float(sims[a, b]), 3),
            "gap_days": round(float(gap_days[a, b]), 1),
            "surface_ratio": round(ratio, 3),
        })
        if len(pairs) >= max_pairs:
            break

    # 同一句可能和很多句相似，按 a 去重，让结果覆盖更多不同话题
    seen, deduped = set(), []
    for p in pairs:
        if p["a"]["content"] in seen:
            continue
        seen.add(p["a"]["content"])
        deduped.append(p)
    return deduped


# ------------------------------------------------------------------ 入口


def analyze(messages, *, model_id=DEFAULT_MODEL, onnx_file=DEFAULT_ONNX_FILE,
            cache_dir=DEFAULT_CACHE_DIR, endpoint=None, allow_download=True,
            min_similarity=DEFAULT_MIN_SIMILARITY, min_gap_days=DEFAULT_MIN_GAP_DAYS,
            surface_max=DEFAULT_SURFACE_MAX, max_pairs=DEFAULT_MAX_PAIRS):
    """唯一入口。返回写进 stats.json 的 `sentiment` 对象。

    **任何失败都只降级、不抛出**——`degraded_reason` 非空即表示这一路没跑通，
    此时 `recurrent_signals` 为空，其余字段仅作诊断用。
    """
    ok, why = available()
    base = {
        "engine": "onnx",
        "model": model_id,
        "onnx_file": onnx_file,
        "params": {
            "min_similarity": min_similarity,
            "min_gap_days": min_gap_days,
            "surface_max": surface_max,
        },
    }
    if not ok:
        return {**base, "degraded_reason": why, "embedded": 0, "dim": None,
                "recurrent_signals": []}

    texts = [m["content"] for m in messages if m.get("type") == "text"]
    if len(texts) < 2:
        return {**base, "degraded_reason": "文字消息不足 2 条，无需检索",
                "embedded": 0, "dim": None, "recurrent_signals": []}

    try:
        os.makedirs(cache_dir, exist_ok=True)
        sess, tokenizer = load(model_id, onnx_file, cache_dir, endpoint, allow_download)
        embeddings = _embed(texts, sess, tokenizer)
    except Exception as exc:
        return {**base, "degraded_reason": f"{type(exc).__name__}: {exc}",
                "embedded": 0, "dim": None, "recurrent_signals": []}

    text_msgs = [m for m in messages if m.get("type") == "text"]
    pairs = _recurrent_pairs(
        text_msgs, embeddings,
        min_similarity=min_similarity,
        min_gap_days=min_gap_days,
        surface_max=surface_max,
        max_pairs=max_pairs,
    )
    return {
        **base,
        "degraded_reason": None,
        "embedded": len(texts),
        "dim": int(embeddings.shape[1]),
        "recurrent_signals": pairs,
    }
