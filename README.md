<div align="center">

<img src="assets/banner.svg" alt="她不一样.Skill" width="860" />

<br/>

[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-0078d4.svg?style=flat-square)]()
[![WeChat](https://img.shields.io/badge/WeChat-4.0%2B-07c160.svg?style=flat-square)]()
[![QQ](https://img.shields.io/badge/QQ-NapCat%20%2B%20QCE-12b7f5.svg?style=flat-square)](https://github.com/shuakami/qq-chat-exporter)
[![Agent Skill](https://img.shields.io/badge/Universal-Agent%20Skill-d97706.svg?style=flat-square)](https://github.com/RomanCohort/She-love-me-plus)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776ab.svg?style=flat-square)]()
[![Zero Deps](https://img.shields.io/badge/核心依赖-零第三方-22c55e.svg?style=flat-square)]()

[快速开始](#快速开始) · [功能特性](#功能特性) · [实时链路](#实时链路) · [工作原理](#工作原理) · [致谢](#致谢)

</div>

---

## 简介

**她不一样** 是一个**通用 Agent Skill**，支持 Claude Code、Codex、Cursor、GitHub Copilot、Gemini CLI 等主流 AI 编程工具。

只需要一句调用指令（例如 Claude 里输入 `/she-love-me`，Codex 里输入 `$she-love-me`），它就能自动解密你的微信数据库（或通过 QCE 提取 QQ 记录）、分析你和某个联系人的全部聊天记录，帮你看清：**她是不是真的不一样——这段感情里，你们到底是什么关系？**

融入专业心理学框架（依恋类型 · Gottman · Sternberg 三角 · 亲密关系六维好感度），支持**危险信号预警**、**逐条对话动作分类**、**军师建议**、**👴 祖师爷寄语**，全程本地运行，数据不上传任何服务器。

> **本仓库是 Plus 版。** 相比原版，新增：逐条对话动作分类学（12 情绪 / 36 意图）、六维好感度评分、关键时刻时间线、SSS–D 回复评分、`analysis.json` 结构契约自检，以及**实时监控链路**（带 WAL 的增量解密 + 本地仪表盘）。

> 不想依赖 agent 入口？
> 可以直接使用传统脚本方案，先导出 `messages.json`，再生成 `analysis_prompt.txt` 交给任意聊天模型：
> [traditional-deployment/README.md](traditional-deployment/README.md)

---

## 快速开始

### 前置条件

**微信分析**：
- Windows / macOS + WeChat 4.0+（**必须处于登录运行状态**）
- Windows 需要使用**管理员终端**
- macOS 请确保终端具备必要系统权限，并按上游解密器提示授权

**QQ 分析**：
- 安装并启动 [QQ Chat Exporter (QCE)](https://github.com/shuakami/qq-chat-exporter)（NapCat + QCE 插件）
- 用手机 QQ 扫码登录，获取控制台显示的 Access Token

**运行环境**：Python 3.9+。项目自身**零第三方依赖**（只用标准库 `sqlite3`），解密器需要的 `pycryptodome` / `zstandard` 由 `setup_check.py` 自动装进 `vendor/`。

### 安装与运行

```bash
git clone https://github.com/RomanCohort/She-love-me-plus
cd She-love-me-plus
```

| 工具 | 调用方式 |
|------|----------|
| [Claude Code](https://claude.ai/code) / [Cursor](https://cursor.sh) / [Copilot](https://github.com/features/copilot) / [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `/she-love-me` |
| [Codex](https://developers.openai.com/codex/overview) | `$she-love-me` 或直接说"使用 she-love-me 分析聊天记录" |

**就这些。** Skill 会先询问平台（微信 / QQ），然后自动处理一切——解密、提取、分析、生成报告。

> 如果你是在会话启动后才 clone 仓库，部分 Agent 需要在仓库根目录重开一次会话，才能重新加载仓库级 Skill。

### 纯手工流程（不用 Agent）

```bash
# 1. 环境检查 + 自动部署解密器
python scripts/setup_check.py --ensure-decryptor

# 2. 解密（需管理员终端 + 微信已登录）
python scripts/decrypt_wechat.py

# 3. 列出联系人（按消息数排序）
python scripts/list_contacts.py --decrypted-dir vendor/wechat-decrypt/decrypted

# 4. 提取指定联系人到独立目录
python scripts/extract_messages.py \
  --decrypted-dir vendor/wechat-decrypt/decrypted \
  --contact "<联系人名字>" \
  --output-dir data/contacts

# 5. 全量统计
python scripts/stats_analyzer.py \
  --input "data/contacts/<联系人>__<hash>/messages.json" \
  --output "data/contacts/<联系人>__<hash>/stats.json"

# 6. 分层采样（--preview 先看各时间范围条数，再选一个）
python scripts/build_chat_history.py --input "data/contacts/<联系人>__<hash>/messages.json" --preview
python scripts/build_chat_history.py \
  --input "data/contacts/<联系人>__<hash>/messages.json" \
  --output "data/contacts/<联系人>__<hash>/chat_history.txt" --since 2026-07-01

# 7. 交给 AI 写 analysis.json（schema 见 references/report-schema.md），然后出报告
python scripts/generate_html_report.py \
  --stats "data/contacts/<联系人>__<hash>/stats.json" \
  --analysis "data/contacts/<联系人>__<hash>/analysis.json" \
  --contact "<联系人名字>" \
  --output "data/contacts/<联系人>__<hash>/reports/"
```

> macOS 用 `python3` 替换 `python`。

### 可选：导出微信表情资源

```bash
python scripts/export_emojis.py --input "data/contacts/<联系人>__<hash>/messages.json"
```

默认在该联系人目录下生成 `emojis.json` / `emojis.csv` / `emojis_assets/` / `emojis_download_manifest.json` / `emojis_preview.html`。聊天记录与表情**分开但不断链**：`messages.json` 里的表情消息通过 `emoji_ref` 关联到 `emojis.json`。

---

## 功能特性

| 功能 | 说明 |
|------|------|
| 🔓 **自动解密** | 自动 clone 并调用 wechat-decrypt，无需手动操作 |
| 👥 **联系人选择** | 按消息数量排列，选你想分析的那个人 |
| 📊 **主动指数** | 主动发起占比 · 连续轰炸次数 · 回复速度差 · 消息长度比 |
| 💜 **被爱指数** | 对方主动次数 · 晚安/早安分析 · 关心频率 |
| 🧊 **冷淡检测** | "嗯""哦""好" 占比 · 长时间已读不回统计 |
| 🧠 **依恋类型诊断** | 安全型 / 焦虑型 / 回避型 / 恐惧型，双方都分析 |
| 🔄 **追逃循环复盘** | 还原完整"案发现场"：触发→撤退→升级→恶化 |
| 💘 **Sternberg 三角** | 激情 · 亲密 · 承诺三维评分，判断爱情类型 |
| 🩹 **修复尝试分析** | 冷战后谁低头？对方接受还是继续惩罚？ |
| ⚠️ **危险预警** | 7 类信号（煤气灯 · 爱情轰炸 · 间歇性强化 · 单相思痴迷等）· **双阈值触发**（量化 + 文本同时满足才高亮，否则降级为观察提示） |
| 🎯 **军师模式** | 核心诊断 + 停止/开始建议（含时机）+ 路线图 + **止损红线** |
| 👴 **祖师爷寄语** | 童锦程视角 · 读局 + 推进关系三条实招 + 金句收尾 |

### Plus 版新增

| 功能 | 说明 |
|------|------|
| 🗣️ **对话动作分类（模块 H）** | 逐条判定 **12 类情绪 + 36 类意图**，按发送方统计分布。`学术讨论` 计入「投入」而非敷衍——这条口径直接接进原有的「回应四类」判定 |
| 📊 **六维好感度（A5.1）** | 主动延续 / 回应投入 / 关心体贴 / 自我开放 / 亲密表达 / 实际行动，五档量表 + 权重。**与「情感对称性」正交**：一个量对不对等，一个量对方喜不喜欢你 |
| 🚩 **边界封顶** | 检出未撤回的「拒绝/划清界限」时刻 → 总分封顶 25。被后续「重新靠近」撤回的边界不再封顶 |
| 🕐 **关键时刻时间线（A3.1）** | 11 类时刻的事件序列：邀约 / 确认 / 取消 / 关心 / 敞开心扉 / 表达心意 / **重新靠近** / 拒绝划界 …… 次要时刻（提问/澄清/说出喜好）默认淡化 |
| 📝 **回复评分（D6）** | 给你**自己**的关键回复打 SSS–D，附两条替代表达。**「不回复」是合法选项**——语料里最高频的错误是"该停的时候补了第二句" |
| 🧾 **结构契约自检** | `analysis_contract.py` 在落盘前抓出枚举越界、假精确分数、口径缺失等问题 |
| 📡 **实时链路** | 带 WAL 的增量解密 + 本地只读仪表盘。见 [实时链路](#实时链路) |

> **关于「假精确」**：六维好感度里只有「主动延续」有干净的统计锚点，`自我开放` / `亲密表达` / `实际行动` **没有对应字段**。所以纯文本依据的维度分数**只能取 0/25/50/75/100** 这五个档位锚点，给 `72` 这种数字会被契约自检拦下——没统计底的精值是假精确。

### 输出效果

```
🔥 主动指数   73 ████████░░  你发起对话 72%
💜 被爱指数   66 ███████░░░  对方主动次数、晚安频率、关心密度
🧊 冷淡指数   28 ███░░░░░░░  回复速度、敷衍词占比
```

报告为**深色现代风格单文件 HTML**（Chart.js 走 CDN，离线打开会缺图），另附终端 Markdown 摘要。首次运行后在 `reports/` 用浏览器打开。

---

## 实时链路

原版是"跑一次、看一眼"的批处理。实时链路把它变成**可以一直开着的监控**——
新消息进来就自动解密、增量摄取、写进本地仪表盘，语义标注由 Claude Code 会话按需补。

### 为什么需要它（一个实测发现）

上游 `wechat-decrypt` 的 `decrypt_database()` **只读数据库主文件，从不读 `-wal`**。

而这台机器上 `session.db-wal` 有 4MB 未 checkpoint。实测只解主文件会让
`last_timestamp` 停在 `10-04 21:59`，真实值却是 `10-07 14:37` —— **差 2 天 16 小时**。

`live_decrypt.py` 就是补上这一层：先把源库复制到私有暂存目录，再把**主文件 + WAL** 一起解密。

### 三条安全约定

1. **只读源库，从不写。** 任何轮询间隔都不会损坏微信数据。
2. **解密副本，不碰原件。** 撕裂窗口缩到拷贝上，Windows 共享冲突变成可捕获的 `PermissionError`（带重试）。
3. **`decrypt_database()` 返回 True 不等于快照有效。** 只有 page 1 有 HMAC 校验，其余页没有——调用方可再跑 `--check`。

### 各层职责

一个文件只有一个写者，**所以全流程不需要任何锁**：

| 脚本 | 职责 | 读写 |
|------|------|------|
| `live_probe.py` | 最便宜的一层：只解密 `session.db`（475 KB），看目标会话那一行变没变 | 读 |
| `live_decrypt.py` | 通用解密层：主文件 + WAL → 可读快照 | 读源库 / 写暂存 |
| `live_ingest.py` | 把源库里的新消息变成 `live_messages.jsonl` + `live_view.json` | **唯一的数据写者** |
| `live_daemon.py` | 常驻进程：探测 → 变了就摄取 → 写心跳 | 调度 |
| `live_serve.py` | 只读 HTTP 服务，默认 `127.0.0.1:8770` | 只读 |
| `live_annotate.py` | Claude 会话写语义标注（`annotations/*.json`） | 只写标注 |
| `live_replay.py` | 回放夹具，用来演示和自检 | 只写夹具 |

> **探测判定用 `last_msg_locald_id`（严格递增的整数），不用时间戳**——同秒内的两条消息时间戳可能相同。
>
> **守护进程不死于静默陈旧。** 每个 tick 都写 `daemon_status.json`；仪表盘看**心跳年龄**而不是 `phase`——被 SIGKILL 的进程来不及把 `phase` 改成 `stopped`，年龄是唯一可信的信号。

### 用法

```bash
# 一次性探测：目标会话有没有更新（退出码 0 = 探测成功，看输出里的 changed）
python scripts/live_probe.py --bundle data --json

# 单独解密某个库（带 WAL）
python scripts/live_decrypt.py --db session/session.db --out scripts/tmp/session.db --refresh --check

# 增量摄取
python scripts/live_ingest.py --bundle data --live data/live --json

# 常驻守护进程（默认 5 秒一个 tick）
python scripts/live_daemon.py --bundle data --live data/live
python scripts/live_daemon.py --once          # 跑一次就退出，测试用

# 打开仪表盘 → http://127.0.0.1:8770
python scripts/live_serve.py --bundle data --live data/live
```

语义标注（在 Claude Code 会话里）：

```bash
python scripts/live_annotate.py --index                       # 整体状态：待标注多少条
python scripts/live_annotate.py --claim --request-id <id>     # 认领
python scripts/live_annotate.py --write --request-id <id> --file ann.json
```

> ⚠️ **守护进程永不调 LLM。** 语义层不常驻、不是 API 调用——是你在会话里让 Claude 读一批消息、把判断写回磁盘。界面会把「待标注」显示成一等视觉状态，而不是一个转不完的圈。

> ℹ️ `live_track.py` 是更早的确定性追踪层，依赖一个**独立的 RL 仓库**（`--rl-root`，默认 `D:\she-love-me-rl`）和一个带 torch 的解释器。不属于主路径，可忽略。

---

## 工作原理

```
WeChat（运行中）/ NapCat + QCE（QQ）
    │
    │  微信：内存扫描提取密钥 → wechat-decrypt 解密数据库
    │  QQ：REST API 导出聊天记录
    ▼
标准 SQLite / JSON 消息数据
    │
    ├─► stats_analyzer.py → stats.json（全量统计：主动性 / 回复速度 / 语言学特征 / 可选情感层）
    │
    ├─► build_chat_history.py（用户动态选择分析范围）
    │       → chat_history.txt（分层采样：起源窗口 / 高冲突区间 / 近30天 / 修复时刻）
    ▼
AI Agent 深度分析（全量统计 + 分层采样关键窗口）
    │  Sternberg 三角（信号计数推导）· Gottman 正负比（词典 + 文本校正）
    │  对称性评分（stats.json 字段加权）· 双阈值危险预警
    │  对话动作分类 · 六维好感度 · 关键时刻时间线 · 回复评分
    │  依恋类型 · 核心恐惧 · 防御机制 · 军师建议 · 👴 祖师爷寄语
    ▼
analysis_contract.py 自检 ──► HTML 报告（暗色现代风格）+ Markdown 摘要
```

实时链路是上面这条管线的**增量分支**：

```
WeChat 源库（+ WAL）
    │  live_probe：只看 session.db 那一行变没变
    ▼
live_daemon ──► live_ingest ──► data/live/live_messages.jsonl
    │                                  │
    │                                  ▼
    │                            live_serve :8770 ──► 仪表盘（零 CDN，离线可用）
    │                                  ▲
    └── 心跳 daemon_status.json ────────┘
                                       │
                        live_annotate（Claude 会话写语义标注）
```

> 微信解密完全依赖 [ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt)，QQ 导出依赖 [shuakami/qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter)，本项目不包含任何解密算法本身。

---

## 项目结构

```
she-love-me-plus/
├── .agents/skills/she-love-me/
│   ├── SKILL.md                               # 唯一 Skill 入口（所有工具共用）
│   ├── agents/openai.yaml
│   └── references/                            # 知识库（SKILL.md 按需读取）
│       ├── analysis-framework.md              # 心理学框架（模块 F / A / B / H）
│       ├── intent-taxonomy.md                 # 12 情绪 / 36 意图 / 12 时刻 kind 受控词表
│       ├── scoring-rubrics.md                 # 六维好感度 + SSS–D 回复评分
│       ├── risk-signals.md                    # 危险预警 7 类信号 + 双阈值触发规则
│       ├── strategist-guide.md                # 军师（含 D6）/ 童锦程寄语 / 语气风格
│       ├── report-schema.md                   # analysis.json 结构 + 评分推导规则
│       ├── report-template.md                 # Step 9 Markdown 展示模板
│       ├── live-annotation-schema.md          # 实时语义标注层契约
│       ├── affect-schema.md                   # 可选情感计算层字段定义
│       └── affect-limits.md                   # 情感层能表征什么、不能表征什么
├── .claude/settings.json                      # Claude Code Skill 路径注册
├── references/tong-jincheng/                  # 祖师爷心智模型参考材料
├── scripts/
│   ├── setup_check.py                         # 环境检查 / 依赖准备 / 自动部署解密器
│   ├── decrypt_wechat.py                      # 微信解密入口（Windows Python 扫描 / macOS C 扫描器）
│   ├── list_contacts.py  list_contacts_qq.py  # 列出联系人
│   ├── extract_messages.py  extract_messages_qq.py
│   ├── extract_media.py                       # 从 hardlink.db 精确捞回图片，逐张解密
│   ├── contact_bundle.py                      # 统一生成联系人导出目录与各类默认路径
│   ├── export_emojis.py                       # 表情下载 / 清单 / 预览页
│   ├── lexicon.py                             # 情绪词 / 冲突词词表（零依赖）
│   ├── metrics_defs.py                        # 口径常量（会话切分 3h/6h/12h 等）
│   ├── stats_analyzer.py                      # 全量统计分析引擎
│   ├── build_chat_history.py                  # 分层采样：动态范围选择 + 关键窗口
│   ├── affect.py  affect_schema.py  affect_anchors.py      # 可选情感计算层
│   ├── semantic_features.py  semantic_anchors.py           # 语义轴特征
│   ├── embed_hf.py                            # 句向量模型下载（ONNX，非 torch）
│   ├── generate_html_report.py                # HTML 报告生成（微信/QQ 共用）
│   ├── analysis_contract.py                   # analysis.json 结构契约自检
│   ├── fts_fallback.py                        # FTS 兜底通道（主库拿不到密钥时的唯一来源）
│   ├── find_wechat_codec_offset.py            # 密钥定位辅助
│   └── live_*.py                              # 实时链路（probe / decrypt / ingest / daemon / serve / ui / annotate / replay / track）
├── vendor/                                    # wechat-decrypt（gitignore）
├── data/
│   ├── contacts/<联系人>__<hash>/             # 每个联系人的独立导出目录（gitignore）
│   └── live/                                  # 实时链路产物（gitignore）
└── reports/                                   # 生成的 HTML 报告（gitignore）
```

---

## 支持平台

| 平台 | 微信 | QQ | 备注 |
|------|------|-----|------|
| Windows | ✅ 支持 | ✅ 支持 | 微信需要管理员终端；QQ 无需管理员 |
| macOS | 🧪 实验性 | ✅ 支持 | 微信依赖上游 wechat-decrypt 与本机权限配置 |
| Linux | 🔜 规划中 | ✅ 支持 | QQ 通过 Docker NapCat 部署可用 |

> ⚠️ **微信 4.1.13+ 的密钥提取已失效**（实测 `enc_key` 在内存中 0 命中）。此时图片、纯表情、引用原文三块会缺——它们同源，都落在无密钥的 `message_1.db` 里。**文本分析不受影响**：`message_fts.db` 只索引文本，FTS 兜底通道照常工作。要拿回完整的图文需要 Frida hook（半天量级的工程）。

---

## 契约自检

`analysis.json` 由模型生成，以前出问题的方式是**静默掉格**：枚举槽被散文污染 → 渲染器精确匹配失败 → 悄悄落进灰色兜底，没人发现。

`analysis_contract.py` 把这类问题在生成阶段就暴露：

```bash
python scripts/analysis_contract.py data/contacts/<联系人>__<hash>/analysis.json
# 或自动读 data/analysis.json
python scripts/analysis_contract.py
```

它检查：顶层键缺失、显式 `null` 撞上渲染器的 `.get(k, {})`、进算术的槽不是数字、枚举越界、包装对象形态不一致、自由文本的条数与必备要素，以及 Plus 版新增的——**假精确分数**（`basis: "text"` 却给 `72`）、**`null` 分数配 `high` 证据等级**、**`basis: "quant"` 却没写口径**、**封顶已触发却没压到 25**、时刻 `confidence < 0.65`、回复评分缺 `context`。

`check_renderer_sync()` 还会直接读渲染器源码比对枚举字面量，防止"文档说七段、代码认五段"。

exit 0 才算过。

---

## 版本规划

- **v1.0**：文字消息分析 · HTML 报告 · 主动/被爱/冷淡指数
- **v2.0**：依恋类型诊断 · Sternberg 三角 · Gottman 四骑士 · 危险预警 · 军师模式
- **v2.1**：核心恐惧分析 · 情感可得性评估 · 权力动态量化 · 修复尝试分析 · 追逃循环复盘 · 止损红线
- **v2.2**：**QQ 聊天记录支持**（通过 QQ Chat Exporter API）· 微信/QQ 统一分析管线
- **v2.3**：👴 **祖师爷寄语**（童锦程视角）
- **v3.0**：🔄 品牌重构「她不一样」· 叙事框架升级
- **v3.1**：架构重构 · SKILL.md 控制平面拆分 · 分层采样引擎 · 评分推导规则 · 双阈值危险预警 · 可空字段设计
- **v3.2**：表情元信息导出 / 本地下载 / 预览页 · 图片按 `hardlink.db` 精确捞回
- **v3.3**（**Plus 版**）：🗣️ 对话动作分类学 · 📊 六维好感度（含边界封顶）· 🕐 关键时刻时间线 · 📝 SSS–D 回复评分 · 🧾 `analysis.json` 结构契约自检 · 📡 **实时链路**（带 WAL 的增量解密 + 本地仪表盘）

---

## 隐私与数据流

**聊天记录从不上传。** 解密、提取、统计、情感分析全部在本机完成。唯一可能联网的环节是**可选**的情感计算层下载模型权重——**它只下载，不上传**。

### 情感计算层（可选，默认关闭）

默认不启用。启用后（`--affect auto`）会从 HuggingFace 下载约 90MB 的句向量模型到 `vendor/hf_cache/`。

**⚠️ 供应链风险提示（请先读这条）**

- 默认从**官方** HuggingFace 拉取；直连不可达时自动回退 `hf-mirror.com` 镜像。
- `--hf-endpoint <url>` 允许你指定任意镜像。**非官方镜像的模型权重可信度由使用者自负**——镜像方若替换了权重，本工具无法察觉。
- 不想联网就加 `--no-download`，只用本地缓存；没有缓存时会跳过情感层（`stats.json` 里 `affect.status` 为 `disabled`），**主分析不受影响，仍然 exit 0**。

| 开关 | 行为 |
|------|------|
| （默认）不传 `--affect` | 完全离线，零下载 |
| `--affect auto` | 需要时下载模型（约 90MB）；聊天内容不上传 |
| `--affect auto --no-download` | 只用本地缓存，绝不联网 |
| `--affect auto --hf-endpoint <url>` | 走指定镜像，**权重可信度自负** |

### 实时链路的网络行为

`live_serve.py` 默认只绑 `127.0.0.1:8770`——**局域网内其他设备也访问不到**。仪表盘零 CDN（字体走系统栈、图表手写 SVG），断网可用。想换地址用 `--host` / `--port`。

### 留在本机的东西

解密密钥、`messages.json`、`stats.json`、`analysis.json`、`data/live/`、生成的 HTML 报告全部留在本机。`vendor/`、`data/`、`reports/`、`analysis/` 已 gitignore，不会被误提交。

### 消费 `stats.json` 时的一条硬规矩

`affect` 这个 key **永远存在**。不可用时它只是 `status` 不等于 `ok`（`disabled` = 没打算算；`degraded` = 试了但没成），**不是缺键**：

```python
affect = stats.get("affect", {})
if affect.get("status") == "ok":
    ...   # 用
else:
    ...   # 跳过，或显示「情感层未启用」+ affect.get("reason")
```

字段定义见 [`references/affect-schema.md`](.agents/skills/she-love-me/references/affect-schema.md)，能力边界见 [`references/affect-limits.md`](.agents/skills/she-love-me/references/affect-limits.md)。

---

## 致谢

> **[Reverie0123/crush-monitor-universal](https://github.com/Reverie0123/crush-monitor-universal)** — 12 情绪 / 36 意图分类学、六维好感度量表、SSS–D 回复评分、关键时刻 kind 与边界封顶规则的来源（MIT License © 2026 Crush Monitor contributors）。本项目**移植的是它的分类学骨架**，并做了两处改写：① 把六维加权求平均改为**证据分级 + 锚点标注**（因为本项目 `stats.json` 只对其中 1 维有干净的统计锚点，直接算分会产出假精确数字）；② 给四类关系敏感意图（试探好感 / 试探邀约 / 暧昧逗弄 / 寻求关注）加了强制的前置判定门槛。 🙏

> **[ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt)** — WeChat 4.0 数据库解密器，本项目微信能力的基础 🙏

> **[shuakami/qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter)** — NapCat + QCE 插件，QQ 聊天记录导出方案 🙏

> **[hotcoffeeshake/tong-jincheng-skill](https://github.com/hotcoffeeshake/tong-jincheng-skill)** — 祖师爷童锦程心智模型与语录整理 🙏

---

## 免责声明

本工具仅供娱乐，不构成情感建议。仅用于分析你自己的数据，请勿侵犯他人隐私。所有数据本地处理，不上传任何服务器。

---

<div align="center">

**MIT License © 2026 她不一样**

*如果这个项目帮你想通了什么，记得给个 ⭐*

</div>

> 曾用名：「她爱我吗？恋情分析室」· 前身：舔狗鉴定所
