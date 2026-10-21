r"""PaddleOCR-VL 服务的按需启停管理。

为什么需要它
------------
8GB 显存上有个硬约束：VL 模型常驻要吃掉 7.5/8.15 GB，
留给分层快路径（版面检测 + PP-OCRv5）只剩 0.65 GB，实测会把它们拖慢 2.6 倍：

    阶段            VL 常驻    释放后
    版面检测         14.2s      6.5s
    轻量 OCR          8.3s      2.3s
    每页合计         22.4s      8.8s

所以正确做法是：**快路径常驻（很小），VL 按需拉起、用完释放。**

命令行用法
----------
    python vl_service.py status     # 在跑吗？占多少显存？
    python vl_service.py start      # 拉起（等就绪，约 1-2 分钟）
    python vl_service.py stop       # 停掉并等显存释放
    python vl_service.py restart

被 layered.py 当作模块用（import vl_service）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
LOG_FILE = LOG_DIR / "vl_server.log"
PID_FILE = HERE / ".vl_server.pid"

HOST = "127.0.0.1"
PORT = int(os.environ.get("VL_PORT", "8100"))
PIPELINE = os.environ.get("VL_PIPELINE", "PaddleOCR-VL-1.6")
BASE = f"http://{HOST}:{PORT}"

START_TIMEOUT = 300      # 首次启动要加载 1.8 GB 权重
STOP_TIMEOUT = 60


# ---------------------------------------------------------------- 查询
def health(timeout: float = 2.0) -> dict | None:
    """能拿到 /health 就说明服务活着。"""
    try:
        r = requests.get(f"{BASE}/health", timeout=timeout)
        return r.json()
    except requests.RequestException:
        return None


def is_running() -> bool:
    h = health()
    return bool(h and h.get("errorCode") == 0)


def find_pids() -> list[int]:
    """按命令行找出服务进程。

    不依赖 pid 文件 —— 那个可能过期（进程被外部杀掉、或换了启动方式）。
    命令行匹配更可靠。
    """
    try:
        import psutil
    except ImportError:
        return _find_pids_fallback()

    pids = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = p.info.get("cmdline") or []
            joined = " ".join(cl)
            if "paddlex" in joined and "--serve" in joined:
                pids.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def _find_pids_fallback() -> list[int]:
    """psutil 不可用时退回 PowerShell。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'paddlex.*--serve' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=20)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:                                     # noqa: BLE001
        return []


