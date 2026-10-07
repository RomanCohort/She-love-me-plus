"""
setup_check.py - 跨平台环境检查与引导

职责：
  - 校验 Python 版本
  - 按需 clone vendor/wechat-decrypt
  - 安装 wechat-decrypt 运行依赖
  - 检查微信是否正在运行（Windows / macOS）
"""
import argparse
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DECRYPTOR_DIR = REPO_ROOT / "vendor" / "wechat-decrypt"
DEFAULT_DECRYPTOR_DIR_DISPLAY = Path("vendor") / "wechat-decrypt"
DECRYPTOR_REPO = "https://github.com/ylytdeng/wechat-decrypt"
DEPENDENCIES = ("pycryptodome", "zstandard")
DEFAULT_MODEL_CACHE_DIR = REPO_ROOT / "vendor" / "hf_cache"

# 情感计算层（可选）。缺任何一项都不影响主分析——stats.json 里 affect.status
# 会是 disabled，消费方判 status 即可。
AFFECT_REQUIRED = ("numpy", "onnxruntime", "tokenizers", "huggingface_hub")
SUPPORTED_PLATFORMS = {"win32": "Windows", "darwin": "macOS"}
RUNTIME_DIRS = (
    REPO_ROOT / "vendor",
    REPO_ROOT / "data",
    REPO_ROOT / "reports",
    REPO_ROOT / "scripts" / "tmp",
)


def run_command(cmd, cwd=None):
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        errors="replace",
    )


def ensure_python_version():
    return sys.version_info >= (3, 9)


def detect_platform():
    return SUPPORTED_PLATFORMS.get(sys.platform, sys.platform)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def build_notes():
    if sys.platform == "win32":
        return [
            "请用管理员权限启动终端和微信。",
            "若解密时报 Access Denied，请重新以管理员身份运行。",
        ]
    if sys.platform == "darwin":
        return [
            "请确认微信已启动并处于登录状态。",
            "若解密失败，请检查终端是否已获得所需系统权限。",
        ]
    return [
        "当前仓库仅正式支持 Windows 和 macOS。",
    ]


def ensure_runtime_dirs():
    created = []
    for directory in RUNTIME_DIRS:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(display_path(directory))
    return created


def ensure_decryptor(decryptor_dir):
    if decryptor_dir.exists():
        return {"changed": False, "message": "wechat-decrypt 已存在"}

    git_path = shutil.which("git")
    if not git_path:
        raise RuntimeError("未找到 git，无法自动 clone wechat-decrypt")

    decryptor_dir.parent.mkdir(parents=True, exist_ok=True)
    result = run_command([git_path, "clone", DECRYPTOR_REPO, str(decryptor_dir)])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "clone wechat-decrypt 失败")

    return {"changed": True, "message": "已 clone wechat-decrypt"}


def install_dependencies():
    result = run_command([sys.executable, "-m", "pip", "install", *DEPENDENCIES])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "安装依赖失败")

    return {"changed": True, "message": "依赖安装完成"}


def check_wechat_process():
    if sys.platform == "win32":
        result = run_command(["tasklist"])
        if result.returncode != 0:
            return False, [], "无法执行 tasklist 检查微信进程"
        matches = [line.strip() for line in result.stdout.splitlines() if "wechat" in line.lower()]
        return bool(matches), matches[:10], ""

    if sys.platform == "darwin":
        matches = []
        for pattern in ("WeChat", "微信", "wechat"):
            result = run_command(["pgrep", "-ifl", pattern])
            if result.returncode not in (0, 1):
                return False, [], f"无法执行 pgrep 检查微信进程: {result.stderr.strip()}"
            for line in result.stdout.splitlines():
                line = line.strip()
                if line and line not in matches:
                    matches.append(line)
        return bool(matches), matches[:10], ""

    return False, [], "当前平台未实现微信进程检测"


