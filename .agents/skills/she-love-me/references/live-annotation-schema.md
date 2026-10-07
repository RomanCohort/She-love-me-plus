# 实时标注层契约（live annotation schema）

这是「实时仪表盘」的**语义标注层**的公开接口。语义层**不常驻、不是 API 调用**——
是用户在 Claude Code 会话里让 Claude 读一批待分析的消息、把判断写回磁盘。

三个文件、三个写者，**一个文件只有一个写者，所以全流程不需要锁**：

| 文件 | 写者 | 读取者 |
|------|------|--------|
| `data/live/inbox.json` | 守护进程（`live_ingest.py`） | Claude 会话（读任务）、仪表盘（读「待标注」计数） |
| `data/live/annotations/<request_id>.json` | Claude 会话 | 仪表盘、Claude 会话（读进度） |
| `data/live/runs/<request_id>.progress.json` | Claude 会话 | Claude 会话（认领/超时）、仪表盘 |

---

## 唯一最重要的一条

**守护进程永不调 LLM。界面必须把「待标注」当成一等视觉状态，而不是一个转不完的圈。**

如果这一条放松了，仪表盘会把「还没人看过」渲染成一个 loading 圈，看起来像坏了——
这是这个设计最可能的失败方式。

---

## 通用约定（照抄 `affect_schema.py` 的既有契约，不另立第二套）

1. **`schema_version` 用 `1.0.0`。** 每个文件都带它。
2. **三态 `status`，键永远存在。** 消费者判 `status`，不判键在不在。`status != "ok"`
   时除固定骨架键外一律为 `null`，不要假设其它键在。
3. **只增不改。** 新字段可以加（消费者应忽略不认识的键）；已有字段名和类型不得变更。
4. **`msg_key` 一律用 `fts_fallback.msg_key_of()`**，不要自己写一份。它不含 `type`、
   不含 `local_id`（`local_id` 跨轮次不稳定），是唯一跨轮次持久化的幂等键。
5. 时间戳是 **epoch 秒**（`time.time()` 或消息的 `create_time`），不是 ISO 字符串。

---

## 受控值域（情绪 / 意图 / 时刻 kind）——只加值域，不改字段

`per_message[].emotion.label` / `per_message[].intent.label` / `key_moments[].kind`
**字段名与类型都不变**（仍是 string），本文档在这里补上此前缺失的**推荐值域**：
见 `intent-taxonomy.md`（12 情绪 / 36 意图 / 12 时刻 kind）。

**这三条是本节的要点**：

1. **词表是推荐值域，不是硬校验。** 读侧继续按本文档的容错契约工作——
   遇到词表外的值**不得报错、不得丢弃整条记录**，UI 走灰色兜底显示即可。
   标 `unknown` / `难判断` 是合法且常用的结果，不是失败。
2. **`per_message[].probs` 的键用 12 情绪的英文 `key`**（`happy` / `calm` / `shy` …），
   而 `emotion.label` 用对应的中文标签（`开心` / `平静` / `害羞` …）。
   下方示例里出现的「平淡 / 敷衍 / 关心」是词表未定义时期的遗留写法，**不要照抄**。
3. **`key_moments[].kind` 的取值见 `intent-taxonomy.md` 第四节**（`boundary` / `reopen` /
   `invitation` / `confirmation` / `cancellation` / `care` / `preference` / `disclosure` /
   `commitment` / `question` / `correction`）。示例里的 `"invitation"` 正是其中之一。
   其中 `question` / `correction` / `preference` 属**次要时刻**，默认不进时间线主视图。

> ⚠️ **`kind` 的 `confidence < 0.65` 不写入**（沿用上游 `collectEvents()` 的门槛）。
> ⚠️ 逐条标注**不是全量义务**——`per_message` 里没有的 `msg_key` 就是 `pending`，
> 这条语义由读侧负责（见下文「`per_message` 语义」一节），写侧不必补全。

---

## 文件 1：`inbox.json`（守护进程写）

