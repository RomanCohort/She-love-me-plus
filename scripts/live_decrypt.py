# -*- coding: utf-8 -*-
"""
live_decrypt.py - 实时链路的解密层

把「源库 + 它的 -wal」解密成一份可读的快照。

为什么必须处理 WAL：
  vendor 的 decrypt_db.decrypt_database() 只 open(db_path,'rb') 读主文件，
  从不读 -wal。而微信的 session.db-wal 可以有 4MB 还没 checkpoint。
  只解主文件 = 最多落后一整个 WAL。
  仓库里 vendor/mcp_server.py:100-131 已经解决过一次，这里移植其语义。

三条安全约定：
  1. 先把源库 copy 到私有暂存目录，再解密「副本」。撕裂窗口缩到拷贝上，
     Windows 的共享冲突也变成可捕获的 PermissionError（带重试）。
  2. 只读源库，从不写。任何轮询间隔都不会损坏微信数据。
  3. decrypted 的 page 1 有 HMAC 校验；其余页没有。所以 decrypt_database()
     返回 True 不等于快照有效 —— 调用方可以再跑 check()。

不 chdir。vendor 的 config.py 把相对路径解析到 vendor 目录（绝对路径），
live_probe.py 里的 os.chdir 是历史包袱，常驻进程里它会污染每一个相对路径。

用法：
  python scripts/live_decrypt.py --db session/session.db --out scripts/tmp/session.db --refresh
  python scripts/live_decrypt.py --db message/message_fts.db --out scripts/tmp/fts.db --refresh --check
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_VENDOR = os.path.join(REPO_ROOT, 'vendor', 'wechat-decrypt')

PAGE_SZ = 4096
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24


# ---------------------------------------------------------------- vendor 装载

_VENDOR = {}


def load_vendor(vendor_dir=DEFAULT_VENDOR):
    """把 vendor 目录加进 sys.path 并返回 (decrypt_db, key_utils)。

    不 chdir：config.py 用 __file__ 解析自己，相对路径也在 load_config 里
    解析成绝对路径，所以不需要切工作目录。
    """
    vendor_dir = os.path.abspath(vendor_dir)
    if vendor_dir in _VENDOR:
        return _VENDOR[vendor_dir]
    if not os.path.isdir(vendor_dir):
        raise RuntimeError('找不到解密器目录：' + vendor_dir)
    if vendor_dir not in sys.path:
        sys.path.insert(0, vendor_dir)
    import decrypt_db  # noqa: E402
    import key_utils  # noqa: E402
    _VENDOR[vendor_dir] = (decrypt_db, key_utils)
    return _VENDOR[vendor_dir]


def load_keys(vendor_dir=DEFAULT_VENDOR, rel_path=None):
    """读 all_keys.json。

    每次调用都重读 —— 密钥扫描会原地重写这个文件（同目录还有
    .bak / .pre-scan-* 备份），缓存在常驻进程里会读到过期的密钥。
    文件被重写的中途可能读到半截 JSON，所以解析失败返回空而不是抛。
    """
    _, key_utils = load_vendor(vendor_dir)
    path = os.path.join(os.path.abspath(vendor_dir), 'all_keys.json')
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    keys = key_utils.strip_key_metadata(raw)
    if rel_path is None:
        return keys
    return key_utils.get_key_info(keys, rel_path)


def _as_key(info):
    """把 get_key_info 的结果归一成 bytes。

    all_keys.json 里存的是十六进制字符串（decrypt_db.main 也是自己
    bytes.fromhex 的），传给 decrypt_page 之前必须转成 bytes。
    """
    if info is None:
        return None
    raw = info.get('enc_key') if isinstance(info, dict) else info
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 拷贝与解密

def copy_with_retry(src, dst, retries=3, delay=1.0):
    """把源文件拷到暂存区。微信正在写时 Windows 可能抛共享冲突，重试几次。"""
    last = None
    for _ in range(max(1, retries)):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            shutil.copy2(src, dst)
            return dst
        except (PermissionError, OSError) as exc:
            last = exc
            time.sleep(delay)
    raise RuntimeError('拷贝失败（微信可能正在写）：%s -> %s（%s）' % (src, dst, last))


def decrypt_full(src_db, dst_db, enc_key, vendor_dir=DEFAULT_VENDOR):
    """全量解密。复用 vendor 的 decrypt_database（它带 page 1 的 HMAC 校验）。"""
    decrypt_db, _ = load_vendor(vendor_dir)
    tmp = dst_db + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    # vendor 这个函数会往 stdout 打进度，守护进程里太吵，压掉
    with contextlib.redirect_stdout(io.StringIO()):
        ok = decrypt_db.decrypt_database(src_db, tmp, enc_key)
    if not ok:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError('page 1 HMAC 校验失败（密钥可能已轮换）：' + src_db)
    os.replace(tmp, dst_db)
    return os.path.getsize(dst_db) // PAGE_SZ


def decrypt_wal(wal_path, dst_db, enc_key, vendor_dir=DEFAULT_VENDOR):
    """把 WAL 里已提交的帧补写进已解密的快照。

    移植自 vendor/mcp_server.py:100-131。salt 校验那两行是关键：
    checkpoint 过程中 WAL 头部会换代，salt 不匹配的帧属于上一代，
    补进去就是脏数据。

    返回补丁的帧数。
    """
    decrypt_db, _ = load_vendor(vendor_dir)
    if not os.path.exists(wal_path) or not os.path.exists(dst_db):
        return 0
    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return 0
    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
    patched = 0
    with open(wal_path, 'rb') as wf, open(dst_db, 'r+b') as df:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        if len(wal_hdr) < WAL_HEADER_SZ:
            return 0
        wal_salt1 = struct.unpack('>I', wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack('>I', wal_hdr[20:24])[0]
        limit = os.path.getsize(dst_db)
        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack('>I', fh[0:4])[0]
            frame_salt1 = struct.unpack('>I', fh[8:12])[0]
            frame_salt2 = struct.unpack('>I', fh[12:16])[0]
            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1000000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue
            # 快照比主文件短时，超出的页不补（等下一次全量）
            if (pgno - 1) * PAGE_SZ + PAGE_SZ > limit:
                continue
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(decrypt_db.decrypt_page(enc_key, ep, pgno))
            patched += 1
    return patched


def mtimes(path):
    """返回 (db_mtime, wal_mtime)，都可能是 None。"""
    db_m = os.path.getmtime(path) if os.path.isfile(path) else None
    wal = path + '-wal'
    wal_m = os.path.getmtime(wal) if os.path.isfile(wal) else None
    return db_m, wal_m


def refresh(rel_path, dst_db, vendor_dir=DEFAULT_VENDOR, state=None, force=False):
    """把 <db_dir>/<rel_path> 解密/更新到 dst_db。

    按 (db_mtime, wal_mtime) 决定做什么：
      全量解密过一次之后，只有 WAL 变了就只打补丁（省掉 22MB 重解）。
    state 是调用方持有的字典，用来记住上次见到的 mtime。
    """
    decrypt_db, _ = load_vendor(vendor_dir)
    db_dir = decrypt_db.DB_DIR
    src_db = os.path.join(db_dir, rel_path)
    if not os.path.isfile(src_db):
        raise RuntimeError('找不到源库：' + src_db)

    enc_key = _as_key(load_keys(vendor_dir, rel_path))
    if not enc_key:
        raise RuntimeError('all_keys.json 里没有 %s 的密钥' % rel_path)

    db_m, wal_m = mtimes(src_db)
    state = state if state is not None else {}
    seen_db = state.get('db_mtime')
    seen_wal = state.get('wal_mtime')

    if not force and os.path.isfile(dst_db):
        if seen_db == db_m and seen_wal == wal_m:
            return {'mode': 'skip', 'patched': 0, 'db_mtime': db_m,
                    'wal_mtime': wal_m, 'dst': dst_db}
        if seen_db == db_m and wal_m is not None:
            # 只有 WAL 动了 —— 补丁即可
            patched = decrypt_wal(src_db + '-wal', dst_db, enc_key, vendor_dir)
            state.update(db_mtime=db_m, wal_mtime=wal_m)
            return {'mode': 'wal', 'patched': patched, 'db_mtime': db_m,
                    'wal_mtime': wal_m, 'dst': dst_db}

    # 全量：先拷副本，再解副本，最后补 WAL
    staging = dst_db + '.src'
    copy_with_retry(src_db, staging)
    pages = decrypt_full(staging, dst_db, enc_key, vendor_dir)
    patched = 0
    wal_ok = True
    wal_error = None
    if os.path.isfile(src_db + '-wal'):
        try:
            copy_with_retry(src_db + '-wal', staging + '-wal')
            patched = decrypt_wal(staging + '-wal', dst_db, enc_key, vendor_dir)
        except RuntimeError as exc:
            # 不致命：主文件已经可用。但绝不能把 wal_mtime 记成已处理，
            # 否则下一个 tick 会跳过，我们就永远停在一次失败的补丁上。
            wal_ok = False
            wal_error = str(exc)
    for leftover in (staging, staging + '-wal'):
        if os.path.exists(leftover):
            try:
                os.remove(leftover)
            except OSError:
                pass
    # 全量之后再取一次 mtime：拷贝期间源库可能又变了
    db_m2, wal_m2 = mtimes(src_db)
    state.update(db_mtime=db_m2)
    if wal_ok:
        state.update(wal_mtime=wal_m2)
    result = {'mode': 'full', 'patched': patched, 'pages': pages,
              'db_mtime': db_m2, 'wal_mtime': wal_m2, 'dst': dst_db}
    if not wal_ok:
        result['wal_error'] = wal_error
        result['stale_risk'] = True
    return result


def check(dst_db):
    """对解密后的快照跑 quick_check。

    只解 page 1 有 HMAC，其余页没有完整性校验 —— 撕裂页会变成
    「看起来成功的垃圾」。这个检查是唯一能发现它的东西。

    注意：带 FTS5 虚表的库会报 'SQL logic error'（影子表让 quick_check
    不适用），新旧副本上都会报，所以那不是损坏信号 —— 标 applicable=False。
    真正的损坏长这样：'database disk image is malformed'。
    """
    try:
        con = sqlite3.connect('file:%s?mode=ro&immutable=1' % dst_db.replace('\\', '/'),
                              uri=True)
    except sqlite3.Error as exc:
        return {'ok': False, 'applicable': True, 'error': str(exc)}
    try:
        row = con.execute('PRAGMA quick_check').fetchone()
    except sqlite3.DatabaseError as exc:
        text = str(exc)
        if 'logic error' in text.lower():
            # FTS5 影子表 —— 这个检查对这类库不适用，不是损坏
            return {'ok': None, 'applicable': False, 'note': text}
        return {'ok': False, 'applicable': True, 'error': text}
    finally:
        con.close()
    value = row[0] if row else 'no result'
    return {'ok': value == 'ok', 'applicable': True, 'result': value}


def open_ro(path):
    """只读打开一份解密快照。

    immutable=1 跳过 WAL/SHM 处理和加锁——这正是只读快照要的：
    不会在解密文件旁边留下 -shm/-wal 残留（live_probe 现在就会留）。
    """
    return sqlite3.connect('file:%s?mode=ro&immutable=1' % path.replace('\\', '/'),
                           uri=True)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description='解密 / 增量更新一份微信数据库快照（含 WAL）')
    parser.add_argument('--db', required=True, help='相对 db_dir 的路径，如 session/session.db')
    parser.add_argument('--out', required=True, help='输出路径')
    parser.add_argument('--vendor', default=DEFAULT_VENDOR)
    parser.add_argument('--refresh', action='store_true', help='按 mtime 决定全量或补丁')
    parser.add_argument('--force', action='store_true', help='强制全量重解')
    parser.add_argument('--check', action='store_true', help='解密后跑 PRAGMA quick_check')
    parser.add_argument('--state', help='把上次见到的 mtime 存这里，让 --refresh 能真的跳过')
    args = parser.parse_args(argv)

    out = os.path.abspath(args.out)
    state = {}
    if args.state and os.path.isfile(args.state):
        try:
            with open(args.state, 'r', encoding='utf-8') as handle:
                state = json.load(handle).get(args.db, {})
        except (OSError, json.JSONDecodeError):
            state = {}
    try:
        if args.refresh or args.force:
            result = refresh(args.db, out, args.vendor, state=state,
                             force=args.force)
        else:
            enc_key = _as_key(load_keys(args.vendor, args.db))
            if not enc_key:
                raise RuntimeError('all_keys.json 里没有 %s 的密钥' % args.db)
            decrypt_db, _ = load_vendor(args.vendor)
            src = os.path.join(decrypt_db.DB_DIR, args.db)
            staging = out + '.src'
            copy_with_retry(src, staging)
            pages = decrypt_full(staging, out, enc_key, args.vendor)
            os.remove(staging)
            result = {'mode': 'full', 'pages': pages, 'dst': out}
    except Exception as exc:  # noqa: BLE001 - CLI 边界，转成 JSON 给上层
        print(json.dumps({'status': 'error',
                          'error': type(exc).__name__ + ': ' + str(exc)},
                         ensure_ascii=False))
        return 2

    if args.check:
        result['check'] = check(out)
    if args.state:
        merged = {}
        if os.path.isfile(args.state):
            try:
                with open(args.state, 'r', encoding='utf-8') as handle:
                    merged = json.load(handle)
            except (OSError, json.JSONDecodeError):
                merged = {}
        merged[args.db] = state
        tmp = args.state + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, args.state)
    result['status'] = 'ok'
    result['bytes'] = os.path.getsize(out)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
