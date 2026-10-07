"""
extract_messages.py - 提取指定联系人的全部消息，并生成独立联系人目录中的 messages.json / emojis.json

消息存储结构（WeChat 4.0）：
  - message/message_N.db -> 每个联系人有独立的 Msg_{md5(username)} 表
  - 列：local_id, local_type, create_time, real_sender_id, message_content, WCDB_CT_message_content
  - real_sender_id 通过 Name2Id 表解析为 username
  - WCDB_CT_message_content == 4 表示 zstd 压缩
  - local_type == 1 文本, 3 图片, 34 语音, 43 视频, 47 表情, 49 链接/文件, 50 通话, 10000 系统

导出结构：
  - messages.json：聊天消息主体；表情消息只保留 emoji_ref
  - emojis.json：独立表情目录；保存 emoji_ref 对应的 md5 / cdnurl / len 等元信息
"""
import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from xml.etree import ElementTree as ET

from contact_bundle import resolve_bundle_paths
from fts_fallback import recover_missing as recover_from_fts, merge_into

# Windows 控制台 UTF-8 输出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import zstandard as zstd
    _zstd_dctx = zstd.ZstdDecompressor()
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False

MSG_TYPE_MAP = {
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


def decompress_content(content, ct):
    if ct == 4 and isinstance(content, bytes) and HAS_ZSTD:
        try:
            return _zstd_dctx.decompress(content).decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return None
    return content


def get_msg_type(local_type):
    base = local_type & 0xFFFFFFFF if local_type > 0xFFFFFFFF else local_type
    return MSG_TYPE_MAP.get(base, "other")


def get_base_local_type(local_type):
    return local_type & 0xFFFFFFFF if local_type > 0xFFFFFFFF else local_type


def parse_emoji_metadata(content):
    """从表情消息 XML 中提取元信息。"""
    if not content or not isinstance(content, str) or "<emoji" not in content:
        return None
    try:
        root = ET.fromstring(content)
        emoji_node = root.find("emoji")
        if emoji_node is None:
            return None
        keep_fields = [
            "type", "md5", "len", "cdnurl", "thumburl", "encrypturl",
            "aeskey", "productid", "designerid", "thumbmd5",
            "androidmd5", "androidlen", "fromusername", "tousername",
            "externurl", "externmd5",
        ]
        meta = {}
        for key in keep_fields:
            value = emoji_node.attrib.get(key)
            if value not in (None, ""):
                meta[key] = html.unescape(value)
        return meta or None
    except Exception:
        return None


def build_emoji_id(meta, local_id):
    if meta and meta.get("md5"):
        return f"emoji_{meta['md5']}"
    return f"emoji_local_{local_id}"


def resolve_sender(username, own_wxid, contact_username):
    if own_wxid and username == own_wxid:
        return "me"
    if username == contact_username:
        return "them"
    return "unknown"


def summarize_quoted(content, qtype):
    """把被引消息渲染成一行。被引的若是图片/表情，其 content 是转义过的 XML。"""
    if qtype == 3:
        return "[图片]"
    if qtype == 47:
        return "[表情]"
    if qtype == 43:
        return "[视频]"
    if qtype == 34:
        return "[语音]"
    if not content:
        return ""
    text = content.strip()
    if text.startswith("<?xml") or text.startswith("<msg") or "<msgsource" in text:
        if "<img" in text:
            return "[图片]"
        if "<emoji" in text:
            return "[表情]"
        return "[媒体消息]"
    return text.replace("\n", " ").strip()


def parse_quote_metadata(content):
    """解析 appmsg 的 <refermsg>，还原被引消息。

    微信的引用回复（local_type 基值 49）在 message_*.db 里存的是 appmsg XML：

        <appmsg>
          <title>回复的正文</title>
          <refermsg>
            <content>被引原文</content>
            <fromusr>被引消息发送者的 wxid</fromusr>
            <createtime>被引消息的时间戳</createtime>
            <displayname>对方**当时**的微信昵称</displayname>
            <type>被引消息类型</type>
          </refermsg>
        </appmsg>

    ⚠️ 基值 49 是 appmsg 容器，引用回复 / 文件 / 卡片 / 拍一拍都落在里面，
    不能只看类型码——**`<refermsg>` 才是引用回复**。

    ⚠️ `displayname` 是**当时**的昵称，通常和现在的备注名不同，这是它最有价值的地方。

    返回 {"reply_text": str, "quote": {...}}，不是引用回复则返回 None。
    """
    if not content or not isinstance(content, str) or "<refermsg>" not in content:
        return None
    try:
        root = ET.fromstring(content)
    except Exception:
        return None
    ref = root.find(".//refermsg")
    if ref is None:
        return None

    def _text(tag):
        node = ref.find(tag)
        return node.text if node is not None and node.text else None

    title_node = root.find(".//appmsg/title")
    reply_text = title_node.text if title_node is not None and title_node.text else ""

    ts = _text("createtime")
    qt = _text("type")
    return {
        "reply_text": reply_text,
        "quote": {
            "content": _text("content") or "",
            "sender_username": _text("fromusr"),
            "display_name": _text("displayname"),
            "timestamp": int(ts) if ts and ts.strip().isdigit() else None,
            "type": int(qt) if qt and qt.strip().isdigit() else None,
            "source": "refermsg",
        },
    }


def backfill_quotes(messages, max_gap=6 * 3600):
    """给「有引用回复但拿不到被引原文」的条目补一个**推断**值。

    为什么需要：09-11 之后的消息只来自 FTS，而 FTS 只存「回复正文 + \\x08」——
    被引原文不在库里（message_1.db 没有密钥）。只能按「最近一条对方发送的消息」
    重建。**这是推断，不是事实**，结果带 source="inferred" 与 basis 显式标注。

    返回补了多少条。
    """
    filled = 0
    for m in messages:
        # 只补 FTS 来的记录：主库（message_*.db）能拿到 appmsg XML，
        # 有没有 <refermsg> 是确定的，不需要猜。
        if m.get("source") != "message_fts":
            continue
        if m.get("local_type") != 49 or m.get("quote"):
            continue
        text = (m.get("content") or "").strip()
        # 基值 49 是 appmsg 容器，拍一拍 / 文件 / 卡片都落在里面，排除掉
        if re.search(r"\.(zip|docx?|xlsx?|pptx?|pdf|bat|exe|rar|7z|txt|csv)$", text, re.I):
            continue
        if "拍了拍" in text or "撤回" in text or "邀请你加入" in text:
            continue
        ts = m.get("timestamp") or 0
        best = None
        for other in messages:
            if other is m:
                continue
            ots = other.get("timestamp") or 0
            if ots >= ts or ts - ots > max_gap:
                continue
            if other.get("sender") == m.get("sender"):
                continue
            if best is None or ots > (best.get("timestamp") or 0):
                best = other
        if best is None:
            continue
        m["type"] = "quote"
        m["quote"] = {
            "content": best.get("content"),
            "sender": best.get("sender"),
            "timestamp": best.get("timestamp"),
            "display_name": None,
            "type": None,
            "source": "inferred",
            "gap_seconds": ts - (best.get("timestamp") or 0),
            "basis": "FTS 不存被引原文；取最近一条对方消息（间隔 %d 秒）——推断，非事实"
                     % (ts - (best.get("timestamp") or 0)),
        }
        filled += 1
    return filled


def load_contacts(decrypted_dir):
    contact_db = os.path.join(decrypted_dir, "contact", "contact.db")
    names = {}
    if not os.path.exists(contact_db):
        return names
    conn = sqlite3.connect(contact_db)
    try:
        for row in conn.execute("SELECT username, nick_name, remark FROM contact").fetchall():
            uname, nick, remark = row
            names[uname] = remark or nick or uname
    finally:
        conn.close()
    return names


def find_username(contact_query, names):
    """模糊匹配联系人名字，返回 username"""
    q = contact_query.strip().lower()
    # 精确匹配 username
    if contact_query in names:
        return contact_query
    # 精确匹配 display_name
    for uname, display in names.items():
        if q == display.lower():
            return uname
    # 模糊匹配
    for uname, display in names.items():
        if q in display.lower() or q in uname.lower():
            return uname
    return None


def get_own_wxid(decrypted_dir):
    """从 config.json 推断自己的 wxid，兼容 Windows / macOS 路径"""
    # 尝试从 vendor/wechat-decrypt/config.json 读取
    config_candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "vendor", "wechat-decrypt", "config.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "vendor", "wechat-decrypt", "config.json"),
    ]
    for cfg_path in config_candidates:
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    cfg = json.load(f)
                if cfg.get("wxid"):
                    return cfg["wxid"]
                db_dir = cfg.get("db_dir", "")
                if db_dir:
                    parts = [p for p in os.path.normpath(db_dir).split(os.sep) if p]
                    if "db_storage" in parts:
                        idx = len(parts) - 1 - parts[::-1].index("db_storage")
                        if idx > 0:
                            candidate = parts[idx - 1]
                            # 目录名形如 wxid_xxx_4946，尾部 _<数字> 是安装序号，
                            # 不是 wxid 的一部分——带上它会让 own_wxid 永远匹配不上。
                            return re.sub(r"_\d+$", "", candidate)
            except Exception:
                pass
    return None