「有 N 条还没被任何 annotation 覆盖的消息」。守护进程在 `ingest()` 末尾重写它。

```json
{
  "schema_version": "1.0.0",
  "request_id": "r-20261007T145500-a3f9",
  "created_at": 1791356900.0,
  "contact": {"display": "对方", "username": "wxid_xxxxxxxxxxxxxxxx"},
  "cursor": {
    "from_ts": 1791122366,
    "to_ts": 1791355060,
    "from_msg_key": "…",
    "to_msg_key": "…"
  },
  "counts": {"new": 7, "pending_total": 20, "text_missing": 0},
  "messages": [
    {
      "msg_key": "…",
      "local_id": 1542,
      "sender": "them",
      "timestamp": 1791355060,
      "type": "text",
      "content": "…",
      "quote": null,
      "text_missing": false,
      "source": "message_fts"
    }
  ],
  "context": {
    "reply_pending": true,
    "pending_duration": 1800.0,
    "hour": 21,
    "data_age_seconds": 300,
    "advice": {},
    "stats_summary": {}
  }
}
```

- `messages` 数组是「**新增的、还没被覆盖**」的那批消息——不是全量历史。守护进程
  **永不修改已存在 request 的 messages 数组**，新材料一律开新 `request_id`。
- `counts.new` = 这批消息条数；`counts.pending_total` = 累计还没被覆盖的总条数
  （含以前批次里一直没被标注的）。`counts.text_missing` = 这批里 `text_missing` 的条数。
- `cursor.from_ts` / `to_ts` 是这批消息的时间范围。

## 文件 2：`annotations/<request_id>.json`（Claude 写）

Claude 会话读完 `inbox.json`，写回一份标注。`request_id` 与 inbox 里的同名。

```json
{
  "schema_version": "1.0.0",
  "request_id": "r-…",
  "produced_at": 1791357000.0,
  "model": "claude-code-session",
  "status": "ok",
  "reason": null,
  "per_message": [
    {
      "msg_key": "…",
      "state": "done",
      "emotion": {"label": "平静", "valence": 0.1, "arousal": 0.3, "confidence": 0.6},
      "intent": {"label": "回答问题", "confidence": 0.7},
      "probs": {"calm": 0.4, "annoyed": 0.3, "shy": 0.3},
      "reason": "一句话理由",
      "evidence": ["msg_key"]
    }
  ],
  "key_moments": [
    {
      "at_msg_key": "…",
      "timestamp": 1791355060,
      "kind": "invitation",
      "title": "标题",
      "why": "为什么",
      "evidence": ["…"]
    }
  ],
  "threshold_notes": [{"signal": "cold_response", "read": "读法", "confidence": 0.5}],
  "blind_spots": {"text_missing": 0, "unrecoverable_media": 3}
}
```

- **顶层 `status` 三态**：`ok`（全部标注完）/ `partial`（只标了一部分）/
  `failed`（一句都没标成）。`reason` 仅在 `status != "ok"` 时非空。
- `per_message[].state` 三态：`done` / `incomplete` / `skipped`。
  - `done`：这一条明确标了情绪/意图，`emotion`、`intent`、`probs` 都有值。
  - `incomplete`：看了、但证据不足所以只给部分（`emotion` 或 `intent` 里至少一个给不出）。
  - `skipped`：这一条不值得标（系统消息、撤回、纯表情等），`emotion`/`intent`/`probs` 为 `null`。
- 消费者判 `per_message[].state`，不判 `emotion` 键在不在。`state` 是**每条消息自己的 status**。
- `probs` 是情绪类别 -> 概率的映射，值域 `[0,1]`；UI 取概率最高的前 3 个展示。
- `key_moments` 是最值得单独拉出来讲的时间点，语义层的「关键时机轴」读它。
- `blind_spots.text_missing` / `unrecoverable_media`：这批里内容结构性不可得、连
  LLM 也读不了的数量，跟 `inbox.context` 里的盲区口径对上。

## 文件 3：`runs/<request_id>.progress.json`（Claude 写）

