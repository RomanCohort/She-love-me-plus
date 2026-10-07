"""
fts_fallback.py - 当消息分片（message_N.db）密钥不可得时，从全文索引补齐新消息。

背景
----
微信 4.1.11+ 起，数据库密钥不再以 `x'<64hex_enc_key><32hex_salt>'` 形式驻留
在进程内存中（改为 password + PBKDF2-HMAC-SHA512 派生），社区内存扫描方案失效。

后果之一是：微信会新建消息分片（message_1.db、message_2.db ...），而
`vendor/wechat-decrypt/all_keys.json` 里没有新分片的密钥 —— 因为
`main.py` 检测到密钥文件已存在就直接返回，永不重扫。于是解密环节静默跳过
新分片，用户以为导出完整，实际丢失最近的全部消息。

兜底思路
--------
`message_fts.db`（全文索引）与消息主库是独立加密的。在密钥轮换时它可能
仍保有可用密钥，且它索引了**所有**消息的正文。因此当新分片不可得时，
可以从 FTS 补齐这段时间的消息。

代价：FTS 不含非文本消息的完整元数据（图片/语音/表情只可能是占位或缺失），
但文字内容、时间戳、发送者都是完整且准确的。

ID 空间陷阱
-----------
⚠️ FTS 的 `session_id` / `sender_id` 与 `message_0.db` 的 `Name2Id` rowid
**不是同一套编号**。实测同一联系人：FTS 里 session_id=325，而 message_0.db
的 Name2Id 里 rowid=325 是另一个人（本联系人是 326）。

因此本模块不依赖任何 ID 映射，改用锚点匹配：拿已从主库提取出的消息
(local_id, create_time) 去比对各候选 session，命中率最高的即为本联系人。
这样无论 ID 空间如何变化都能自动对齐。
"""
import hashlib
import os
import re
import sqlite3

# FTS 分片表名形如 message_fts_v4_0 .. message_fts_v4_N
_FTS_TABLE_RE = re.compile(r"^message_fts_v\d+_\d+$")