def check_affect_env(model_cache_dir=None):
    """诊断情感计算层（affect）的可选依赖与模型缓存。

    **只诊断，不下载。** 下载由 `stats_analyzer.py --affect auto` 在主流程里按需
    触发——setup_check 擅自拉 90MB 会让人措手不及。这里的职责是把「缺什么」讲
    清楚，让人自己决定要不要装。

    返回的 `enabled` 表示「现在就能跑」；`hint` 说明下一步该做什么。
    """
    out = {"enabled": False, "versions": {}, "missing": [],
           "model_cache": None, "model_cached": None, "hint": ""}

    for mod in AFFECT_REQUIRED:
        try:
            out["versions"][mod] = getattr(importlib.import_module(mod), "__version__", "?")
        except ImportError:
            out["missing"].append(mod)

    if out["missing"]:
        out["hint"] = (f"缺少 {'、'.join(out['missing'])}。装法："
                       "python -m pip install -r requirements-optional.txt")
        return out

    cache = Path(model_cache_dir) if model_cache_dir else DEFAULT_MODEL_CACHE_DIR
    out["model_cache"] = display_path(cache)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import embed_hf
        out["model_cached"] = embed_hf.cached(cache_dir=str(cache))
        out["model"] = embed_hf.DEFAULT_MODEL
    except Exception as exc:
        out["hint"] = f"依赖齐了，但 embed_hf 导入失败：{type(exc).__name__}: {exc}"
        return out

    out["enabled"] = bool(out["model_cached"])
    if not out["model_cached"]:
        out["hint"] = ("依赖齐了，但模型尚未下载。首次运行 "
                       "`stats_analyzer.py --affect auto` 会下载约 90MB 到上述目录"
                       "（大陆会自动回退 hf-mirror.com 镜像）。")
    return out


def main():
    parser = argparse.ArgumentParser(description="检查 she-love-me 运行环境")
    parser.add_argument(
        "--ensure-decryptor",
        action="store_true",
        help="若 wechat-decrypt 不存在则自动 clone，并安装依赖",
    )
    parser.add_argument(
        "--decryptor-dir",
        default=str(DEFAULT_DECRYPTOR_DIR),
        help=f"wechat-decrypt 目录，默认 {DEFAULT_DECRYPTOR_DIR_DISPLAY}",
    )
    parser.add_argument(
        "--ensure-affect",
        action="store_true",
        help="诊断情感计算层（affect）的可选依赖与模型缓存。"
             "只诊断，不下载模型——下载由 stats_analyzer.py --affect auto 触发",
    )
    args = parser.parse_args()

    decryptor_dir = Path(args.decryptor_dir).expanduser().resolve()
    report = {
        "status": "ok",
        "platform": detect_platform(),
        "python_executable": sys.executable,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "decryptor_dir": display_path(decryptor_dir),
        "decryptor_present": decryptor_dir.exists(),
        "wechat_running": False,
        "wechat_processes": [],
        "notes": build_notes(),
        "actions": [],
    }

    # 放在所有 early-exit 之前——否则微信没开时，`--ensure-affect` 的诊断结果会
    # 被「请先打开微信」那条错误吞掉，而它跟微信毫无关系。
    if args.ensure_affect:
        report["affect"] = check_affect_env()

    created_dirs = ensure_runtime_dirs()
    if created_dirs:
        report["actions"].append({
            "changed": True,
            "message": "已初始化运行目录",
            "directories": created_dirs,
        })

    if sys.platform not in SUPPORTED_PLATFORMS:
        report["status"] = "error"
        report["error"] = f"当前平台 {detect_platform()} 暂未正式支持"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(1)

    if not ensure_python_version():
        report["status"] = "error"
        report["error"] = "需要 Python 3.9 或更高版本"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(1)

    try:
        if args.ensure_decryptor:
            if not decryptor_dir.exists():
                report["actions"].append(ensure_decryptor(decryptor_dir))
                report["decryptor_present"] = True
            report["actions"].append(install_dependencies())
    except RuntimeError as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(1)

    running, processes, error = check_wechat_process()
    report["wechat_running"] = running
    report["wechat_processes"] = processes
    if error:
        report["status"] = "error"
        report["error"] = error
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(1)

    if not running:
        report["status"] = "error"
        report["error"] = "请先打开微信并登录，然后重新运行"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(1)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