任务进度。**认领协议：文件存在 + mtime 就是认领**（不搞加锁，一个文件一个写者）。

```json
{
  "request_id": "r-…",
  "state": "working",
  "total": 20,
  "done": 9,
  "started_at": 1791356950.0,
  "updated_at": 1791357000.0,
  "per_message": {"<msg_key>": "done"}
}
```

- `state` 四态：`claimed`（认领了还没开工）/ `working`（在标）/ `done`（写完标注）/
  `stale`（认领了但超时了）。
- `per_message` 是 `msg_key -> 该条进度状态`，值域：`pending` / `analyzing` /
  `done` / `incomplete` / `skipped`。
- **超时**：`updated_at` 超过 10 分钟没动 → 视为 `stale`，别的 Claude 会话可以接管。
- 写完 annotations 后把 `state` 置 `done`；中途放弃置 `stale`。

---

## 状态优先级：成品 > 进度

同一个 `msg_key` 可能同时出现在 `annotations/<id>.json` 的 `per_message`（成品态
`done/incomplete/skipped`）和某份 `runs/<id>.progress.json` 的 `per_message`（进度态
`analyzing/pending`）里。**成品态永远赢**：

| 成品（annotation） | 进度（progress） | UI 显示 |
|--------------------|------------------|---------|
| `done` / `incomplete` / `skipped` | 任意 | annotation 的 state（成品优先） |
| 无（或 `pending`） | `analyzing` | **标注中** |
| 无（或 `pending`） | `pending`（且 request `claimed`/`working`） | **标注中** |
| 无（或 `pending`） | 无，或 request `stale`/`done` | **待标注** |

- 「标注中」（`analyzing`）只来自 progress，注解是成品不会再回到「标注中」。
- progress 的 request 层 `state == stale`（认领了但 10 分钟没动）→ UI 顶部显示
  「N 个标注中断」，对应消息按**待标注**处理（不是「标注中」——那会假装有人在标）。
- `skipped` 是「看过决定不标」，与「没写进 per_message（=pending）」不同，见下文。

---

## 认领与覆盖规则（守护进程视角）

1. **守护进程永不修改已存在 request 的 messages 数组。** 新材料一律开新 `request_id`。
2. archive 判定「已覆盖」= `annotations/` 下所有文件的 `per_message[].msg_key` 并集。
   一条消息只要出现在任何一份标注的 `per_message` 里，就算已覆盖（不管它 state 是
   `done` 还是 `skipped`——`skipped` 也是「看过、决定不标」，不是「漏了」）。
3. 覆盖并集只用来算「待标注」的差集；**不删** 任何已写的 annotation 文件。
4. 认领协议不加锁：Claude 写 progress 文件，文件存在 + mtime 就是认领。守护进程
   只管写 inbox，永不去读 progress 判断该不该开新 request——那是 Claude 会话自己的事。

---

## 消费者判读备忘

```python
inbox = read_json("data/live/inbox.json") or {}
n_pending = (inbox.get("counts") or {}).get("pending_total", 0)

for a in list_annotations("data/live/annotations"):
    if a.get("status") == "ok":
        ...
    for pm in a.get("per_message") or []:
        if pm.get("state") == "done":
            ...
        elif pm.get("state") == "skipped":
            ...   # 也是「处理过了」，不是「没处理」
```

**别写** `a["per_message"][0]["emotion"]["label"]`——`state != "done"` 时 `emotion`
可能是 `null` 或根本缺键。永远先判 `state`。

---

## 读侧容错契约（文件接口的边界情况，实测过）

标注是「文件当接口」，而文件接口最容易**静默失效**——出了错不报错，悄悄显示成
别的东西。所以读侧（`live_serve`、`live_ingest`、`live_annotate` 的读取函数）
必须对坏输入宽容，**跳过坏条目、绝不崩溃**。以下每一条都是 2026-10-07 实测过的。

