"""
decrypt_wechat.py - she-love-me 的跨平台解密入口

职责：
  - Windows / Linux: 直接调用 wechat-decrypt 的 main.py decrypt
  - macOS: 编译并调用 C 版密钥扫描器，再执行 decrypt_db.py

用法：
  python scripts/decrypt_wechat.py              # 自动选择第一个检测到的账号
  python scripts/decrypt_wechat.py --select     # 交互式选择账号
  python scripts/decrypt_wechat.py <目录路径>   # 指定数据库目录
"""
import os
import platform
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DECRYPTOR_DIR = REPO_ROOT / "vendor" / "wechat-decrypt"
MACOS_SCANNER = DECRYPTOR_DIR / "find_all_keys_macos"
MACOS_SCANNER_SOURCE = DECRYPTOR_DIR / "find_all_keys_macos.c"
KEYS_FILE = DECRYPTOR_DIR / "all_keys.json"


def run_command(cmd, cwd, check=True, env=None):
    """执行命令并处理输出编码。"""
    if env is None:
        env = os.environ.copy()
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    # 使用安全编码输出，避免 GBK 控制台编码错误
    console_encoding = sys.stdout.encoding or "utf-8"
    if result.stdout:
        safe_output = result.stdout.replace(str(REPO_ROOT), ".")
        try:
            print(safe_output, end="")
        except UnicodeEncodeError:
            # 回退到 ASCII 安全输出
            print(safe_output.encode(console_encoding, errors="replace").decode(console_encoding), end="")
    if result.stderr:
        safe_error = result.stderr.replace(str(REPO_ROOT), ".")
        try:
            print(safe_error, end="", file=sys.stderr)
        except UnicodeEncodeError:
            print(safe_error.encode(console_encoding, errors="replace").decode(console_encoding), end="", file=sys.stderr)
    if check and result.returncode != 0:
        raise RuntimeError(f"命令执行失败: {' '.join(cmd)}")
    return result.returncode


def ensure_decryptor_exists():
    if not DECRYPTOR_DIR.exists():
        raise RuntimeError("未找到 vendor/wechat-decrypt，请先执行 setup_check.py")


def should_rebuild_macos_scanner():
    if not MACOS_SCANNER.exists():
        return True
    return MACOS_SCANNER_SOURCE.stat().st_mtime > MACOS_SCANNER.stat().st_mtime


def load_existing_keys():
    if not KEYS_FILE.exists():
        return {}

    try:
        with KEYS_FILE.open(encoding="utf-8") as f:
            keys = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}

    return {name: value for name, value in keys.items() if not str(name).startswith("_")}


def run_macos_flow():
    existing_keys = load_existing_keys()
    if existing_keys:
        print(f"[*] 检测到现有数据库密钥 {len(existing_keys)} 个，跳过重复扫描。")
    else:
        if should_rebuild_macos_scanner():
            print("[*] 编译 macOS 密钥扫描器...")
            run_command(
                ["cc", "-O2", "-o", str(MACOS_SCANNER), str(MACOS_SCANNER_SOURCE), "-framework", "Foundation"],
                cwd=DECRYPTOR_DIR,
            )

        print("[*] 运行 macOS 密钥扫描器...")
        scanner_rc = run_command([str(MACOS_SCANNER)], cwd=DECRYPTOR_DIR, check=False)
        if scanner_rc != 0:
            raise RuntimeError(
                "macOS 密钥扫描失败。通常需要：1) 以 root 运行扫描器；2) 对 /Applications/WeChat.app 做 ad-hoc 重签名；3) 重启微信后重试。"
            )

    print("[*] 开始解密全部数据库...")
    run_command([sys.executable, "decrypt_db.py"], cwd=DECRYPTOR_DIR)


def run_default_flow(db_dir=None, interactive=False):
    """运行默认解密流程。

    Args:
        db_dir: 可选，指定微信数据库目录路径。
        interactive: 是否交互式选择账号。
    """
    print("[*] 调用 wechat-decrypt 主流程...")

    env = os.environ.copy()
    if db_dir:
        # 直接写入配置文件
        config_file = DECRYPTOR_DIR / "config.json"
        config = {}
        if config_file.exists():
            try:
                with config_file.open(encoding="utf-8") as f:
                    config = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        config["db_dir"] = str(db_dir)
        with config_file.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        print(f"[+] 已配置数据库目录: {db_dir}")
    elif not interactive:
        # 非交互模式：自动选择第一个检测到的账号
        env["WECHAT_DECRYPT_NONINTERACTIVE"] = "1"

    run_command([sys.executable, "main.py", "decrypt"], cwd=DECRYPTOR_DIR, env=env)


def main():
    ensure_decryptor_exists()
    system = platform.system().lower()

    # 解析命令行参数
    db_dir = None
    interactive = False

    for arg in sys.argv[1:]:
        if arg == "--select":
            interactive = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            print("\n选项:")
            print("  --select    交互式选择微信账号（多个账号时）")
            print("  <目录路径>  直接指定微信数据库目录")
            print("  -h, --help  显示帮助信息")
            sys.exit(0)
        else:
            db_dir = arg
            if not Path(db_dir).exists():
                print(f"[!] 指定的目录不存在: {db_dir}", file=sys.stderr)
                sys.exit(1)

    if system == "darwin":
        run_macos_flow()
        return

    run_default_flow(db_dir, interactive)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        sys.exit(1)