def vram_used() -> int | None:
    """当前 GPU 已用显存（MiB）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:                                     # noqa: BLE001
        return None


def status() -> dict:
    h = health()
    pids = find_pids()
    return {
        "running": bool(h and h.get("errorCode") == 0),
        "health": h.get("errorMsg") if h else None,
        "pids": pids,
        "vram_used_mib": vram_used(),
        "url": BASE,
        "pipeline": PIPELINE,
    }


# ---------------------------------------------------------------- 启停
def start(wait: bool = True, timeout: int = START_TIMEOUT,
          verbose: bool = True) -> bool:
    """拉起服务。已在跑就直接返回 True。"""
    if is_running():
        if verbose:
            print(f"VL 服务已在运行 -> {BASE}")
        return True

    # 有残留进程先清掉，否则端口会冲突
    stale = find_pids()
    if stale:
        if verbose:
            print(f"清理残留进程 {stale} …")
        _kill(stale)
        time.sleep(3)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：父进程退出后服务继续活着
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP)

    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    log.write(f"\n\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
              f"pipeline={PIPELINE} =====\n")

    cmd = [sys.executable, "-m", "paddlex", "--serve",
           "--pipeline", PIPELINE,
           "--host", HOST, "--port", str(PORT), "--device", "gpu:0"]
    if verbose:
        print(f"启动 VL 服务 …（首次要加载约 1.8 GB 权重，等 1-2 分钟）")
        print(f"  日志: {LOG_FILE}")

    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            env=env, creationflags=flags,
                            cwd=str(HERE))
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    if not wait:
        return True

    t0 = time.time()
    while time.time() - t0 < timeout:
        if is_running():
            if verbose:
                print(f"  ✓ 就绪，用时 {time.time() - t0:.1f}s -> {BASE}")
            return True
        time.sleep(3)
        if verbose and int(time.time() - t0) % 15 < 3:
            print(f"  … 等待中 {time.time() - t0:.0f}s")
    if verbose:
        print(f"  ✗ {timeout}s 内未就绪，看日志: {LOG_FILE}")
    return False


def _kill(pids: list[int]) -> None:
    if os.name == "nt":
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
    else:
        import signal
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def stop(wait: bool = True, timeout: int = STOP_TIMEOUT,
         verbose: bool = True) -> bool:
    """停掉服务并等显存释放（显存不释放，快路径就快不起来）。"""
    pids = find_pids()
    if not pids and not is_running():
        if verbose:
            print("VL 服务本来就没在跑")
        return True

    before = vram_used()
    if verbose:
        print(f"停止 VL 服务 pid={pids}（当前显存 {before} MiB）…")
    _kill(pids)

    if not wait:
        return True

    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(2)
        if not find_pids() and not is_running():
            time.sleep(2)                    # 再等驱动回收显存
            after = vram_used()
            if verbose:
                print(f"  ✓ 已停止，用时 {time.time() - t0:.1f}s，"
                      f"显存 {before} -> {after} MiB")
            return True
    if verbose:
        print(f"  ✗ {timeout}s 内未完全停止")
    return False


def ensure_running(verbose: bool = True) -> bool:
    """只在需要时启动。layered.py 用这个。"""
    if is_running():
        return True
    return start(wait=True, verbose=verbose)


# ===========================================================================
# WSL 里的 vLLM 后端
# ===========================================================================
# 两条后端实测差了 40 倍以上（同一张裁剪图）：
#   native PaddleOCR /layout-parsing    8 – 23 秒
#   WSL vLLM /v1/chat/completions       0.2 秒（首次含 Triton JIT 约 118s）
#
# vLLM 跑在 WSL 里（Windows 原生装不了），所以通过 wsl 命令驱动一个 shell 脚本。
# 那个脚本里封着四个必需参数，缺一个都起不来——详见 wsl_vllm.sh 顶部注释。

VLLM_PORT = int(os.environ.get("VLLM_PORT", "8110"))
WSL_DISTRO = os.environ.get("WSL_DISTRO", "Ubuntu-22.04")
WSL_SCRIPT = os.environ.get("WSL_SCRIPT", "/mnt/d/visonalbe/wsl_vllm.sh")
VLLM_BASE = f"http://127.0.0.1:{VLLM_PORT}"


def _wsl(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(["wsl", "-d", WSL_DISTRO, "-u", "root", "--", *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def vllm_health(timeout: float = 3.0) -> dict | None:
    try:
        r = requests.get(f"{VLLM_BASE}/v1/models", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except requests.RequestException:
        return None


def vllm_running() -> bool:
    return vllm_health() is not None


def vllm_start(wait: bool = True, verbose: bool = True) -> bool:
    if vllm_running():
        if verbose:
            print(f"vLLM 已在运行 -> {VLLM_BASE}")
        return True
    if verbose:
        print(f"在 WSL({WSL_DISTRO}) 里启动 vLLM …（约 1 分钟）")
    try:
        r = _wsl(["bash", WSL_SCRIPT, "start"])
    except subprocess.TimeoutExpired:
        if verbose:
            print("  启动超时")
        return False
    out = (r.stdout or "") + (r.stderr or "")
    if verbose:
        for line in out.strip().splitlines()[-12:]:
            print("  " + line)
    if not wait:
        return True
    return vllm_running()


def vllm_stop(verbose: bool = True) -> bool:
    if verbose:
        print("停止 WSL 里的 vLLM …")
    try:
        r = _wsl(["bash", WSL_SCRIPT, "stop"], timeout=120)
    except subprocess.TimeoutExpired:
        return False
    if verbose and r.stdout:
        for line in r.stdout.strip().splitlines()[-5:]:
            print("  " + line)
    return not vllm_running()


def vllm_status() -> dict:
    h = vllm_health()
    return {"running": h is not None,
            "url": VLLM_BASE,
            "models": [m["id"] for m in h["data"]] if h else [],
            "distro": WSL_DISTRO,
            "vram_used_mib": vram_used()}


def vllm_warmup(crop_png: bytes, model: str = "PaddleOCR-VL",
                verbose: bool = True) -> float:
    """用真实尺寸的图预热，把 Triton 内核编译掉。

    必须用真实尺寸 —— 用 1x1 占位图会编译不同形状的内核，白费功夫。
    返回值是本次耗时（秒），首次通常 100+ 秒，之后 0.2 秒级。
    """
    import base64 as _b64
    body = {"model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,"
                               + _b64.b64encode(crop_png).decode()}},
                {"type": "text", "text": "OCR:"}]}],
            "max_tokens": 64, "temperature": 0.0}
    t = time.time()
    r = requests.post(f"{VLLM_BASE}/v1/chat/completions", json=body, timeout=600)
    r.raise_for_status()
    dt = time.time() - t
    if verbose:
        print(f"  预热耗时 {dt:.1f}s")
    return dt


# ---------------------------------------------------------------- CLI
def main() -> int:
    argv = sys.argv[1:]
    backend = "paddle"
    if argv and argv[0] in ("paddle", "vllm"):
        backend = argv.pop(0)
    cmd = argv[0] if argv else "status"

    if backend == "vllm":
        if cmd == "status":
            print(json.dumps(vllm_status(), ensure_ascii=False, indent=2))
            return 0
        if cmd == "start":
            return 0 if vllm_start() else 1
        if cmd == "stop":
            return 0 if vllm_stop() else 1
        if cmd == "restart":
            vllm_stop(); time.sleep(2)
            return 0 if vllm_start() else 1
        print(__doc__)
        return 1

    if cmd == "status":
        s = status()
        print(json.dumps(s, ensure_ascii=False, indent=2))
        if not s["running"]:
            print("\n服务未运行。启动：python vl_service.py start")
        return 0

    if cmd == "start":
        return 0 if start() else 1

    if cmd == "stop":
        return 0 if stop() else 1

    if cmd == "restart":
        stop(verbose=True)
        time.sleep(2)
        return 0 if start() else 1

    print(__doc__)
    print(f"未知命令: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
