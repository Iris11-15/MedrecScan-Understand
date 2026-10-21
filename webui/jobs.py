"""任务注册表 + 单飞文件锁 + 进度读取（round37 F1 / F7 / F8 / T4 / T5）。

进度**只靠读文件**（_pipeline_logs/<case>__*.log 的 mtime + _medrec/RUN_META.json），
不靠进程内存 ⇒ 网页重启不影响正在跑的批次（A6）。
"""
from __future__ import annotations

import csv
import glob
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import guards
import pii

MEDREC = "/home/ubuntu/medrec/bin/medrec"        # 入口唯一（§0.2）；绝不用裸 medrec
RUN_ROOT = guards.RUN_ROOT                # 写根（round42c：raid5）
LEGACY_RUN_ROOT = guards.LEGACY_RUN_ROOT  # 旧批次原地只读，不搬不删
RUN_ROOTS = guards.RUN_ROOTS
HEALTH = RUN_ROOT / "_health"
LOCK = HEALTH / "run.lock"
WORKSPACE = Path("/home/ubuntu/medrec/workspace")


def batch_dir(batch: str) -> Path:
    """批次目录解析：写根优先；写根没有、旧根有 ⇒ 用旧根。
    新批次一律落写根（round42c 之前那 200+ 个批次原地留在旧根）。"""
    p = RUN_ROOT / batch
    if p.exists():
        return p
    for r in RUN_ROOTS[1:]:
        q = r / batch
        if q.is_dir():
            return q
    return p


# ------------------------------------------------------------------ F1 单飞锁
def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def foreign_runs() -> list[str]:
    """扫 /proc 找**不是网页发起**的跑批（手敲命令不会拿 F1 锁 ⇒ 网页拦不住，只能提示）。"""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{d}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "medrec_cli.py run" in cmd and " --batch " in cmd:
            batch = cmd.split(" --batch ")[1].split()[0]
            out.append(f"pid={d} batch={pii.scrub_text(batch)}")
    return sorted(out)


def read_lock() -> dict:
    try:
        data = json.loads(LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    data["alive"] = pid_alive(data.get("pid", -1))
    if data.get("started_at"):
        data["running_min"] = round((time.time() - float(data["started_at"])) / 60.0, 1)
    return data


def acquire_lock(batch: str, fingerprint: str, command: str) -> tuple[bool, str, dict]:
    """O_EXCL 建锁。已锁且进程还活着 ⇒ 拒绝（F1）。进程已死 ⇒ 锁改名留证后接管。"""
    HEALTH.mkdir(parents=True, mode=0o700, exist_ok=True)
    guards.ensure_run_root()
    rec = {"pid": None, "batch": batch, "started_at": time.time(),
           "input_fingerprint": fingerprint, "command": command}
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            cur = read_lock()
            if cur.get("alive"):
                return False, (f"正在跑 {cur.get('batch', '?')}，已运行 "
                               f"{cur.get('running_min', '?')} 分钟（pid={cur.get('pid')}）"
                               "⇒ 单飞锁拒绝第二个跑批（F1：两批抢 GPU/Strata 会让 s/例 不可比、"
                               "baselines.jsonl 双写）"), cur
            moved = LOCK.with_name(f"run.lock.stale-{int(time.time())}")
            try:
                os.replace(LOCK, moved)      # 不删：陈旧锁也是证据
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        return True, "", rec
    return False, f"锁文件状态异常：{LOCK}", {}


def set_lock_pid(pid: int) -> None:
    try:
        rec = json.loads(LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    rec["pid"] = pid
    tmp = LOCK.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, LOCK)
    os.chmod(LOCK, 0o600)


def release_lock() -> None:
    try:
        LOCK.unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------------ 起进程（T4）
def build_command(src: Path, batch: str, steps: list[str] | None, log: Path) -> tuple[list[str], str]:
    argv = ["setsid", "nohup", MEDREC, "run", str(src), "--batch", batch]
    # 写根 ≠ config/pipeline.yaml 的 paths.run_root ⇒ 必须显式 --run-root。
    # medrec_cli 会把它转成 MEDREC_RUN_ROOT + bin/medrec_stage.py 垫片，六个阶段才一致。
    argv += ["--run-root", str(RUN_ROOT)]
    if steps and ",".join(steps) != ",".join(guards.STEPS_ALL):
        argv += ["--steps", ",".join(steps)]
    shown = (" ".join(shlex.quote(a) for a in argv) + f" > {log} 2>&1 < /dev/null &")
    return argv, shown


def spawn(src: Path, batch: str, steps: list[str] | None, fingerprint: str) -> dict:
    batch_dir = RUN_ROOT / batch          # 新批次只落写根（F2 已拦重名）
    log = batch_dir / "_webui" / "run.log"
    (batch_dir / "_webui").mkdir(parents=True, exist_ok=False)
    argv, shown = build_command(src, batch, steps, log)
    ok, why, _ = acquire_lock(batch, fingerprint, shown)   # 先锁再起（F1）
    if not ok:
        (batch_dir / "_webui").rmdir()
        raise RuntimeError(why)
    fh = open(log, "ab")
    try:
        proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=str(Path(MEDREC).parent.parent),
                                env=_clean_env())
        set_lock_pid(proc.pid)
    except OSError:
        release_lock()
        raise
    finally:
        fh.close()
    return {"pid": proc.pid, "command": shown, "log": str(log)}


PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def _clean_env() -> dict:
    env = dict(os.environ)
    for k in PROXY_KEYS:
        env.pop(k, None)
    env["no_proxy"] = "*"
    env["NO_PROXY"] = "*"
    return env


# ------------------------------------------------------------------ 进度（T5）
def log_dir(batch: str) -> Path:
    return batch_dir(batch) / "_pipeline_logs"


def run_meta(batch: str) -> dict:
    try:
        return json.loads((batch_dir(batch) / "_medrec" / "RUN_META.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def progress(batch: str) -> dict:
    bdir = batch_dir(batch)
    meta = run_meta(batch)
    lock = read_lock()
    # 起跑时刻：正在跑的取锁里的 started_at；跑完的 RUN_META.epoch 是**收尾**时刻，
    # 得减 wall_s 才是起点（否则 mtime 过滤会把整批日志筛掉）。
    if lock.get("batch") == batch and lock.get("started_at"):
        t0 = float(lock["started_at"])
    elif meta.get("epoch"):
        t0 = float(meta["epoch"]) - float(meta.get("wall_s") or 0) - 5.0
    else:
        t0 = 0.0
    cases = guards.manifest_cases(bdir / "00_index" / "manifest.tsv")
    total = int(meta.get("cases_planned") or len(cases))
    per_stage: dict[str, int] = {}
    done_ts: dict[str, float] = {}
    for f in glob.glob(str(log_dir(batch) / "*.log")):
        name = os.path.basename(f)[:-4]
        case, _, stage = name.rpartition("__")   # case_id 里本身含 "__" ⇒ 从右切
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if t0 and mt < t0 - 1:
            continue
        per_stage[stage] = per_stage.get(stage, 0) + 1
        done_ts[case] = max(done_ts.get(case, 0.0), mt)
    ts = sorted(done_ts.values())
    steady = None
    if len(ts) >= 11:
        steady = round((ts[-1] - ts[9]) / (len(ts) - 10), 4)
    elif ts:
        steady = None    # 例数 <11：稳态窗口算不出来（产线口径 keep=N-10），不猜
    finished = bool(meta)
    out = {
        "batch": batch,
        "state": "done" if finished else ("running" if lock.get("batch") == batch else "unknown"),
        "done": len(ts), "total": total,
        "stage_counts": per_stage,
        "steady_s_per_case_live": steady,
        "elapsed_min": round((time.time() - t0) / 60.0, 1) if t0 else None,
        "warnings": [pii.scrub_text(w) for w in meta.get("warnings", [])],
        "rc": meta.get("rc"),
        "stages": meta.get("stages", {}),
        "column_set": meta.get("column_set", {}),
        "key_params": meta.get("key_params", {}),
        "finished": finished,
    }
    if finished:
        out["steady_s_per_case"] = meta.get("stages", {}).get("steady_s_per_case")
    return out


def log_tail(batch: str, lines: int = 40, which: str = "run") -> dict:
    """日志尾部：一律过 pii.scrub_text（G4/A7：目录名里的姓名段掩成 ***）。"""
    p = (batch_dir(batch) / "_webui" / "run.log" if which == "run"
         else log_dir(batch) / f"{which}")
    if not p.is_file():
        return {"path": str(p), "lines": [], "error": "还没有这个日志"}
    try:
        raw = p.read_bytes()[-200_000:].decode("utf-8", "replace").splitlines()
    except OSError as exc:
        return {"path": str(p), "lines": [], "error": str(exc)}
    tail = [pii.scrub_text(ln) for ln in raw[-lines:]]
    return {"path": str(p), "lines": tail, "bytes_total": p.stat().st_size, "error": ""}


# ------------------------------------------------------------------ 历史批次
def _missing_rates(batch_dir: Path) -> tuple[int, int]:
    p = batch_dir / "05_qc" / "_summary" / "missing_rates.csv"
    filled = rows = 0
    try:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for rec in csv.DictReader(fh):
                filled += int(rec.get("n_filled") or 0)
                rows += int(rec.get("n_rows") or 0)
    except (OSError, ValueError):
        return 0, 0
    return filled, rows


def batches(limit: int = 40) -> list[dict]:
    dirs = []
    seen: set[str] = set()
    for root in RUN_ROOTS:
        if not root.is_dir():
            continue
        for p in root.iterdir():
            if not p.is_dir() or p.name.startswith("_") or p.name == "_ingest":
                continue
            if p.name in seen:
                continue          # 写根优先：同名批次不被旧根盖掉
            seen.add(p.name)
            try:
                dirs.append((p.stat().st_mtime, p))
            except OSError:
                continue
    dirs.sort(reverse=True)
    out = []
    for _mt, p in dirs[:limit]:
        meta = run_meta(p.name)
        try:
            man = json.loads((p / "06_table" / "_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            man = {}
        filled, nrows = _missing_rates(p)
        stages = meta.get("stages", {})
        out.append({
            "batch": p.name,
            "rows": man.get("rows"), "cols": man.get("columns"),
            "columns_yaml_version": man.get("columns_yaml_version"),
            "dedup_key": (man.get("dedup") or {}).get("key_form"),
            "hits": f"{filled}/{nrows}" if nrows else "",
            "warnings": len(meta.get("warnings", [])),
            "steady_s_per_case": stages.get("steady_s_per_case"),
            "rc": meta.get("rc"),
            "time": meta.get("ts") or time.strftime("%Y-%m-%d %H:%M", time.localtime(_mt)),
            "has_check": (p / "06_table" / "wide.csv").is_file(),
        })
    return out


# ------------------------------------------------------------------ F6 状态诚实
def drafts() -> dict:
    """扫 workspace/round*/patches/ 里**未落地**的 patch 草案（>0 ⇒ 页面黄条）。"""
    items = []
    for d in sorted(WORKSPACE.glob("round*/patches")):
        for f in sorted(d.iterdir()):
            if f.suffix not in (".patch", ".diff"):
                continue
            if f.name.startswith("LANDED"):
                continue
            items.append(str(f.relative_to(WORKSPACE.parent)))
    return {"count": len(items), "items": items}


_doctor_cache = {"ts": 0.0, "data": {}}


def doctor_info(ttl: float = 20.0) -> dict:
    now = time.time()
    if now - _doctor_cache["ts"] < ttl and _doctor_cache["data"]:
        return _doctor_cache["data"]
    out = {"columns_version": "unknown", "expect_wide_cols": None, "yaml_cols": None,
           "dedup_key": "unknown", "fail": None, "warn": None, "error": ""}
    try:
        res = subprocess.run([MEDREC, "doctor"], capture_output=True, text=True,
                             timeout=120, env=_clean_env())
        text = res.stdout + res.stderr
        for ln in text.splitlines():
            if ln.startswith("COLUMNS "):
                out["yaml_cols"], out["expect_wide_cols"] = _kv(ln, ["yaml", "expect_wide"])
                out["columns_version"] = _kv1(ln, "version")
            elif "去重键形态" in ln:
                out["dedup_key"] = _kv1(ln, "key")
            elif ln.startswith("=== doctor:"):
                out["fail"], out["warn"] = _kv(ln, ["FAIL", "WARN"])
        if res.returncode not in (0, 1):
            out["error"] = f"medrec doctor rc={res.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    _doctor_cache.update(ts=now, data=out)
    return out


def _kv1(line: str, key: str) -> str:
    for tok in line.replace("[", " [").split():
        if tok.startswith(f"{key}="):
            return tok.split("=", 1)[1]
    return "unknown"


def _kv(line: str, keys: list[str]) -> list[str]:
    return [_kv1(line, k) for k in keys]


def system_load() -> dict:
    la = os.getloadavg()
    mem = {}
    try:
        for ln in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            k, _, v = ln.partition(":")
            mem[k.strip()] = int(v.split()[0])
    except (OSError, ValueError):
        pass
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    return {"load1": round(la[0], 2), "load5": round(la[1], 2),
            "cpu_count": os.cpu_count(),
            "mem_total_mb": total // 1024, "mem_used_pct":
            round(100.0 * (total - avail) / total, 1) if total else None}