| 情况 | 行为（已实测） |
|------|----------------|
| `annotations/` 目录不存在（全新 live 目录） | `annotation_summary` 返回全 0，`build_inbox` 正常算 pending。不炸。 |
| 标注文件是半截 / JSON 坏 | `read_json` 兜底成 `None`，读侧跳过。只数能解析的。 |
| 标注文件 JSON 合法但形状是 `list` 等非 dict | 读侧 `isinstance(body, dict)` 跳过。不炸。 |
| `per_message` 里的元素不是 dict（字符串 / `None` / 数字） | 读侧 `isinstance(pm, dict)` 跳过该条。**这是修掉的真实 bug**：原来 `pm.get()` 会 `AttributeError`，把 `/api/snapshot` 和 `ingest` 的 inbox 计算都炸掉（后者会崩守护进秇的 tick）。 |
| `per_message` 里的 `msg_key` 当前消息列表里不存在（标注比消息新 / 消息重提取换了 key） | **不会渲染幽灵标注**。UI 只按真实消息的 `msg_key` 去 `annoByKey.get(m.key)` 查，从不反过来遍历标注映射去渲染；`jumpTo` 也是 `if (row)` 守卫。幽灵 key 在映射里但永远查不到，无副作用。 |
| 两份标注覆盖同一个 `msg_key` | **按 `produced_at` 升序排序后，后者覆盖前者**（dict 赋值覆盖）。即**较新的 `produced_at` 赢**。`live_ingest` / `live_serve` / `live_annotate` 三处读侧都是同一实现，行为一致。 |
| `progress` 说 `working` 但 `updated_at` 是 20 分钟前 | `effective_state()` 判 `stale`（超过 `STALE_SECONDS=600` 没动就覆盖）。**UI 不会永远显示「标注中」**——UI 根本不读 progress 的 `working` 态，只读 inbox 的「待标注」计数和 annotation 的最终 `state`。 |
| 顶层 `status: "partial"` 但 `per_message` 全是 `done` | **两者各自为政，不互查**。顶层 `status` 只驱动 `requests_ok` 计数（`partial` 不算 ok）；气泡的状态完全由 `per_message[].state` 驱动（全 `done` 就全显示 done）。**内部矛盾以逐条 `state` 为准，顶层 `status` 只是给批次的粗粒度标注。** |
| `counts.pending_total` 与 `per_message` 实际覆盖数不一致（inbox 写完之后又来了新消息） | 顶部「待标注」数字**直接读 `inbox.counts.pending_total`，不重算**。它是「上次 ingest 那一刻」的快照。新消息到来后，inbox 会在下一次 ingest 重写时更新 `pending_total`。所以这个数字可能**滞后一个 tick**，但不会撒谎——它永远是某个确定时刻的真值。 |

### `per_message` 语义：读者默认 pending，写者不强制补全

**`per_message` 里没有的 `msg_key` = `pending`。这个语义由读者负责，写者不强制补全全量。**

换句话说：Claude 写标注时**不必**给每条消息都写一条——它只写它实际看过的；
所有没出现在 `per_message` 里的消息，UI 一律渲染成「待标注」。`replay` 夹具生成的
progress 里 `total=20` 但 `per_message` 只有 11 条，剩下的 9 条就是 pending。

配套语义：
- `state: "skipped"` 是**显式的「看过、决定不标」**，与「根本没写进 per_message」
  （= 没看过 = pending）**不同**。写者想表达「我看了但这条不用标」必须写 `skipped`，
  不能省略——省略会被读成 pending。
- `state: "incomplete"` 是「看了、但证据不足只给一半」，也**必须显式写**。

### 认领超时的落点

`stale` 判定只在 Claude 会话自己读 progress 时用（`live_annotate --index` /
`--status` 的 `effective_state()`）。守护进程**不看** progress 的 state，所以一个
卡死的会话不会让 daemon 停摆；它只会让那份 request 一直没人接管，等另一个 Claude
会话把它判 stale 后重新认领。UI 同样不显示 progress 的 `working`/`claimed`——
这是有意的：仪表盘只诚实地显示「待标注 N」，不显示「谁正在标」。