def msg_key(sender, timestamp, content):
    """一条消息的稳定键。

    为什么不能用 local_id：本模块的 merge_into() 会给新记录重新编号
    local_id（已有记录的保持不变），所以同一条消息在不同轮次里可能有
    不同的 local_id。任何要跨轮次持久化的东西（去重、标注、进度）都必须
    用这个键，不能用 local_id。

    为什么不含 type：FTS 把 type 49 记成 "link"，而 extract_messages 的
    backfill_quotes() 之后同一条会变成 "quote"。把 type 放进键会让同一条
    消息算出两个键，去重失效。同一人、同一秒、同样的正文，就是同一条。
    """
    raw = "%s|%s|%s" % (int(timestamp or 0), sender or "", content or "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def msg_key_of(record):
    """对一条 messages.json 风格的记录算 msg_key。"""
    return msg_key(record.get("sender"), record.get("timestamp"), record.get("content"))

# 与 extract_messages.MSG_TYPE_MAP 对齐（FTS 只有 local_type，无压缩标志）
_FTS_TYPE_MAP = {
    1: "text",
    3: "image",
    34: "voice",
    42: "card",
    43: "video",
    47: "emoji",
    48: "location",
    49: "link",
    50: "call",
    10000: "system",
    10002: "revoke",
}


def _base_local_type(local_type):
    return local_type & 0xFFFFFFFF if local_type and local_type > 0xFFFFFFFF else local_type


def list_fts_tables(conn):
    """列出 FTS 库中的全部分片表。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return sorted(t for (t,) in rows if _FTS_TABLE_RE.match(t))


def open_fts(decrypted_dir=None, path=None):
    """打开 message_fts.db；不存在或无法打开时返回 None。

    path 用于指向一份私有的、打过 WAL 补丁的副本 —— 实时链路走这条，
    因为 vendor 的解密器只读主文件、不读 -wal（实测会落后一整个 WAL）。
    """
    if path is None:
        if not decrypted_dir:
            return None
        path = os.path.join(decrypted_dir, "message", "message_fts.db")
    if not os.path.exists(path):
        return None
    try:
        return sqlite3.connect(path)
    except sqlite3.Error:
        return None


def _iter_rows(conn, tables, session_id):
    for t in tables:
        yield from conn.execute(
            f"SELECT message_local_id, create_time, sender_id, local_type, acontent "
            f"FROM [{t}] WHERE session_id=?",
            (session_id,),
        )


def candidate_sessions(conn, tables):
    """返回 FTS 中出现过的全部 session_id。"""
    seen = set()
    for t in tables:
        for (sid,) in conn.execute(f"SELECT DISTINCT session_id FROM [{t}]"):
            seen.add(sid)
    return sorted(seen)


def _clean(text):
    """FTS 的 acontent 含 \\x08 之类的分隔控制字符，清理掉。"""
    return str(text).replace("\x08", "").strip()


def identify_session(conn, tables, known_messages, min_hits=3):
    """用 (local_id, create_time) 锚点匹配，找出已知消息所属的 FTS session。

    Args:
        known_messages: 形如 [{"local_id": int, "timestamp": int, "sender": "me"/"them"}, ...]

    Returns:
        (session_id, me_sender_id, them_sender_id) 或 None
    """
    if not known_messages:
        return None
    anchors = {}
    for m in known_messages:
        lid, ts = m.get("local_id"), m.get("timestamp")
        if lid is None or ts is None:
            continue
        anchors[(int(lid), int(ts))] = m.get("sender")

    best = None
    for sid in candidate_sessions(conn, tables):
        hits = 0
        me_votes, them_votes = {}, {}
        for mlid, ctime, sender_id, _lt, _c in _iter_rows(conn, tables, sid):
            want = anchors.get((int(mlid), int(ctime))) if mlid is not None and ctime is not None else None
            if want is None:
                continue
            hits += 1
            (me_votes if want == "me" else them_votes)[sender_id] = \
                (me_votes if want == "me" else them_votes).get(sender_id, 0) + 1
        if hits and (best is None or hits > best[1]):
            best = (sid, hits, me_votes, them_votes)

    if not best or best[1] < min_hits:
        return None
    _sid, hits, me_votes, them_votes = best
    me_id = max(me_votes, key=me_votes.get) if me_votes else None
    them_id = max(them_votes, key=them_votes.get) if them_votes else None
    return best[0], me_id, them_id, hits


def extract_since(conn, tables, session_id, me_sender_id, them_sender_id, since_ts):
    """提取 since_ts 及之后的消息，转换为与 messages.json 一致的记录格式。

    边界取 >=（即只跳过 `create_time < since_ts`）：同秒内的两条消息时间戳
    可能相同，用 > 会把边界那条永久漏掉。重复由 merge_into 的 msg_key 去重兜住，
    所以放宽边界是安全的。

    文本为空的行走（图片 / 纯表情 / 语音）不再丢弃，标 text_missing=True。
    它们的 acontent 只有 \\x08 之类的分隔符。丢掉会让轮流、回复节奏这类
    按条数算的统计错位 —— 而且实时链路里 FTS 是唯一来源，丢一条就是永久丢。
    内容确实拿不到，所以标出来让下游别去解读它。
    """
    rows = []
    for mlid, ctime, sender_id, local_type, content in _iter_rows(conn, tables, session_id):
        if ctime is None or int(ctime) < since_ts:
            continue
        if sender_id == me_sender_id:
            sender = "me"
        elif sender_id == them_sender_id:
            sender = "them"
        else:
            continue
        text = _clean(content)
        base = _base_local_type(local_type)
        record = {
            "local_id": int(mlid) if mlid is not None else None,
            "sender": sender,
            "content": text,
            "timestamp": int(ctime),
            "type": _FTS_TYPE_MAP.get(base, "other"),
            "local_type": base,
            "source": "message_fts",
        }
        if not text:
            record["text_missing"] = True
        rows.append(record)
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def recover_missing(decrypted_dir=None, known_messages=None, verbose=True, path=None):
    """高层入口：从 FTS 补齐已知消息之后的部分。

    path 指向一份打过 WAL 补丁的私有副本时优先用它（实时链路）。

    Returns:
        dict: {
            "ok": bool,
            "reason": str,               # ok=False 时说明原因
            "session_id": int | None,
            "match_hits": int,           # 锚点命中数，越高越可信
            "messages": [...],           # 可直接追加到 messages.json 的记录
        }
    """
    known_messages = known_messages or []
    result = {"ok": False, "reason": "", "session_id": None,
              "match_hits": 0, "messages": []}

    conn = open_fts(decrypted_dir, path=path)
    if conn is None:
        result["reason"] = "message_fts.db 不存在或无法打开"
        return result

    try:
        tables = list_fts_tables(conn)
        if not tables:
            result["reason"] = "message_fts.db 中没有 FTS 分片表"
            return result

        ident = identify_session(conn, tables, known_messages)
        if not ident:
            result["reason"] = "锚点匹配失败，无法在 FTS 中定位该联系人"
            return result

        session_id, me_id, them_id, hits = ident
        result["session_id"] = session_id
        result["match_hits"] = hits

        if me_id is None or them_id is None:
            result["reason"] = "锚点不足，无法确定双方 sender_id"
            return result

        last_ts = max((m.get("timestamp", 0) for m in known_messages), default=0)
        msgs = extract_since(conn, tables, session_id, me_id, them_id, last_ts)
        result["ok"] = True
        result["messages"] = msgs
        if verbose:
            print(f"[*] FTS 兜底: session_id={session_id} 锚点命中 {hits} 条, "
                  f"补出 {len(msgs)} 条新消息")
        return result
    finally:
        conn.close()


def merge_into(messages, new_records):
    """把 FTS 记录追加进 messages 列表并去重。

    去重键是 msg_key，不是 (local_id, timestamp)。原因：本函数会给新记录
    重新编号 local_id，于是键里的 local_id 与 FTS 原始的 mlid 不在同一个
    空间 —— 同一批记录再投递一次时键完全不同，会被当成新消息重复追加。
    原来的写法只在「几乎不会重复投递」时侥幸成立；实时链路每几秒投一次，
    必然会撞上。

    已有消息的 local_id 保持不变（emojis.json 里的 first_local_id 依赖它），
    新记录从当前最大 local_id 继续编号。

    msg_key 只用来判重，不写回记录 —— messages.json 的字段集合保持不变，
    键由 msg_key_of() 现算。存进去反而会造出一份半新半旧的格式（只有
    FTS 来的那批带键），而且把公式变更变成了一次数据迁移。
    """
    seen = {msg_key_of(m) for m in messages}
    merged = list(messages)
    next_id = max((m.get("local_id") or 0 for m in messages), default=0) + 1
    added = []
    for r in sorted(new_records, key=lambda x: x.get("timestamp") or 0):
        if msg_key_of(r) in seen:
            continue
        seen.add(msg_key_of(r))
        r = dict(r)
        r["local_id"] = next_id
        next_id += 1
        added.append(r)
    merged.extend(added)
    return merged