def extract_messages_from_db(db_path, table_name, id_to_username, own_wxid, contact_username, contact_display):
    messages = []
    emoji_records = {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT local_id, local_type, create_time, real_sender_id, "
                f"message_content, WCDB_CT_message_content "
                f"FROM [{table_name}] ORDER BY create_time ASC"
            ).fetchall()

            last_known_sender = None  # 用于 revoke 消息回溯
            for row in rows:
                local_id, local_type, create_time, real_sender_id, content, ct = row
                content = decompress_content(content, ct)
                msg_type = get_msg_type(local_type)
                base_local_type = get_base_local_type(local_type)
                emoji_meta = parse_emoji_metadata(content) if msg_type == "emoji" else None
                # 基值 49 是 appmsg 容器（引用回复 / 文件 / 卡片 / 拍一拍都在里面）。
                # 靠内容判别：**`<refermsg>` 才是引用回复**，不能只看类型码。
                quote_meta = parse_quote_metadata(content) if base_local_type == 49 else None
                if quote_meta:
                    msg_type = "quote"

                # 判断发送方
                sender_username = id_to_username.get(real_sender_id, "")
                if own_wxid and sender_username == own_wxid:
                    sender = "me"
                elif sender_username == contact_username:
                    sender = "them"
                elif msg_type == "revoke" and last_known_sender:
                    # revoke 消息 real_sender_id 通常为 0，回溯上一条消息的 sender
                    sender = last_known_sender
                elif own_wxid and not sender_username:
                    sender = "unknown"
                else:
                    sender = "them" if sender_username == contact_username else "me"

                if sender in ("me", "them"):
                    last_known_sender = sender

                # 处理内容
                display_content = content or ""
                if msg_type == "text" and display_content and ":\n" in display_content:
                    # 群消息格式，只取内容部分（此处处理单聊，理论上不会有这种格式）
                    display_content = display_content.split(":\n", 1)[-1]
                elif msg_type == "image":
                    display_content = "[图片]"
                elif msg_type == "voice":
                    display_content = "[语音消息]"
                elif msg_type == "video":
                    display_content = "[视频]"
                elif msg_type == "emoji":
                    display_content = "[表情]"
                elif msg_type == "call":
                    display_content = "[通话]"
                elif msg_type == "revoke":
                    display_content = "[撤回了一条消息]"
                elif msg_type == "system":
                    display_content = f"[系统消息] {display_content}"
                elif msg_type == "quote":
                    display_content = quote_meta["reply_text"] or "[引用]"
                elif msg_type == "link":
                    # 尝试提取链接标题
                    if display_content and "<title>" in display_content:
                        import re as _re
                        m = _re.search(r"<title>(.*?)</title>", display_content)
                        display_content = f"[链接] {m.group(1)}" if m else "[链接]"
                    else:
                        display_content = "[链接/文件]"

                record = {
                    "local_id": local_id,
                    "sender": sender,
                    "content": display_content,
                    "timestamp": create_time,
                    "type": msg_type,
                    "local_type": base_local_type,
                }
                if quote_meta:
                    q = dict(quote_meta["quote"])
                    q["sender"] = resolve_sender(q.pop("sender_username", None),
                                                 own_wxid, contact_username)
                    q["content"] = summarize_quoted(q.get("content"), q.get("type"))
                    record["quote"] = q
                if emoji_meta:
                    emoji_id = build_emoji_id(emoji_meta, local_id)
                    record["emoji_ref"] = emoji_id
                    existing = emoji_records.get(emoji_id)
                    if not existing:
                        emoji_records[emoji_id] = {
                            "emoji_id": emoji_id,
                            "md5": emoji_meta.get("md5"),
                            "type": emoji_meta.get("type"),
                            "len": emoji_meta.get("len"),
                            "cdnurl": emoji_meta.get("cdnurl"),
                            "thumburl": emoji_meta.get("thumburl"),
                            "encrypturl": emoji_meta.get("encrypturl"),
                            "aeskey": emoji_meta.get("aeskey"),
                            "productid": emoji_meta.get("productid"),
                            "designerid": emoji_meta.get("designerid"),
                            "thumbmd5": emoji_meta.get("thumbmd5"),
                            "androidmd5": emoji_meta.get("androidmd5"),
                            "androidlen": emoji_meta.get("androidlen"),
                            "fromusername": emoji_meta.get("fromusername"),
                            "tousername": emoji_meta.get("tousername"),
                            "externurl": emoji_meta.get("externurl"),
                            "externmd5": emoji_meta.get("externmd5"),
                            "first_local_id": local_id,
                            "first_timestamp": create_time,
                            "occurrence_count": 1,
                        }
                    else:
                        existing["occurrence_count"] = existing.get("occurrence_count", 0) + 1
                messages.append(record)
        finally:
            conn.close()
    except Exception as e:
        print(f"[!] 读取 {db_path} 失败: {e}", file=sys.stderr)

    return messages, emoji_records


