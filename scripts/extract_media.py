"""
extract_media.py - 提取指定联系人的图片与表情包，产出可直接查看的媒体目录与索引

为什么需要它
------------
微信 4.1.11+ 之后 `message_1.db` 拿不到密钥、`message_fts.db` 只索引文本，于是
09-11 之后的图片和表情包在 messages.json 里只剩 `[图片]` / `[表情]` 占位符。
但**图片文件本身仍在硬盘上**，且 `hardlink.db` 里存着完整的「聊天 → 文件」索引。

定位方式（不扫盘）
------------------
`hardlink.db` 的 `dir2id(username)` 是单列表，靠隐式 rowid 索引，而**两个层级共用
同一个 rowid 空间**：
  - `image_hardlink_info_v4.dir1` → `dir2id.username` = md5(contact_username)  (chat hash)
  - `image_hardlink_info_v4.dir2` → `dir2id.username` = "YYYY-MM"               (月份)
这是 O(1) 精确命中，比现有代码「md5(username) 猜目录名 + 全量 glob 扫盘」快得多。

⚠️ 磁盘文件名是 `file_name` 列，**不是** `md5` 列。实测 `file_name` 与磁盘 basename
   交集 97.5%，而 `md5` 列交集为 **0%**。

路径模板
--------
  type=2 普通图片    <wechat_base>/msg/attach/{chat_hash}/{month}/Img/{file_name}
  type=4 收到的表情  <wechat_base>/msg/attach/{chat_hash}/{month}/Rec/{extra_buffer.field1}/Img/{file_name}
`extra_buffer` 是 protobuf，field 1 是 Rec 下的子目录名。

解密
----
复用 `vendor/wechat-decrypt/decode_image.decrypt_dat_file`。选它而非
`batch_decrypt_images.decrypt_dat`：它接受 key 参数，后者从模块常量读、不可传参。

⚠️ 但它的**尾部校验会造假故障**（jpg 须以 FF D9 收尾、png 末尾须含 IEND）。
实测（2026-09-30）两张图 AES 段完全正确、主体 PIL 可完整 load，仅因尾部不是
FF D9 就被判成 `decrypt_failed`。**加了 `--lenient` 后这类图会以
`decoded_unverified` 落盘**（图可用，但尾部没有通过 magic 校验，需人工过目）。

反过来，尾部校验挡的假阳性是真的存在——**「XOR key 错但 AES 段恰好凑出合法
magic」**。所以 `--lenient` 只该在严格模式报错后**逐张**兜底，不要拿来当默认：
对 12KB 缩略图遍历 256 个 XOR key，有 17–22 个都能让 PIL 成功 `load()` 且尺寸
一致（JPEG 头在 AES 段、与 XOR 无关，PIL 对截断流宽容）。**判 XOR key 只能靠
「结尾是否 FF D9」或人工看图，不能靠「PIL 能打开」。**

产出
----
  data/media/<slug>/<YYYYmmdd_HHMMSS>_<base8>.<ext>
  data/media/<slug>/media_index.json

⚠️ 09-11 之后没有消息行，**无法确知图片的发送者**。索引里的 `sender_hint` 是推断
   （依据最近前一条消息的发送者与间隔），带 `basis` 字段显式标注，不得当作事实引用。
   与图片消息时间戳能对上的（09-11 之前）才写 `linked_message`，那才是真值。
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys

# Windows 控制台 UTF-8 输出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from contact_bundle import resolve_bundle_paths  # noqa: E402
from extract_messages import find_username, load_contacts  # noqa: E402

# 图片消息与磁盘文件的时间戳容差：两者都来自微信自己写入，通常差几秒。
# 取得太紧会漏配，太松会错配。
_IMAGE_MSG_TOLERANCE = 120

# 推断发送者时允许的最大间隔；超过就放弃推断（不猜）。
_SENDER_HINT_MAX_GAP = 600

# 同一张图常有多个变体，按质量降序；空后缀 = 原图。
# 对齐 vendor/wechat-decrypt/export_messages.py 的既有约定。
_VARIANT_PRIORITY = {"": 0, "_W": 1, "_h": 2}


def lenient_decrypt_bytes(dat_path, aes_key, xor_key):
    """宽松解密：跳过 decode_image 的尾部 magic 校验，返回 (blob, fmt, reason)。

    只为兜住「主体完全可解、但尾部不是 FF D9 / 不含 IEND」的好图——严格模式
    (`decrypt_dat_file`) 会把这类判成 decrypt_failed。**校验被跳过了，所以调用方
    必须把结果标成 unverified，不能当 `ok`。**

    blob 为 None 时 reason 说明卡在哪一步：
      not_v2 / no_key / truncated_aes / unpad_failed / unknown_format
    成功时 reason 为 "ok"（尾部恰好合规）或 "tail_magic_mismatch"（跳过了校验）。
    """
    import struct
    import decode_image as di
    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    with open(dat_path, "rb") as f:
        data = f.read()
    if len(data) < 15 or data[:6] not in (di.V2_MAGIC_FULL, di.V1_MAGIC_FULL):
        return None, None, "not_v2"

    if not aes_key:
        return None, None, "no_key"
    if isinstance(aes_key, str):
        aes_key = aes_key.encode("ascii")[:16]
    if isinstance(xor_key, str):
        xor_key = int(xor_key, 0)
    if len(aes_key) < 16:
        return None, None, "no_key"
    # V1 用固定 key，与 v2_decrypt_file 保持一致
    if data[:6] == di.V1_MAGIC_FULL:
        aes_key = b"cfcd208495d565ef"

    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    aligned = di.aligned_aes_block_size(aes_size)
    offset = 15
    if offset + aligned > len(data):
        return None, None, "truncated_aes"

    try:
        cipher = AES.new(aes_key[:16], AES.MODE_ECB)
        dec_aes = Padding.unpad(cipher.decrypt(data[offset:offset + aligned]), AES.block_size)
    except (ValueError, KeyError):
        return None, None, "unpad_failed"
    offset += aligned

    raw_end = len(data) - xor_size
    raw = data[offset:raw_end] if offset < raw_end else b""
    blob = dec_aes + raw + bytes(b ^ xor_key for b in data[raw_end:])

    fmt = di.detect_image_format(blob[:16])
    if blob[:4] == b"wxgf":
        fmt = "hevc"
    elif fmt == "bin":
        # magic 不匹配任何已知格式 = AES key 错，解出来是随机字节。这条要保留，
        # 它是唯一能挡「key 错但凑出合法头」的判据。
        return None, None, "unknown_format"

    if fmt == "jpg" and blob[-2:] != b"\xff\xd9":
        return blob, fmt, "tail_magic_mismatch"
    if fmt == "png" and b"IEND" not in blob[-12:]:
        return blob, fmt, "tail_magic_mismatch"
    return blob, fmt, "ok"


def load_vendor_config(vendor_dir):
    """读 vendor 的 config.json，并推导 wechat_base_dir。

    `wechat_base_dir` 不在 config.json 里——它由 vendor 的 config.py 从 db_dir
    推导（db_dir = <base>/db_storage）。这里做同样的推导，避免依赖其内部模块。
    """
    cfg_path = os.path.join(vendor_dir, "config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as exc:
            print(f"[!] 读取 {cfg_path} 失败: {exc}", file=sys.stderr)

    base_dir = cfg.get("wechat_base_dir")
    if not base_dir:
        db_dir = cfg.get("db_dir", "")
        if db_dir:
            parts = [p for p in os.path.normpath(db_dir).split(os.sep) if p]
            if "db_storage" in parts:
                idx = len(parts) - 1 - parts[::-1].index("db_storage")
                if idx > 0:
                    base_dir = os.sep.join(parts[:idx])
                    if os.path.isabs(db_dir) and not os.path.isabs(base_dir):
                        base_dir = os.sep + base_dir
    return cfg, cfg_path, base_dir


def ensure_image_key(vendor_dir, cfg):
    """config 里没有 image_aes_key 时，调 vendor 的 find_image_key.py 现场提取。

    前提：Weixin.exe 正在运行，且最近在微信里点开过 2-3 张图片（密钥只在内存里）。
    """
    if cfg.get("image_aes_key"):
        return True, "vendor/wechat-decrypt/config.json（已有）"

    script = os.path.join(vendor_dir, "find_image_key.py")
    if not os.path.exists(script):
        return False, f"找不到 {script}"

    print("[*] config 里没有 image_aes_key，尝试从微信进程内存提取…", file=sys.stderr)
    try:
        proc = subprocess.run(
            [sys.executable, "find_image_key.py"],
            cwd=vendor_dir, capture_output=True, text=True, timeout=600,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "find_image_key.py 超时（10 分钟）"
    except Exception as exc:
        return False, f"调用 find_image_key.py 失败: {exc}"

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, "find_image_key.py 退出码 %d：%s" % (proc.returncode, " / ".join(tail))
    if proc.stdout:
        for line in proc.stdout.strip().splitlines():
            if line.startswith("[+]") or "AES key" in line:
                print(f"  {line.strip()}", file=sys.stderr)

    # 重新读一遍 config
    with open(os.path.join(vendor_dir, "config.json"), encoding="utf-8") as f:
        fresh = json.load(f)
    cfg.update(fresh)
    if cfg.get("image_aes_key"):
        return True, "现场提取（find_image_key.py）"
    return False, "find_image_key.py 跑完但 config 里仍没有 image_aes_key"


def split_variant(file_name):
    """拆出 (base, variant)。base 是同一张图的公共前缀，variant ∈ {'', '_W', '_h', '_t'}"""
    stem = file_name
    if stem.lower().endswith(".dat"):
        stem = stem[:-4]
    for suf in ("_t", "_W", "_h"):
        if stem.endswith(suf):
            return stem[: -len(suf)], suf
    return stem, ""


def parse_extra_buffer_field1(blob):
    """从 image_hardlink_info_v4.extra_buffer 里取 protobuf field 1（Rec 子目录名）。

    格式形如 b'\\n\\x10865edd94e5e09f70\\x10\\xc4\\xdb\\n'：
      tag 0x0A = field 1 / wire type 2 (length-delimited)，随后 1 字节长度，再是内容。
    长度按单字节读——vendor 侧有同样的已知局限，超过 127 字节的目录名不存在。
    """
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return None
    if len(blob) < 2 or blob[0] != 0x0A:
        return None
    length = blob[1]
    end = 2 + length
    if length == 0 or end > len(blob):
        return None
    try:
        return blob[2:end].decode("ascii")
    except UnicodeDecodeError:
        return None


def query_image_records(hardlink_db, chat_hash):
    """按 chat hash 取该联系人的全部图片记录，附带解析好的磁盘路径。"""
    if not os.path.exists(hardlink_db):
        return []
    conn = sqlite3.connect(hardlink_db)
    try:
        rows = conn.execute(
            """
            SELECT h.file_name, h.file_size, h.modify_time, h.type, h.extra_buffer,
                   d1.username AS chat_hash, d2.username AS month
            FROM image_hardlink_info_v4 h
            JOIN dir2id d1 ON d1.rowid = h.dir1
            JOIN dir2id d2 ON d2.rowid = h.dir2
            WHERE d1.username = ?
            """,
            (chat_hash,),
        ).fetchall()
    finally:
        conn.close()
    return rows


def resolve_dat_path(wechat_base, chat_hash, month, file_name, rec_type, extra_buffer):
    """按 type 拼出 .dat 的真实路径。"""
    parts = [wechat_base, "msg", "attach", chat_hash, month]
    if rec_type == 4:
        sub = parse_extra_buffer_field1(extra_buffer)
        if sub:
            parts += ["Rec", sub]
    parts += ["Img", file_name]
    return os.path.join(*parts)


def build_time_index(messages):
    """把 messages.json 整理成便于按时间查找的结构。"""
    msgs = messages if isinstance(messages, list) else messages.get("messages", [])
    ordered = sorted(msgs, key=lambda m: m.get("timestamp") or 0)
    image_msgs = [m for m in ordered if m.get("type") == "image"]
    return ordered, image_msgs


def link_message(ordered, image_msgs, ts):
    """给一张图配上下文。

    优先找**图片类型的消息**（09-11 之前有），时间戳对上就是真值，发送者可信；
    对不上则退化为「最近的前一条消息」，发送者只能推断。
    """
    best, best_delta = None, None
    for m in image_msgs:
        delta = abs((m.get("timestamp") or 0) - ts)
        if delta <= _IMAGE_MSG_TOLERANCE and (best_delta is None or delta < best_delta):
            best, best_delta = m, delta

    if best is not None:
        return {
            "kind": "linked_message",
            "time": _fmt(best.get("timestamp")),
            "sender": best.get("sender"),
            "content": best.get("content"),
            "delta_seconds": best_delta,
        }, {"value": best.get("sender"), "basis": "图片消息时间戳精确匹配（±%ds）" % best_delta}

    prev = None
    for m in ordered:
        if (m.get("timestamp") or 0) <= ts:
            prev = m
        else:
            break
    if prev is None:
        return None, {"value": None, "basis": "该图之前没有消息，无法推断"}

    gap = ts - (prev.get("timestamp") or 0)
    ctx = {
        "kind": "nearest_preceding_message",
        "time": _fmt(prev.get("timestamp")),
        "sender": prev.get("sender"),
        "content": prev.get("content"),
        "gap_seconds": gap,
    }
    if gap <= _SENDER_HINT_MAX_GAP:
        hint = {"value": prev.get("sender"),
                "basis": "前一条消息是「%s」，间隔 %d 秒——推断，非事实" % (prev.get("sender"), gap)}
    else:
        hint = {"value": None,
                "basis": "前一条消息间隔 %d 秒，过远，不推断发送者" % gap}
    return ctx, hint


def _fmt(ts):
    if not ts:
        return None
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def load_messages(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("messages", [])


def collect_stickers(messages_path, assets_dir):
    """把 export_emojis.py 的产物折进索引。

    表情包不重复实现下载——`export_emojis.py` 已经做完了 cdnurl+aeskey 下载与落盘。
    这里只负责把已有资产挂上路径，让分析阶段一个文件看到全部媒体。

    能力边界：只能覆盖**有消息行的那些**表情（当前全在 09-11 之前）。
    09-11 之后收到的表情包没有消息行 → 定位不到，这里天然为空。
    """
    if not messages_path:
        return []
    emojis_path = os.path.join(os.path.dirname(os.path.abspath(messages_path)), "emojis.json")
    if not os.path.exists(emojis_path):
        return []
    try:
        with open(emojis_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[!] 读取 emojis.json 失败: {exc}", file=sys.stderr)
        return []

    # 资产文件按 md5 命名，扫一次建索引
    asset_by_key = {}
    if assets_dir and os.path.isdir(assets_dir):
        for dirpath, _dirnames, filenames in os.walk(assets_dir):
            for name in filenames:
                stem = os.path.splitext(name)[0]
                asset_by_key.setdefault(stem, os.path.join(dirpath, name))
                for key in (stem[:32], stem[:32].lower()):
                    asset_by_key.setdefault(key, os.path.join(dirpath, name))

    out = []
    for rec in data.get("emoji_records", []):
        md5 = (rec.get("md5") or "").strip()
        asset = asset_by_key.get(md5) or asset_by_key.get(md5.lower())
        out.append({
            "kind": "sticker",
            "md5": md5 or None,
            "time": _fmt(rec.get("first_timestamp")),
            "timestamp": rec.get("first_timestamp"),
            "path": asset,
            "downloaded": bool(asset and os.path.exists(asset)),
            "occurrence_count": rec.get("occurrence_count"),
            "caption": rec.get("productid") or None,
            "hint": None if asset else "未下载——跑 scripts/export_emojis.py --input <messages.json> 补齐",
        })
    return out


def parse_time_arg(value, is_end=False):
    if value is None:
        return None
    value = str(value).strip()
    if re.fullmatch(r"\d+", value):
        return int(value)
    import datetime
    try:
        dt = datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"无法解析时间参数: {value}（应为 YYYY-MM-DD 或秒级时间戳）")
    if is_end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def main():
    parser = argparse.ArgumentParser(
        description="提取指定联系人的图片与表情包，产出可查看的媒体目录与索引")
    parser.add_argument("--decrypted-dir", required=True, help="解密后的 SQLite 目录")
    parser.add_argument("--contact", required=True, help="联系人名字（备注名/昵称/wxid）")
    parser.add_argument("--messages", help="messages.json；用于图文配对与表情包合并")
    parser.add_argument("--vendor-dir", default=os.path.join("vendor", "wechat-decrypt"),
                        help="vendor/wechat-decrypt 目录（读 config.json 的图片密钥）")
    parser.add_argument("--output-dir", default=os.path.join("data", "media"),
                        help="媒体落盘根目录")
    parser.add_argument("--since", help="只取该时间之后的图（YYYY-MM-DD 或时间戳）")
    parser.add_argument("--until", help="只取该时间之前的图（YYYY-MM-DD 或时间戳）")
    parser.add_argument("--ensure-key", action="store_true",
                        help="图片密钥缺失时自动跑 find_image_key.py 现场提取"
                             "（需 Weixin.exe 运行中且近期点开过图片）")
    parser.add_argument("--no-stickers", action="store_true", help="跳过表情包合并")
    parser.add_argument("--lenient", action="store_true",
                        help="严格解密失败时逐张兜底：跳过尾部 magic 校验（FF D9 / IEND），"
                             "主体可解的图以 decoded_unverified 落盘。"
                             "图能看，但未经尾部校验，需人工过目")
    args = parser.parse_args()

    try:
        since = parse_time_arg(args.since)
        until = parse_time_arg(args.until, is_end=True)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1

    # ── 1. 联系人定位（复用 extract_messages 的既有实现） ──────────────────
    names = load_contacts(args.decrypted_dir)
    if not names:
        print(json.dumps({"error": f"无法加载联系人数据: {args.decrypted_dir}"}, ensure_ascii=False))
        return 1
    username = find_username(args.contact, names)
    if not username:
        print(json.dumps({"error": f"找不到联系人: {args.contact}"}, ensure_ascii=False))
        return 1
    display = names.get(username, username)
    chat_hash = hashlib.md5(username.encode("utf-8")).hexdigest()
    print(f"[*] 联系人 {display} → {username}", file=sys.stderr)
    print(f"[*] chat hash {chat_hash}", file=sys.stderr)

    # ── 2. vendor 配置 + 图片密钥 ─────────────────────────────────────────
    cfg, cfg_path, wechat_base = load_vendor_config(args.vendor_dir)
    key_source = "vendor/wechat-decrypt/config.json"
    key_ok = bool(cfg.get("image_aes_key"))
    key_reason = ""
    if not key_ok and args.ensure_key:
        key_ok, key_reason = ensure_image_key(args.vendor_dir, cfg)
        key_source = key_reason
    aes_key = cfg.get("image_aes_key")
    xor_key = cfg.get("image_xor_key", 0x88)
    if isinstance(xor_key, str):
        xor_key = int(xor_key, 0)

    if not wechat_base or not os.path.isdir(wechat_base):
        print(json.dumps({"error": f"无法确定微信数据根目录（wechat_base_dir）: {wechat_base}"},
                         ensure_ascii=False))
        return 1

    # ── 3. 查 hardlink 索引 ───────────────────────────────────────────────
    hardlink_db = os.path.join(args.decrypted_dir, "hardlink", "hardlink.db")
    rows = query_image_records(hardlink_db, chat_hash)
    print(f"[*] hardlink 命中 {len(rows)} 条图片记录", file=sys.stderr)
    if not rows:
        print(f"[!] 该联系人在 hardlink.db 里没有图片记录；"
              f"确认已解密 hardlink/hardlink.db", file=sys.stderr)

    # ── 4. 过滤 + 按 base 去重（同图多变体只留最优） ────────────────────────
    dedup = {}
    for file_name, file_size, modify_time, rec_type, extra_buffer, _ch, month in rows:
        if since and modify_time < since:
            continue
        if until and modify_time > until:
            continue
        base, variant = split_variant(file_name)
        if variant == "_t":          # 缩略图，丢弃
            continue
        priority = _VARIANT_PRIORITY.get(variant, 9)
        prev = dedup.get(base)
        if prev is None or priority < prev["priority"]:
            dedup[base] = {
                "base": base, "file_name": file_name, "size": file_size,
                "modify_time": modify_time, "type": rec_type, "month": month,
                "extra_buffer": extra_buffer, "priority": priority,
            }

    # ── 5. 逐张解密落盘 ───────────────────────────────────────────────────
    sys.path.insert(0, os.path.abspath(args.vendor_dir))
    try:
        from decode_image import decrypt_dat_file
    except Exception as exc:
        print(json.dumps({"error": f"无法导入 vendor 的 decode_image: {exc}"}, ensure_ascii=False))
        return 1

    bundle = resolve_bundle_paths(display, username, media_root=args.output_dir)
    media_dir = bundle["media_dir_path"]
    os.makedirs(media_dir, exist_ok=True)

    ordered, image_msgs = build_time_index(load_messages(args.messages))

    items = []
    stats = {"decoded": 0, "decoded_unverified": 0, "missing_file": 0,
             "decrypt_failed": 0, "no_key": 0}
    for rec in sorted(dedup.values(), key=lambda r: r["modify_time"]):
        dat_path = resolve_dat_path(wechat_base, chat_hash, rec["month"],
                                    rec["file_name"], rec["type"], rec["extra_buffer"])
        item = {
            "kind": "image",
            "time": _fmt(rec["modify_time"]),
            "timestamp": rec["modify_time"],
            "format": None,
            "size": rec["size"],
            "source_dat": os.path.relpath(dat_path, wechat_base).replace("\\", "/"),
            "path": None,
        }
        ctx, hint = link_message(ordered, image_msgs, rec["modify_time"])
        item["context"] = ctx
        item["sender_hint"] = hint

        if not os.path.exists(dat_path):
            item["status"] = "missing_file"
            stats["missing_file"] += 1
        elif not aes_key:
            item["status"] = "no_key"
            stats["no_key"] += 1
        else:
            import datetime
            stamp = datetime.datetime.fromtimestamp(rec["modify_time"]).strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(media_dir, f"{stamp}_{rec['base'][:8]}.tmp")
            got, fmt = decrypt_dat_file(dat_path, out_path, aes_key=aes_key, xor_key=xor_key)
            if got and fmt:
                final = os.path.join(media_dir, f"{stamp}_{rec['base'][:8]}.{fmt}")
                try:
                    if os.path.exists(final):
                        os.remove(final)
                    os.replace(got, final)
                except OSError:
                    final = got
                item["path"] = final
                item["format"] = fmt
                item["status"] = "ok"
                stats["decoded"] += 1
            else:
                if os.path.exists(out_path):
                    os.remove(out_path)
                blob = fmt = None
                reason = None
                if args.lenient:
                    blob, fmt, reason = lenient_decrypt_bytes(dat_path, aes_key, xor_key)

                if blob is None:
                    item["status"] = "decrypt_failed"
                    if reason:
                        item["fail_reason"] = reason
                    stats["decrypt_failed"] += 1
                else:
                    final = os.path.join(media_dir, f"{stamp}_{rec['base'][:8]}.{fmt}")
                    try:
                        with open(final, "wb") as f:
                            f.write(blob)
                    except OSError as exc:
                        print(f"[!] 写出 {final} 失败: {exc}", file=sys.stderr)
                        item["status"] = "decrypt_failed"
                        item["fail_reason"] = "write_failed"
                        stats["decrypt_failed"] += 1
                    else:
                        item["path"] = final
                        item["format"] = fmt
                        # 跳过了尾部 magic 校验：图能看，但不能算已验证
                        item["status"] = "decoded_unverified"
                        item["verify"] = reason
                        stats["decoded_unverified"] += 1
        items.append(item)

    # ── 6. 表情包合并（复用 export_emojis.py 的产物，不重写下载） ──────────
    stickers = []
    if not args.no_stickers:
        # 表情包资产跟 emojis.json 同目录（export_emojis.py 的落盘约定），
        # 不能用 bundle["emojis_assets_dir"]——那是按 output= 推导的，可能是别处。
        assets_dir = (os.path.join(os.path.dirname(os.path.abspath(args.messages)), "emojis_assets")
                      if args.messages else None)
        stickers = collect_stickers(args.messages, assets_dir)
        print(f"[*] 表情包 {len(stickers)} 条（其中已下载 "
              f"{sum(1 for s in stickers if s['downloaded'])} 条）", file=sys.stderr)

    # ── 7. 写索引 ─────────────────────────────────────────────────────────
    index = {
        "status": "ok",
        "contact": display,
        "contact_username": username,
        "chat_hash": chat_hash,
        "media_dir": media_dir,
        "key_ok": bool(aes_key),
        "key_source": key_source if aes_key else None,
        "key_reason": key_reason or None,
        "since": _fmt(since), "until": _fmt(until),
        "total": len(items) + len(stickers),
        "images": len(items),
        "stickers": len(stickers),
        "stats": stats,
        "note": "sender_hint 是依据相邻消息的**推断**，非事实；"
                "只有 context.kind == 'linked_message' 的发送者是可信的。"
                "status: ok=严格校验通过；decoded_unverified=--lenient 兜底，"
                "跳过了尾部 magic 校验，需人工过目；decrypt_failed=解不出。",
        "items": items + stickers,
    }
    index_path = bundle["media_index_path"]
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"[+] 解密 {stats['decoded']} 张 → {media_dir}", file=sys.stderr)
    if stats["decoded_unverified"]:
        print(f"[!] {stats['decoded_unverified']} 张宽松解出（尾部 magic 未通过校验，"
              f"状态 decoded_unverified，请人工过目）", file=sys.stderr)
    if stats["missing_file"]:
        print(f"[!] {stats['missing_file']} 张磁盘上找不到（hardlink 快照滞后）", file=sys.stderr)
    if stats["decrypt_failed"]:
        hint = "" if args.lenient else "（加 --lenient 可尝试兜底）"
        print(f"[!] {stats['decrypt_failed']} 张解密失败{hint}", file=sys.stderr)
    if stats["no_key"]:
        print(f"[!] {stats['no_key']} 张因缺少图片密钥被跳过（加 --ensure-key）", file=sys.stderr)
    print(f"[+] 索引 → {index_path}", file=sys.stderr)

    print(json.dumps({
        "status": "ok",
        "contact": display,
        "images": len(items),
        "decoded": stats["decoded"],
        "decoded_unverified": stats["decoded_unverified"],
        "stickers": len(stickers),
        "media_dir": media_dir,
        "index_path": index_path,
        "key_ok": bool(aes_key),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