def main():
    parser = argparse.ArgumentParser(description="提取指定联系人的微信消息")
    parser.add_argument("--decrypted-dir", required=True)
    parser.add_argument("--contact", required=True, help="联系人名字（备注名/昵称/wxid）")
    parser.add_argument("--output", help="输出 messages.json 文件路径；不传时可配合 --output-dir 使用")
    parser.add_argument("--output-dir", help="按联系人自动创建导出目录，例如 data/contacts")
    parser.add_argument("--no-fts-fallback", action="store_true",
                        help="禁用 FTS 兜底（默认开启：当消息分片密钥不可得时，"
                             "从 message_fts.db 补齐其后的消息）")
    args = parser.parse_args()

    if not args.output and not args.output_dir:
        parser.error("必须提供 --output 或 --output-dir 其中之一")

    decrypted_dir = os.path.abspath(args.decrypted_dir)
    names = load_contacts(decrypted_dir)
    if not names:
        print(json.dumps({"error": "无法加载联系人数据"}))
        sys.exit(1)

    contact_username = find_username(args.contact, names)
    if not contact_username:
        print(json.dumps({"error": f"找不到联系人: {args.contact}"}))
        sys.exit(1)

    contact_display = names.get(contact_username, contact_username)
    own_wxid = get_own_wxid(decrypted_dir)
    table_hash = hashlib.md5(contact_username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"

    msg_dir = os.path.join(decrypted_dir, "message")
    if not os.path.exists(msg_dir):
        print(json.dumps({"error": f"消息目录不存在: {msg_dir}"}))
        sys.exit(1)

    msg_dbs = sorted([
        f for f in os.listdir(msg_dir)
        if re.match(r"message_\d+\.db$", f)
    ])

    bundle = resolve_bundle_paths(
        contact_display=contact_display,
        contact_username=contact_username,
        output=args.output,
        output_dir=args.output_dir,
    )

    all_messages = []
    emoji_catalog = {}
    for db_file in msg_dbs:
        db_path = os.path.join(msg_dir, db_file)
        conn = sqlite3.connect(db_path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if not exists:
                conn.close()
                continue

            id_to_username = {}
            try:
                for rowid, user_name in conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall():
                    if user_name:
                        id_to_username[rowid] = user_name
            except sqlite3.OperationalError:
                pass
            conn.close()

            msgs, emojis = extract_messages_from_db(db_path, table_name, id_to_username, own_wxid,
                                                    contact_username, contact_display)
            all_messages.extend(msgs)
            for emoji_id, meta in emojis.items():
                if emoji_id not in emoji_catalog:
                    emoji_catalog[emoji_id] = meta
                else:
                    emoji_catalog[emoji_id]["occurrence_count"] = (
                        emoji_catalog[emoji_id].get("occurrence_count", 0)
                        + meta.get("occurrence_count", 0)
                    )
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    # 按时间排序
    all_messages.sort(key=lambda m: m["timestamp"])

    # FTS 兜底：微信 4.1.11+ 新建的消息分片拿不到密钥时，主库提取会静默跳过，
    # 导致最近的消息整体缺失。此处从全文索引补齐。
    fts_info = {"used": False, "recovered": 0}
    if not args.no_fts_fallback and all_messages:
        try:
            fb = recover_from_fts(decrypted_dir, all_messages)
            fts_info = {
                "used": bool(fb.get("ok")),
                "recovered": len(fb.get("messages", [])),
                "session_id": fb.get("session_id"),
                "match_hits": fb.get("match_hits", 0),
                "reason": fb.get("reason", ""),
            }
            if fb.get("ok") and fb["messages"]:
                all_messages = merge_into(all_messages, fb["messages"])
        except Exception as exc:  # 兜底失败不应中断主流程
            fts_info = {"used": False, "recovered": 0, "error": str(exc)}
            print(f"[!] FTS 兜底失败（不影响主流程）: {exc}", file=sys.stderr)

    # 引用回复兜底：09-11 之后的消息只来自 FTS，FTS 只存「回复正文 + \x08」，
    # 被引原文不在库里。按「最近一条对方消息」重建——**推断，非事实**。
    inferred_quotes = 0
    try:
        inferred_quotes = backfill_quotes(all_messages)
    except Exception as exc:
        print(f"[!] 引用回复兜底失败（不影响主流程）: {exc}", file=sys.stderr)
    fact_quotes = sum(1 for m in all_messages if (m.get("quote") or {}).get("source") == "refermsg")
    if fact_quotes or inferred_quotes:
        print(f"[*] 引用回复：{fact_quotes} 条原文可还原（refermsg），"
              f"{inferred_quotes} 条为推断", file=sys.stderr)

    result = {
        "contact_username": contact_username,
        "contact_display": contact_display,
        "own_wxid": own_wxid or "unknown",
        "bundle_dir": bundle["bundle_dir"],
        "total": len(all_messages),
        "emoji_total": sum(1 for m in all_messages if m.get("type") == "emoji"),
        "emoji_catalog_file": os.path.basename(bundle["emojis_path"]),
        "fts_fallback": fts_info,
        "messages": all_messages,
    }

    emoji_result = {
        "contact_username": contact_username,
        "contact_display": contact_display,
        "bundle_dir": bundle["bundle_dir"],
        "total_messages": result["emoji_total"],
        "unique_emojis": len(emoji_catalog),
        "emoji_records": sorted(
            emoji_catalog.values(),
            key=lambda x: (x.get("first_timestamp", 0), x.get("emoji_id", "")),
        ),
    }

    os.makedirs(bundle["bundle_dir"], exist_ok=True)
    with open(bundle["messages_path"], "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(bundle["emojis_path"], "w", encoding="utf-8") as f:
        json.dump(emoji_result, f, ensure_ascii=False, indent=2)

    print(f"[+] 已提取 {len(all_messages)} 条消息 -> {bundle['messages_path']}", file=sys.stderr)
    print(f"[+] 已导出 {result['emoji_total']} 条表情消息 -> {bundle['emojis_path']}", file=sys.stderr)
    if fts_info.get("recovered"):
        print(f"[+] FTS 兜底补出 {fts_info['recovered']} 条新消息 "
              f"(session_id={fts_info.get('session_id')}, "
              f"锚点命中 {fts_info.get('match_hits')} 条)", file=sys.stderr)
    print(json.dumps({
        "status": "ok",
        "total": len(all_messages),
        "emoji_total": result["emoji_total"],
        "contact": contact_display,
        "bundle_dir": bundle["bundle_dir"],
        "messages_path": bundle["messages_path"],
        "emojis_path": bundle["emojis_path"],
        "fts_fallback": fts_info,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
