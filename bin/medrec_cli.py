#!/usr/bin/env python3
"""medrec —— 病历取数产线的一条命令入口（run / check / doctor）。

  medrec run <病历目录>                        一条命令：扫目录 → locate→s1→…→s6 → 出宽表
                                             批次名缺省 = 目录名，产物 run/wide/<目录名>/06_table/wide.csv
  medrec run --cases FILE --batch TAG          按 case_id 列表跑（round25 用法，保留不动）
  medrec check <batch|目录> [--compare B]      只读核对产物（命中数 / 页数 / 日期 / 逐格 sha）
  medrec doctor [--cases FILE] [--batch TAG]   只读体检：8110 / 8080 / 显存 / config.env / cases

调试/对照旋钮（--serial --no-final-table --strict-mapping --no-locate-cache --force）
不在日常路径上：`medrec run --advanced` 列全，`run --help` 末段也有。

参数唯一来源：
  * scripts/config.env  —— 端口、模型名、K(R15_CASE_PARALLEL)、CONC/PCONC、
    R22_ESC_*、R23_OCR_MAX_TOKENS。本文件**不复制**这些值，只在 doctor 里回显。
  * config/pipeline.yaml —— paths.run_root、ocr.backend、ocr.vllm_url、llm.*。
    经 wide.common.load_pipeline() 现读，所以 doctor 看到的和产线跑的是同一份。

只读产线代码；产物只落在 run_root/<batch>/（run_root 可用 --run-root 指到别处）。
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import importlib
import json
import os
import subprocess
import sys
import re
import statistics
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("MEDREC_ROOT") or Path(__file__).resolve().parent.parent)
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

for _k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

STEPS_ALL = "locate,s1,s2,s3,s4,s5,s6"
# AGENTS.md §二 交付配置：8110 必须带 --max-num-seqs 1（确定性开关，不是性能参数）。
# 产线闸门 src/wide/s1_ocr.py:514 vllm_single_seq_replicas() 读的就是同一个值。
EXPECT_MAX_NUM_SEQS = 1
# doctor 要回显的 config.env 键（只回显，不复制取值）
ENV_KEYS = ("PORT_OCR_VL", "PORT_OCR_PADDLE", "PORT_QWEN", "MODEL_QWEN_NAME",
            "MODEL_OCR_VL_NAME", "R15_CASE_PARALLEL", "R23_OCR_CONCURRENCY",
            "R23_PROBE_CONCURRENCY", "R22_ESC_WINDOW", "R22_ESC_MAX_CALLS",
            "R23_OCR_MAX_TOKENS", "MEDREC_PY")


# ───────────────────────────────────────────────────────── 小工具
def _get_json(url: str, timeout: float = 5.0):
    return json.loads(_get_text(url, timeout))


def _get_text(url: str, timeout: float = 5.0) -> str:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer sk-local"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _http_status(url: str, timeout: float = 5.0) -> tuple[int | None, str]:
    """只要状态码（404/401/403 都是**有应答**，不是失败）。用于「功能开没开」这类探测。"""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer sk-local"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.status), ""
    except urllib.error.HTTPError as exc:      # noqa: PERF203
        return int(exc.code), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _proc_by_cmd(*needles: str) -> list[tuple[int, str]]:
    """按 cmdline 片段找 pid（只读 /proc）。判活用端口/命令行，不用 pgrep。"""
    hits = []
    for d in glob.glob("/proc/[0-9]*"):
        pid = int(d.split("/")[-1])
        if pid == os.getpid():
            continue
        try:
            cmd = Path(d, "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if all(n in cmd for n in needles):
            hits.append((pid, " ".join(cmd.split())))
    return hits


def _log_target(pid: int, fd: str = "1") -> str:
    try:
        return os.readlink(f"/proc/{pid}/fd/{fd}")
    except OSError:
        return ""


def _descendant_pids(roots: set[int]) -> set[int]:
    """roots 及其全部子孙 pid（只读 /proc/<pid>/stat 的 ppid）。

    `vllm serve` 的**真正干活的进程不是监听端口的那个**：监听父进程 1323792，
    显卡上是子进程 1324093（cmdline = `VLLM::EngineCore`，不含 `--port 8110`）。
    只按 cmdline 匹配 ⇒ 卡号查不到 ⇒ 「卡忙」整轮不测（round31 smoke 实测）。
    """
    kids: dict[int, list[int]] = {}
    for d in glob.glob("/proc/[0-9]*"):
        pid = int(d.split("/")[-1])
        try:
            st = Path(d, "stat").read_text(encoding="utf-8", errors="replace")
            ppid = int(st.rsplit(") ", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append(pid)
    out: set[int] = set()
    stack = list(roots)
    while stack:
        p = stack.pop()
        if p in out:
            continue
        out.add(p)
        stack.extend(kids.get(p, []))
    return out


def _md5(path: Path) -> str:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return "(unreadable)"


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "(unreadable)"


def _env_line(key: str) -> str:
    return f"{key}={os.environ.get(key, '(unset)')}"


def _ocr_serving_max_num_seqs(port_ocr: str) -> str:
    """8110 服务端 `--max-num-seqs` 的实测值（只读 /proc，查不到返回 "(缺)"）。

    AGENTS.md §二 交付配置 = 1，那是**确定性开关**不是性能参数；产线闸门
    src/wide/s1_ocr.py:514 vllm_single_seq_replicas() 读的是同一个值。
    不等于 1 ⇒ 并发下同一页两次会出不同正文 ⇒ 逐格 sha 的 0.4–0.7% 只是噪声带。
    """
    procs = _proc_by_cmd("vllm", "--port", port_ocr)
    if not procs:
        return "(缺)"
    m = re.search(r"--max-num-seqs(?:[= ]+)(\d+)", procs[0][1])
    return m.group(1) if m else "(缺)"


# ───────────────────────────────────────────────────────── doctor
DOC_AGENTS_MD = ROOT / "AGENTS.md"
STRATA_CONF_JSON = Path("/home/ubuntu/Strata/strata-iq3_s.json")
ENV_KEY_PREFIXES = ("R15_", "R22_", "R23_", "R24_", "OCR_VLLM_", "MEDREC_ABORT")


def _grep_lines(path: Path, needle: str, limit: int = 3) -> list[str]:
    """在文件里找含 needle 的行，返回 `文件:行号` 片段（红线③：结论要带证据）。"""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, ln in enumerate(fh, 1):
                if needle in ln:
                    out.append(f"{path.name}:{i}")
                    if len(out) >= limit:
                        break
    except OSError:
        return []
    return out


def _env_key_consumers(key: str) -> list[str]:
    """这个 env 键在 src/ bin/ scripts/ config/ 里有没有消费者（「死键」检测）。"""
    hits: list[str] = []
    for base in (SRC, ROOT / "bin", ROOT / "scripts", ROOT / "config"):
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.suffix not in (".py", ".sh", ".yaml", ".json"):
                continue
            if f.suffix == ".py" and ".pre" in f.name:
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if key in txt:
                hits.append(str(f.relative_to(ROOT)))
                if len(hits) >= 4:
                    break
        if len(hits) >= 4:
            break
    return hits


def _doctor_doc_vs_live(say, cfg: dict) -> None:
    """§9.4：把「文档写的」与「现网跑的」逐条对照，不一致给 WARN/FAIL。

    这个项目已经出现五次「文档/配置指向现实之外」，其中一次（pipeline.yaml 指向已死的
    8110）直接让管线跑不起来 ⇒ doctor 不能只打现值，必须**对照**。
    """
    print("--- 文档 vs 现网（round31 §9.4）---")
    # 1) --max-num-seqs：AGENTS.md vs 启动脚本默认 vs config.env vs 现网进程
    doc = _grep_lines(DOC_AGENTS_MD, "--max-num-seqs", 2)
    sh = _grep_lines(ROOT / "scripts" / "10-start-ocr.sh", "VLLM_DETERMINISM=", 2)
    cenv = _grep_lines(ROOT / "scripts" / "config.env", "OCR_VLLM_DETERMINISM=", 2)
    live_vals: list[str] = []
    for port in (os.environ.get("PORT_OCR_VL", "8110"),
                 os.environ.get("PORT_OCR_PADDLE", "8100")):
        for pid, cmd in _proc_by_cmd("--port", port):
            m = re.search(r"--max-num-seqs(?:[= ]+)(\d+)", cmd)
            live_vals.append(f"{port}(pid={pid})={m.group(1) if m else '(缺)'}")
    print(f"     AGENTS.md {doc} 写 --max-num-seqs {EXPECT_MAX_NUM_SEQS}"
          f"（确定性开关，AGENTS.md §二）")
    print(f"     scripts/10-start-ocr.sh {sh} 默认值 / scripts/config.env {cenv} 覆盖值")
    print(f"     现网进程 {live_vals or '(没有在跑的 OCR --serve/vllm)'}")
    nums = {v.split("=")[-1] for v in live_vals} - {"(缺)"}
    say(not nums or nums == {str(EXPECT_MAX_NUM_SEQS)},
        "DOC-vs-LIVE --max-num-seqs",
        f"文档={EXPECT_MAX_NUM_SEQS} 现网={sorted(nums) or 'n/a'}"
        + ("" if not nums or nums == {str(EXPECT_MAX_NUM_SEQS)}
           else " ⇒ 现网不是 AGENTS.md 的确定性配置：同一页两次可能出不同正文"),
        warn=True)

    # 2) pipeline.yaml 指的 OCR 端点 vs 实际在监听的端口
    ch = _ocr_channel(cfg)
    for e in ch["endpoints"]:
        say(e["healthy"], f"DOC-vs-LIVE ocr.backend={ch['backend']} → {e['url']}",
            f"{e['how']} max_num_seqs={e['max_num_seqs']}", warn=False)
    other = []
    for port in (os.environ.get("PORT_OCR_PADDLE", "8100"),
                 os.environ.get("PORT_OCR_VL", "8110")):
        if port not in [e["port"] for e in ch["endpoints"]]:
            ok, how = _probe_ocr_endpoint(f"http://127.0.0.1:{port}",
                                          "vllm" if port == os.environ.get("PORT_OCR_VL") else "paddle")
            other.append(f"{port}={'活但没被 pipeline.yaml 引用' if ok else '死'}")
    if other:
        say(True, "DOC-vs-LIVE 其它 OCR 端口", "; ".join(other), warn=True)

    # 3) GPU 分配：Strata 配置 vs AGENTS.md §二 的卡位
    try:
        scfg = json.loads(STRATA_CONF_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        say(False, "DOC-vs-LIVE Strata gpu", f"读不到 {STRATA_CONF_JSON}: {exc}", warn=True)
        scfg = {}
    got_gpu = scfg.get("gpu")
    doc_gpu = [1, 2, 3]      # AGENTS.md §二：「GPU1/2/3 = Strata(8080)」
    print(f"     {STRATA_CONF_JSON}:\"gpu\"={got_gpu}  AGENTS.md §二 写 {doc_gpu}"
          f"（{DOC_AGENTS_MD.name} 卡位段）")
    say(got_gpu == doc_gpu, "DOC-vs-LIVE Strata gpu",
        f"配置={got_gpu} 文档={doc_gpu}"
        + ("" if got_gpu == doc_gpu else " ⇒ 文档卡位过期（AGENTS.md §二 需人更新）"),
        warn=True)

    # 4) config.env 死键：每个键在 src/ bin/ scripts/ config/ 里有没有消费者
    dead: list[str] = []
    checked = 0
    try:
        txt = (ROOT / "scripts" / "config.env").read_text(encoding="utf-8", errors="replace")
    except OSError:
        txt = ""
    for m in re.finditer(r"^export\s+([A-Z][A-Z0-9_]+)=", txt, re.M):
        key = m.group(1)
        if not key.startswith(ENV_KEY_PREFIXES):
            continue
        checked += 1
        if not _env_key_consumers(key):
            dead.append(key)
    say(not dead, f"DOC-vs-LIVE config.env 死键（查 {checked} 个 {ENV_KEY_PREFIXES} 前缀键）",
        ("无死键" if not dead else f"无消费者={dead} ⇒ 设了没人读，改了不影响行为"),
        warn=True)


def cmd_doctor(a) -> int:
    fails = warns = 0

    def say(ok: bool, name: str, evidence: str, warn: bool = False) -> None:
        nonlocal fails, warns
        tag = "OK  " if ok else ("WARN" if warn else "FAIL")
        print(f"{tag} {name}: {evidence}")
        if not ok:
            if warn:
                warns += 1
            else:
                fails += 1

    print(f"=== medrec doctor  {time.strftime('%F %T')} ===")
    print(f"     argv={' '.join(sys.argv[1:]) or '(none)'}")

    # 1) config.env
    cenv = ROOT / "scripts" / "config.env"
    say(cenv.is_file(), "config.env", f"{cenv} md5={_md5(cenv)}")
    for k in ENV_KEYS:
        print(f"     {_env_line(k)}")

    # 2) pipeline.yaml（走产线同一个 loader，不是自己 parse）
    try:
        import wide.common as C
        cfg = C.load_pipeline()
        run_root = Path(cfg["paths"]["run_root"])
        ocr = cfg.get("ocr") or {}
        llm = cfg.get("llm") or {}
        print(f"     pipeline.yaml version={cfg.get('version')} "
              f"md5={_md5(ROOT / 'config' / 'pipeline.yaml')}")
        print(f"     paths.run_root={run_root}  ocr.backend={ocr.get('backend')} "
              f"ocr.vllm_url={ocr.get('vllm_url')} ocr.vllm_model={ocr.get('vllm_model')} "
              f"ocr.vllm_urls={ocr.get('vllm_urls')}")
        print(f"     llm.base_url={llm.get('base_url')} llm.model={llm.get('model')} "
              f"llm.enable_thinking={llm.get('enable_thinking')}")
        say(True, "pipeline.yaml", f"loaded via wide.common.load_pipeline()")
    except Exception as exc:  # noqa: BLE001
        say(False, "pipeline.yaml", f"{type(exc).__name__}: {exc}")
        cfg, run_root = {}, ROOT / "run" / "wide"

    # 3) 解释器 / src 可导入
    say(SRC.is_dir(), "src", f"{SRC} exists; import wide.tools.run_steps ...", )
    try:
        importlib.import_module("wide.tools.run_steps")
        print(f"     interpreter={sys.executable} version={sys.version.split()[0]}")
    except Exception as exc:  # noqa: BLE001
        say(False, "src import", f"{type(exc).__name__}: {exc}")

    # 4) OCR 端点 8110
    port_ocr = os.environ.get("PORT_OCR_VL", "8110")
    url_ocr = f"http://127.0.0.1:{port_ocr}"
    try:
        d = _get_json(url_ocr + "/v1/models")
        ids = [m.get("id") or m.get("model") for m in (d.get("data") or [])]
        say(True, f"OCR {port_ocr} /v1/models", f"models={ids}")
    except Exception as exc:  # noqa: BLE001
        say(False, f"OCR {port_ocr} /v1/models", f"{type(exc).__name__}: {exc}")
    procs = _proc_by_cmd("vllm", "--port", port_ocr)
    if procs:
        pid, cmd = procs[0]
        key = [x for x in ("--max-num-seqs", "--max-model-len", "--gpu-memory-utilization",
                           "--no-enable-prefix-caching", "--limit-mm-per-prompt",
                           "--max-num-batched-tokens") if x in cmd]
        say(True, f"OCR {port_ocr} cmdline", f"pid={pid} log={_log_target(pid)}")
        print(f"     flags={key}")
        print(f"     cmdline={cmd}")
        # 5.4 —— 把 --max-num-seqs 的实际值与 AGENTS.md 写的 1 对照，不一致标 WARN
        m = re.search(r"--max-num-seqs(?:[= ]+)(\d+)", cmd)
        got = m.group(1) if m else "(缺)"
        say(got == str(EXPECT_MAX_NUM_SEQS), f"OCR {port_ocr} --max-num-seqs",
            f"实测={got} AGENTS.md §二 交付配置={EXPECT_MAX_NUM_SEQS}"
            + ("" if got == str(EXPECT_MAX_NUM_SEQS) else
               " → 不是确定性开关：并发下同一页两次可能出不同正文；"
               "需要人按 AGENTS.md 重启 8110（medrec 不会重启服务）"),
            warn=True)
    else:
        say(False, f"OCR {port_ocr} cmdline", "no /proc cmdline matches 'vllm --port "
            + port_ocr + "'")
    try:
        raw = _get_text(url_ocr + "/metrics", timeout=5.0)
        rows = [ln for ln in raw.splitlines() if ln.startswith("vllm:num_requests_")]
        say(True, f"OCR {port_ocr} /metrics",
            f"prometheus bytes={len(raw)} running/waiting={rows[:4] or '(none)'}")
    except Exception as exc:  # noqa: BLE001
        say(False, f"OCR {port_ocr} /metrics", f"{type(exc).__name__}: {exc}", warn=True)

    # 5) 抽取端点 8080
    port_llm = os.environ.get("PORT_QWEN", "8080")
    url_llm = f"http://127.0.0.1:{port_llm}"
    want_model = os.environ.get("MODEL_QWEN_NAME", "")
    try:
        d = _get_json(url_llm + "/v1/models")
        ids = [m.get("id") or m.get("model") for m in (d.get("data") or [])]
        ok = (want_model in ids) if want_model else bool(ids)
        say(ok, f"LLM {port_llm} /v1/models",
            f"models={ids} expected={want_model!r} (config.env MODEL_QWEN_NAME)")
    except Exception as exc:  # noqa: BLE001
        say(False, f"LLM {port_llm} /v1/models", f"{type(exc).__name__}: {exc}")
    try:
        st = _get_json(url_llm + "/v1/status")
        say(True, f"LLM {port_llm} /v1/status",
            f"concurrency={st.get('concurrency')}")
    except Exception as exc:  # noqa: BLE001
        say(False, f"LLM {port_llm} /v1/status", f"{type(exc).__name__}: {exc}", warn=True)
    try:
        m = _get_json(url_llm + "/metrics")
        eng, live = m.get("engine") or {}, m.get("live") or {}
        print(f"     engine.batch_slots={eng.get('batch_slots')} "
              f"expert_slots={eng.get('expert_slots')} "
              f"expert_cache_mib={eng.get('expert_cache_mib')} "
              f"vram_free_mib={eng.get('vram_free_mib')}")
        print(f"     live.parallel={live.get('parallel')} running={live.get('running')} "
              f"waiting={live.get('waiting')} slots={[s.get('state') for s in (live.get('slots') or [])]} "
              f"totals.requests={(m.get('totals') or {}).get('requests')}")
        say(True, f"LLM {port_llm} /metrics", "reachable")
    except Exception as exc:  # noqa: BLE001
        say(False, f"LLM {port_llm} /metrics", f"{type(exc).__name__}: {exc}", warn=True)

    # 6) GPU 显存
    try:
        out = subprocess.run(["nvidia-smi",
                              "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
        for ln in out.stdout.strip().splitlines():
            print(f"     {ln}")
            f = [x.strip() for x in ln.split(",")]
            if len(f) >= 5 and f[3].endswith("MiB") and f[4].endswith("MiB"):
                used = int(f[3].split()[0]); total = int(f[4].split()[0])
                if used / total > 0.95:
                    say(False, f"GPU{f[0]} memory", f"{used}/{total} MiB used (>95% full)", warn=True)
    except Exception as exc:  # noqa: BLE001
        say(False, "nvidia-smi", f"{type(exc).__name__}: {exc}")

    # 7) cases 文件 + 源 manifest 覆盖
    if a.cases:
        p = Path(a.cases)
        if p.is_file():
            ids = [x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
            say(True, "cases file", f"{p} n={len(ids)} md5={_md5(p)} first={ids[:2]}")
            man = ROOT / "run" / "wide" / "allmap" / "00_index" / "manifest.tsv"
            if man.is_file():
                with open(man, encoding="utf-8") as f:
                    have = {r.get("case_id") for r in csv.DictReader(f, delimiter="\t")}
                miss = [x for x in ids if x not in have]
                say(not miss, "cases ⊂ source manifest",
                    f"manifest={man} n={len(have)} missing={len(miss)} {miss[:3]}")
            else:
                say(False, "cases ⊂ source manifest", f"no manifest at {man}", warn=True)
        else:
            say(False, "cases file", f"not found: {p}")

    # 8) 产物目录可写
    batch = a.batch or "<batch>"
    tgt = (Path(a.run_root) if a.run_root else run_root) / batch
    parent = tgt.parent
    say(os.access(parent, os.W_OK), "run dir writable",
        f"{parent} (batch={batch}; --run-root overrides pipeline.yaml paths.run_root)")

    # 8.5) round34 E1：列集 / 删列 / 去重键形态（「代码已改、配置未改」必须一眼可见）
    _doctor_columns_and_dedup(say, cfg)

    # 9) round31 §9.4：文档 vs 现网 对照器
    _doctor_doc_vs_live(say, cfg)

    print(f"=== doctor: FAIL={fails} WARN={warns} ===")
    return 1 if fails else 0


# ───────────────────────────────────── round34 E1：列集 / 删列 / 去重键 自检
def _column_defs() -> list:
    """columns.yaml 的列定义（id + header）。唯一来源，不另造第二份。"""
    C = importlib.import_module("wide.common")
    return list(C.load_columns().get("columns") or [])


def _drop_ids_of_run() -> list:
    """本次宽表应当**删掉**的列 id —— 与 s6 用同一个解析器，不另造第二套。

    缺 normalize.drop_output_columns ⇒ 用 s6 的默认（round33 §1.4）。
    这样「bin 的 patch 先落、columns.yaml 后改」时 check 的基准仍然对得上。
    """
    try:
        cfg = importlib.import_module("wide.common").load_pipeline()
    except Exception:  # noqa: BLE001
        cfg = {}
    return importlib.import_module("wide.s6_table").resolve_drop_ids(cfg, False)


def _align_wide_to_columns(head: list, cols: list) -> tuple[list, list]:
    """wide.csv 表头 → columns.yaml id：**按顺序**做子序列匹配，不按位置硬对。

    round33 的 check 按**下标**对齐 ⇒ 删一列后整体左移，日期检查既假 FAIL 又真漏检。
    表头里有 2 组重名列（姓名 / 序号）⇒ 按名字查会串，按顺序匹配不会。
    返回 (aligned_ids, errors)；aligned_ids[i] = head[i] 的 id，对不上为 ""。
    """
    ids = [str(c.get("id") or "") for c in cols]
    heads = [str(c.get("header") or "") for c in cols]
    aligned = [""] * len(head)
    errs: list[str] = []
    j = 0
    for i, h in enumerate(head):
        while j < len(ids) and heads[j] != h:
            j += 1
        if j >= len(ids):
            errs.append(f"wide 第 {i + 1} 列表头 {h!r} 在 columns.yaml 里找不到（列序变了？）")
            continue
        aligned[i] = ids[j]
        j += 1
    return aligned, errs


def _doctor_columns_and_dedup(say, cfg: dict) -> None:
    """round34 E1：把「本次会出的列数 / 就诊医院 是否已删 / 去重键形态」写成证据。

    以前「24 列 = 新链」是新旧链判据，删列之后失效（round34 E2）⇒ 这里报**基准**
    （columns.yaml 全量 − normalize.drop_output_columns），不是记一个写死的数。
    """
    try:
        S6 = importlib.import_module("wide.s6_table")
        cols = _column_defs()
        ids = [str(c.get("id") or "") for c in cols]
        drop = _drop_ids_of_run()
        key_form = S6.resolve_dedup_key_form(cfg, None)
        expect = len(ids) - len([x for x in drop if x in set(ids)])
        ver = importlib.import_module("wide.common").load_columns().get("version")
        print(f"COLUMNS yaml={len(ids)} drop={drop} expect_wide={expect} version={ver}")
        say("hospital" in drop, "就诊医院 已移除",
            f"normalize.drop_output_columns={drop}（缺键=默认 {list(S6.DEFAULT_DROP_IDS)}）"
            + ("" if "hospital" in drop else
               " → 现网宽表仍带 就诊医院，与 round33 §1.4 不一致（人来改 pipeline.yaml/columns.yaml）"),
            warn=True)
        say(key_form in S6.DEDUP_KEY_FORMS, "去重键形态",
            f"key={key_form} 分量={list(S6.DEDUP_COMPONENTS)} "
            f"（缺键=默认 {S6.DEFAULT_DEDUP_KEY_FORM}；triple=旧键，只用于对照跑批）")
    except Exception as exc:  # noqa: BLE001
        say(False, "columns/dedup state", f"{type(exc).__name__}: {exc}", warn=True)


# ───────────────────────────────────────────────────────── run
ADVANCED_HELP = """调试 / 对照旋钮（日常路径不用；round26 §5.2 从 `run --help` 的主表收进来）
  --serial              整批串行跑法（默认走 wide.tools.run_steps --pipeline 按例流水线）—— 排错用
  --no-final-table      只留增量 wide.csv，不跑收尾全量 s6（验收「增量 == 全量」用）
  --strict-mapping      s4 不加 --allow-draft（只认人工确认过的映射表）
  --no-locate-cache     locate 忽略缓存 —— **只有测提速才开**，日常开会把缓存收益算没
  --force               各阶段强制重跑（默认 = 续跑，已完成的阶段跳过）
  --ocr-log-pattern P   OCR 请求计数日志的行片段（默认 POST /v1/chat/completions）
  --log-dir D           每例每步日志目录（默认 <run_root>/<batch>/_pipeline_logs，稳态 s/例 也读它）
  --detach              真后台跑（setsid + 打印日志路径），跑完 `medrec stages <batch>` 看阶段速度表
  --setsid/--no-setsid  脱离/不脱离父 shell 的作业控制（默认：stdout 不是终端时自动脱离）
  --no-sample           关掉事中采样（默认开）；--sample-interval S / --max-poll N 调它
  --max-poll N          事中污染阈值：跑期间完成的**外部** Strata 请求数 > N ⇒ 标 WARN（默认 0）
  --busy-wait-s S       前置闸门②（Strata 有人在用）最多等 S 秒直到空闲（默认 90，每 10 s 复查）
  --ignore-busy         前置闸门②不等待，直接开跑
  --compare-with B      跑完与批次 B 做逐格 sha 对照（默认自动找上一次同配置跑）

这些都不改变抽到的字段，只改变跑法/落点；改字段语义的是 config/pipeline.yaml，不是 CLI。
"""


def _resolve_input(a) -> tuple[int, str]:
    """输入源解析（round26 §4）：位置参数 <目录> → 扫描根 + 缺省批次名 = 目录名。

    --case / --cases / --steps / --run-root 的语义保持 round25 原样。
    """
    a.dir_root = ""
    a.batch_derived = False
    a.manifest_ids = None
    if getattr(a, "dir", ""):
        p = Path(a.dir).expanduser()
        if not p.exists():
            return 2, (f"ERROR 目录不存在：{a.dir}\n"
                       f"      先 `ls {Path(a.dir).parent if str(a.dir).count('/') else '.'}` 确认路径；"
                       f"按 case_id 跑用 `medrec run --case ID --batch TAG`；"
                       f"体检用 `medrec doctor`")
        if not p.is_dir():
            return 2, (f"ERROR 不是目录：{a.dir}\n"
                       f"      `medrec run` 的位置参数只接**病历目录**（下面按 院区/病例/*.pdf 摆）；"
                       f"单例用 `--case ID`，清单用 `--cases FILE`")
        a.dir_root = str(p.resolve())
    if not a.batch:
        if not a.dir_root:
            return 2, ("ERROR 没给 --batch，也没给目录 → 批次名无从派生。\n"
                       "      要么 `medrec run <病历目录>`（批次名 = 目录名），"
                       "要么 `medrec run --cases FILE --batch TAG`")
        derived = Path(a.dir_root).name
        if not derived or derived in (".", ".."):
            return 2, f"ERROR 无法从目录名派生批次名：{a.dir_root}（换个正常的目录，或显式 --batch TAG）"
        a.batch = derived
        a.batch_derived = True
    if not (a.dir_root or a.cases or a.case):
        return 2, ("ERROR 不知道要跑什么：既没有 <目录>，也没有 --case/--cases。\n"
                   "      `medrec run <病历目录>`｜`medrec run --case ID --batch TAG`｜"
                   "已有批次只重出表：`medrec run --batch TAG --steps s6`")
    return 0, ""


def _build_run_argv(a) -> list[str]:
    argv = ["--batch", a.batch, "--steps", a.steps]
    if a.case:
        for c in a.case:
            argv += ["--case", c]
    if a.cases:
        argv += ["--case-file", a.cases]
    if a.force:
        argv += ["--force"]
    if not a.strict_mapping:
        argv += ["--allow-draft"]
    if a.no_locate_cache:
        argv += ["--no-locate-cache"]
    if not a.serial:
        argv += ["--pipeline"]
    if a.no_final_table:
        argv += ["--no-final-table"]
    if a.ocr_log:
        argv += ["--ocr-log", a.ocr_log, "--ocr-log-pattern", a.ocr_log_pattern]
    if a.json_out:
        argv += ["--json-out", a.json_out]
    if a.log_dir:
        argv += ["--log-dir", a.log_dir]
    # --tag 只是 run_steps 写进 JSON 的「状态标签」（src/wide/tools/run_steps.py:352 定义、
    # :440 只回写进 out["tag"]），不参与路径、不参与任何判据 → 与 --batch 功能重复，
    # 本轮从 CLI 上删掉；这里自动填 batch 名，summary.json 里的 tag 字段仍然自描述。
    argv += ["--tag", a.batch]
    return argv


def _report_table(batch_dir: Path) -> None:
    """出表后回显行数/列数/非空格数 + 主诉/查体非空例数。只打计数，不打正文（红线⑨）。"""
    wide = batch_dir / "06_table" / "wide.csv"
    if not wide.is_file():
        print(f"TABLE missing: {wide}")
        return
    with open(wide, encoding="utf-8") as f:
        rd = csv.reader(f)
        head = [h.lstrip("\ufeff") for h in next(rd, [])]
        rows = [r for r in rd if r]
    if not rows or not head:
        print(f"TABLE {wide} rows=0")
        return
    # csv.reader 而非 DictReader：columns.yaml **故意**保留 2 组重名列（姓名 / 序号），
    # DictReader 的 keys() 会折叠重名列 → 列数与非空格少算（实测 23/920 vs 24/960）。
    def cell(r, i):
        return r[i] if i < len(r) else ""
    nonempty = sum(1 for r in rows for i in range(len(head)) if cell(r, i).strip())
    line = (f"TABLE {wide} rows={len(rows)} cols={len(head)} "
            f"nonempty_cells={nonempty}/{len(rows) * len(head)}")
    for key in ("主诉", "查体"):
        for i, name in enumerate(head):
            if name == key:
                line += f" {key}={sum(1 for r in rows if cell(r, i).strip())}/{len(rows)}"
                break
    print(line)


# ───────────────────────────────────────────────────────── check
DATE_STRICT = re.compile(r"^\d{4}年(0[1-9]|1[0-2])月(0[1-9]|[12]\d|3[01])日$")
DATE_COL_IDS = ("admit_time_raw", "admit_time_std", "admit_time_std_dup", "discharge_date")
DATE_ISO = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])( \d{2}:\d{2})?$")
# 原文列 = **照抄原文**（round33 三修 date_norm=verbatim）⇒ 允许中文带时分、允许空格、
# 允许月日不补零，也允许原文本来就是 ISO。实测形态见 workspace/round34/E3_date_shapes.out。
DATE_CN_ORIG = re.compile(r"^\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
                          r"(\s*\d{1,2}\s*(?:时|点)(\s*\d{1,2}\s*分?)?)?$")
DATE_CN_OR_ISO = re.compile(DATE_CN_ORIG.pattern + r"|^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
                            r"( \d{2}:\d{2}(:\d{2})?)?$")
# round34 E3(c)：日期判据必须**按列**分档 —— 三修之后标准化列是 ISO、原文列照抄原文，
# 用同一个 DATE_STRICT（只认中文）判全部 4 列 ⇒ 现网必然假 FAIL（round34 A2 实测）。
DATE_STRICT_BY_ID = {
    "admit_time_raw": (DATE_CN_OR_ISO, "columns.yaml allowed=YYYY年MM月DD日 或 原文"),
    "discharge_date": (DATE_CN_OR_ISO, "columns.yaml allowed=YYYY年MM月DD日 或 原文"),
    "admit_time_std": (DATE_ISO, "round33 三修：标准化列 = yyyy-MM-dd[ HH:MM]"),
    "admit_time_std_dup": (DATE_ISO, "同上（模板第 19 列，与第 18 列同源同值）"),
}


def _date_pattern(col_id: str):
    """该日期列应当匹配的正则；不是日期列 ⇒ None（跳过）。"""
    hit = DATE_STRICT_BY_ID.get(col_id or "")
    return hit[0] if hit else None


def _run_root(a) -> Path:
    if getattr(a, "run_root", ""):
        return Path(a.run_root).expanduser().resolve()
    return Path(importlib.import_module("wide.common").load_pipeline()["paths"]["run_root"])


def _batch_dir_of(a, name: str) -> Path:
    """batch 名或目录都行：能当目录用就当目录，否则按 <run_root>/<name> 解析。"""
    p = Path(name).expanduser()
    if p.is_dir():
        return p.resolve()
    return _run_root(a) / name


def _column_ids() -> list[str]:
    C = importlib.import_module("wide.common")
    return [c["id"] for c in C.load_yaml(ROOT / "config" / "columns.yaml")["columns"]]


def _cell_sha(batch_dir: Path) -> dict:
    """`<case>|<col_id>` → sha16(值)。只落 sha，不落正文（红线⑨）。"""
    out: dict[str, str] = {}
    ids = _column_ids()
    norm = batch_dir / "04_norm"
    for d in sorted(norm.iterdir()) if norm.is_dir() else []:
        fj = d / "fields.json"
        if not fj.is_file():
            continue
        try:
            blob = json.loads(fj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        fields = blob.get("fields") or {}
        for cid in ids:
            v = (fields.get(cid) or {}).get("value")
            if v is None:
                continue
            out[f"{d.name}|{cid}"] = hashlib.sha1(str(v).encode("utf-8")).hexdigest()[:16]
    return out


def _scan_trace_sha(batch_dir: Path) -> dict:
    """逐例 scan_trace 的 sha —— 口径照抄 workspace/round25/scripts/cmp25.py:45-58。

    基线「scan_trace 0」= 同配置两臂 trace_sha_differ=0（workspace/round25/delivery_state.md:61）。
    """
    p = batch_dir / "00_index" / "page_index.json"
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cases = d.get("cases") if isinstance(d, dict) else d
    rows: dict[str, dict] = {}
    for cid, e in (cases.items() if isinstance(cases, dict) else []):
        e = e or {}
        st = e.get("scan_trace") or []
        rows[cid] = {"n": len(st), "hit": sum(1 for x in st if x.get("hit")),
                     "fp": e.get("frontpage_pages"), "pn": e.get("first_progress_pages"),
                     "trace": hashlib.sha256(json.dumps(
                         [[x.get("page"), x.get("phase"), x.get("hit"),
                           x.get("strip_frac"), x.get("positions")] for x in st],
                         ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]}
    return rows


def cmd_check(a) -> int:
    """只读核对一个批次的产物 —— 跑完不用翻文档就知道对不对（round26 §7.4）。"""
    b = _batch_dir_of(a, a.batch)
    fails: list[str] = []
    warns: list[str] = []
    print(f"=== medrec check  {time.strftime('%F %T')} ===")
    print(f"CHECK batch={a.batch} dir={b}")
    if not b.is_dir():
        print(f"FAIL 批次目录不存在：{b}"
              "（`medrec run <目录>` 的产物在 <run_root>/<目录名>/；--run-root 覆盖过就带上）")
        return 1

    cols = _column_defs()
    ids = [str(c.get("id") or "") for c in cols]
    pi_path = b / "00_index" / "page_index.json"
    pi = json.loads(pi_path.read_text(encoding="utf-8")) if pi_path.is_file() else {}
    cases_pi = pi.get("cases") or {}
    shard_dir = b / "00_index" / "page_index"
    shards = sorted(shard_dir.glob("*.json")) if shard_dir.is_dir() else []
    print(f"PI_CASES={len(cases_pi)} SHARDS={len(shards)}/{len(cases_pi)} "
          f"n_shards_field={pi.get('n_shards')}")
    if len(shards) != len(cases_pi):
        fails.append(f"SHARDS {len(shards)} != PI_CASES {len(cases_pi)}")

    ocr_root = b / "01_ocr"
    ocr_cases = pages = 0
    if ocr_root.is_dir():
        for d in ocr_root.iterdir():
            if not d.is_dir():
                continue
            ocr_cases += 1
            pages += sum(1 for f in d.glob("*.json") if not f.name.startswith("_"))
    print(f"OCR_CASES={ocr_cases} SIGMA_PAGES={pages}")

    scanned = sum(len((cases_pi.get(c) or {}).get("scan_trace") or []) for c in cases_pi)
    hit = sum(1 for c in cases_pi
              for x in ((cases_pi.get(c) or {}).get("scan_trace") or []) if x.get("hit"))
    print(f"SCAN_TRACE cases={len(cases_pi)} scanned={scanned} hit={hit}")

    wp = b / "06_table" / "wide.csv"
    if not wp.is_file():
        fails.append(f"缺 {wp}")
        print(f"WIDE=MISSING {wp}")
    else:
        rows = list(csv.reader(open(wp, encoding="utf-8-sig")))
        head, data = rows[0], [r for r in rows[1:] if r]
        drop = _drop_ids_of_run()
        expect_cols = len(ids) - len([x for x in drop if x in set(ids)])
        print(f"WIDE rows={len(data)} cols={len(head)} expect={expect_cols} "
              f"(columns.yaml {len(ids)} - drop_output_columns {drop})")
        if len(head) != expect_cols:
            fails.append(f"wide.csv 列数 {len(head)} != columns.yaml {len(ids)} - "
                         f"normalize.drop_output_columns {drop} = {expect_cols}")
        hits = sum(1 for r in data for v in r if (v or "").strip())
        print(f"HITS={hits}/{len(head) * max(len(data), 1)}")
        idx = {h: i for i, h in enumerate(head)}
        for name in ("主诉", "查体"):
            if name in idx:
                ne = sum(1 for r in data if (r[idx[name]] or "").strip())
                print(f"NONEMPTY {name}={ne}/{len(data)}")
                if ne < len(data):
                    warns.append(f"{name} 有空值 {ne}/{len(data)}")
        # round34 E3(a)：日期列按**表头顺序**对到 columns.yaml 的 id（旧写法按下标硬对
        # ⇒ 删一列后整体左移，既假 FAIL 又真漏检；表头还有 2 组重名列，按名字查会串）
        aligned, align_errs = _align_wide_to_columns(head, cols)
        for msg in align_errs:
            fails.append(f"表头对齐失败：{msg}")
        checked = [head[i] for i, cid in enumerate(aligned) if cid in DATE_COL_IDS]
        print(f"DATE_COLS_CHECKED={json.dumps(checked, ensure_ascii=False)}")
        bad = []
        for i, cid in enumerate(aligned):
            pat = _date_pattern(cid)
            if pat is None:
                continue
            for j, r in enumerate(data):
                v = (r[i] if i < len(r) else "").strip()
                if v and not pat.match(v):
                    bad.append((head[i], j + 1))
        print(f"DATE_NONSTRICT={len(bad)}")
        for x in bad[:10]:
            print(f"   BAD {x[0]} row={x[1]}")
        if bad:
            fails.append(f"日期非严格 {len(bad)} 格")
        if cases_pi and len(data) != len(cases_pi):
            warns.append(f"wide 行数 {len(data)} != PI_CASES {len(cases_pi)}")

    qf = b / "05_qc" / "_summary" / "qc_flags.csv"
    kinds: dict[str, int] = {}
    if qf.is_file():
        for r in csv.DictReader(open(qf, encoding="utf-8-sig")):
            k = r.get("kind") or ""
            kinds[k] = kinds.get(k, 0) + 1
    print("QC_KINDS=" + json.dumps(kinds, ensure_ascii=False, sort_keys=True))
    if kinds.get("provenance_rejected", 0):
        # 来源守卫（round10 落地，默认开）判坏是**预期行为**，基线 906–907/960 已含它 → 记 WARN 不记 FAIL
        warns.append(f"provenance_rejected={kinds['provenance_rejected']}"
                     "（来源守卫 G3 判坏，AGENTS.md §来源守卫；基线命中数已含此代价）")

    sha = _cell_sha(b)
    only = _case_list(a)
    if only:
        keep = set(only)
        sha = {k: v for k, v in sha.items() if k.split("|", 1)[0] in keep}
        print(f"ONLY_CASES={len(keep)} cells_in_scope={len(sha)}")
    out = Path(a.cell_sha_out).expanduser() if a.cell_sha_out else b / "_medrec" / "cell_sha.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sha, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(f"CELL_SHA_FILE={out} n={len(sha)}")
    if a.compare:
        b2 = _batch_dir_of(a, a.compare)
        s2 = _cell_sha(b2)
        if only:
            keep = set(only)
            s2 = {k: v for k, v in s2.items() if k.split("|", 1)[0] in keep}
        got = _ocr_serving_max_num_seqs(os.environ.get("PORT_OCR_VL", "8110"))
        g = _cell_diff_gate(sha, s2, b, b2, got)
        common, diff, rate = g["common"], g["diff_keys"], g["diff_rate_pct"]
        print(f"COMPARE A={b} B={b2}")
        print(f"CELLS A={len(sha)} B={len(s2)} common={common} "
              f"onlyA={len(set(sha) - set(s2))} onlyB={len(set(s2) - set(sha))} "
              f"diff={len(diff)} diff_rate={rate:.2f}%")
        for k in diff[:10]:
            print(f"   DIFF {k}")
        ta, tb = _scan_trace_sha(b), _scan_trace_sha(b2)
        keys = sorted(set(ta) | set(tb))
        if only:
            keys = [k for k in keys if k in set(only)]
        dtr = [k for k in keys if ta.get(k, {}).get("trace") != tb.get(k, {}).get("trace")]
        dfp = [k for k in keys if ta.get(k, {}).get("fp") != tb.get(k, {}).get("fp")]
        dpn = [k for k in keys if ta.get(k, {}).get("pn") != tb.get(k, {}).get("pn")]
        print(f"SCAN_TRACE cases={len(keys)} trace_sha_differ={len(dtr)} "
              f"fp_differ={len(dfp)} pn_differ={len(dpn)}")
        for k in dtr[:10]:
            print(f"   TRACE {k} {ta.get(k, {}).get('n')}/{ta.get(k, {}).get('hit')}"
                  f" -> {tb.get(k, {}).get('n')}/{tb.get(k, {}).get('hit')}")
        if got != str(EXPECT_MAX_NUM_SEQS):
            print(f"NOTE 8110 --max-num-seqs={got}（AGENTS.md §二 交付配置={EXPECT_MAX_NUM_SEQS}）"
                  f" → 服务端不在确定性模式：同一页两次会出不同正文，"
                  f"所以下面这个 sha 差异率是**噪声带**不是硬地板；"
                  f"判「有没有改行为」看 SCAN_TRACE（0 = 选页没变）+ 上面的命中数/主诉·查体。"
                  f" 要硬地板需人按 AGENTS.md 重启 8110（medrec 不重启任何服务）。")
        # 判红口径与 `medrec run` 的 §9.3 自动对照**同一个函数**（_cell_diff_gate）：
        # 噪声带上沿 + 输入顺序不同时剔除 seq_no。旧的常数 0.7% 只在
        # OCR 确定性开关（--max-num-seqs 1）在的时候成立，现网 64 ⇒ 会把干净对判红。
        lo, hi = g["noise_band_pct"]
        print(f"GATE diff_rate={rate:.3f}% 剔除_seq_no={g['diff_rate_excl_seq_no_pct']:.3f}% "
              f"输入顺序{'相同' if g['same_input_order'] else '不同'}"
              f" ⇒ 判红口径={g['gate_rate_pct']:.3f}% 噪声带上沿={hi}%"
              f"（{g['noise_band_why']}）")
        if g["above_band"]:
            fails.append(f"逐格 sha 差异率 {g['gate_rate_pct']:.2f}% > 同配置噪声带上沿 {hi}%"
                         f"（全量 {rate:.2f}%；输入顺序{'相同' if g['same_input_order'] else '不同'}"
                         f"{'，已剔除 seq_no' if not g['same_input_order'] else ''}）")
        elif g["gate_rate_pct"] > lo:
            warns.append(f"逐格 sha 差异率 {g['gate_rate_pct']:.2f}% 高于噪声地板 {lo}%"
                         f"（带内，仅诊断；上沿 {hi}%）")

    print(f"=== check: FAIL={len(fails)} WARN={len(warns)} ===")
    for x in fails:
        print(f"  FAIL {x}")
    for x in warns:
        print(f"  WARN {x}")
    return 1 if fails else 0


# ──────────────────────────────────────────────────────────────────────────────────────
# round31：把「跑的时候机器上有什么」变成可观测 + 有守卫
#
# 动机（round31 §9）：2.7 s/例 达成过三次（round24 2.672 / round27 2.667 / round28 2.735），
# 但 round25 3.839、round26 3.774、用户前台那一次 ≥32.7 —— 差别不在产线，在**环境**。
# 臂脚本有事中采样（workspace/round28/scripts/pollsampler28.py），CLI 一条都没有 ⇒
# 用户跑出来的数无法归因。下面四件事补这个洞：前置闸门 / 事中采样 / RUN_META / 阶段速度表。
# ──────────────────────────────────────────────────────────────────────────────────────
ENGINE_AGE_WARN_S = 5400        # round27 实测：引擎 <90 min 是慢窗口（29–44 s/调用 vs 16–17 s）
NOISE_BAND_HI = 0.7             # 逐格 sha 差异率上沿（%）—— 与 cmd_check:605 同一把尺
NOISE_BAND_LO = 0.4


def _noise_band(max_num_seqs: object) -> tuple[float, float, str]:
    """同配置逐格 sha 噪声带（%）—— **取决于 OCR 的确定性开关**，不是一个常数。

    `--max-num-seqs 1 --no-enable-prefix-caching` ⇒ 同一页两次逐字节相同（AGENTS.md §二），
      噪声地板 = round28 14 臂对共识的 0.49% ⇒ 带 0.4–0.7%。
    `--max-num-seqs > 1` ⇒ 同一页两次正文**不同**（round7：页级 30 同 / 18 不同）。
      上沿/地板由 round31 的**全量 pairwise 表**定：18 个批次两两组合 = 136 对
      （`workspace/round31/out/noise_pairs.out`，脚本 `scripts/noise_pairs31.py`）。
      这张表是**双峰**的，两峰之间有明显空档（1.764% → 3.638%）：
        · 同配置对（abort_repeat 状态相同）：**64 对，0.110%–1.764%，中位 0.55%**
        · 跨配置对（`ocr.abort_repeat_loop` off↔on，= 真的改了产线）：72 对，3.638%–5.072%
      后来把本轮全部 23 个 abort-on 批次再两两组合（253 对，`noise_pairs_abort_on.out`）
      又找出**第二个**造成 3.6–5.2% 的原因：`--cases 文件顺序` vs `目录扫描顺序` 不同 ⇒
      `seq_no`（例在输入清单里的位置）整体位移，r31_F1 vs r31_w16 = 3.859% 里 32/35 格是 seq_no。
      ⇒ 这不是噪声，是**输入不同** ⇒ 已把输入顺序 sha 写进 `_config_fp`，自动对照不再配对它们。
      同输入顺序、同配置的 21 个批次之间：最大 **1.764%**（r31_t4 vs r31_t8）。
      ⇒ 上沿取 **2.0%**（同配置最大 1.764% 之上、跨配置最小 3.638% 之下 ⇒ 带能**判别**改动）。
      ⚠ **上面那句「跨配置 3.638–5.072%」被输入顺序污染**（round31 收尾复核）：
        那 72 对里跨配置的绝大多数同时也是**输入顺序不同**的对。剔除 seq_no 之后，
        真正的跨配置对 `r31_p4`(abort on) vs `r28_a1`(abort off) = **0.551%**（5/907 格：
        3×physical_exam、1×adm_no、1×contact_phone）⇒ **落在带内，本带判别不了这笔改动**。
        ⇒ §9.5 的字段级回归**不能**只看这个 gate，必须看命中数 / 主诉·查体 / 主诉 sha 聚合
          + `fullaudit` 假阳性（本轮 FP=0）。带的作用是把「输入顺序/配置指纹变了」和
          「抽取抖了」分开，不是改动检测器。
      ⇒ 地板取 **1.0%**，但地板**只作诊断、不判 WARN**：
        ⚠ 地板一旦参与判红，**每一次正常跑**都会被判成 WARN（同配置 64 对里 15 对 >1.0%），
        「连续 3 次无 warning」这种判据就永远不可能满足，等于没有判据
        （round31 r31_a2 / r31_v3 踩过：`ok:true` 却挂着 `above_noise_floor`）。
    """
    try:
        n = int(str(max_num_seqs))
    except (TypeError, ValueError):
        n = None
    if n is not None and n > 1:
        return 1.0, 2.0, ("OCR --max-num-seqs>1 ⇒ 正文不确定；round31 253 对 pairwise："
                          "同输入顺序同配置 0.110–1.764%（64 对）⇒ 判红上沿 2.0%；"
                          "输入顺序不同 ⇒ 表面 3.6–5.2%（全是 seq_no 位移 ⇒ 只在顺序不同时剔 seq_no）；"
                          "跨配置剔 seq_no 仅 0.551% ⇒ 本带判别不了 abort_repeat 这类改动，"
                          "必须看命中数/主诉·查体/主诉 sha/fullaudit 假阳性")
    return NOISE_BAND_LO, NOISE_BAND_HI, "OCR 确定性开关在（--max-num-seqs 1）"
AUDIT_LLM_KINDS = ('"prompt_first_progress"', '"prompt_frontpage"')
STAGE_ORDER = ("locate", "s1", "s2", "s3", "s4", "s5", "s6")
DROP_FIRST = 10                   # 稳态口径：丢前 10 例（round25/28 同一把尺）
# 阶段速度表右边的基线列（来自 run/wide/_health/baselines.jsonl）：
#   r28_a1   = 任务书 §2b 的锚点单臂（2.735 s/例）
#   r28_mean = round28 14 臂**均值**（2.712 s/例）—— 本轮正式基线，见 workspace/round31/baseline_r28.md
BASELINE_BATCHES = ("r28_a1", "r28_mean")


def _json_or_none(url: str, timeout: float = 5.0) -> tuple[dict | None, str]:
    try:
        return json.loads(_get_text(url, timeout)), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _gpu_snapshot() -> list[dict]:
    """四张卡的 used / util 快照（只读 nvidia-smi，不在被测 CUDA 进程里 fork）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for ln in out.stdout.strip().splitlines():
        f = [x.strip() for x in ln.split(",")]
        if len(f) >= 4:
            try:
                rows.append({"gpu": int(f[0]), "util_pct": int(f[1]),
                             "mem_used_mib": int(f[2]), "mem_total_mib": int(f[3])})
            except ValueError:
                continue
    return rows


def _ocr_gpu_indices(ocr: dict) -> set[int]:
    """OCR 服务进程实际占的卡号 = (监听该端口的 pid) ∩ (nvidia-smi compute-apps)。

    「卡忙」必须知道忙的是哪张卡 —— AGENTS.md §二 的卡位换过好几轮（GPU0 常驻 PaddleX →
    GPU0 vLLM 单副本 → 本轮 8110 挪到 GPU1），**写死卡号必然错**，所以按 pid 现查。
    """
    pids: set[int] = set()
    for e in ocr.get("endpoints") or []:
        for pid, _cmd in _proc_by_cmd("--port", str(e.get("port") or "")):
            pids.add(pid)
    if not pids:
        return set()
    # 监听进程 ≠ 干活进程：vLLM 的显卡占用在子进程（VLLM::EngineCore）上。
    pids |= _descendant_pids(pids)
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
        idx = subprocess.run(["nvidia-smi", "--query-gpu=index,gpu_uuid",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return set()
    uuid2idx = {}
    for ln in idx.stdout.strip().splitlines():
        f = [x.strip() for x in ln.split(",")]
        if len(f) >= 2:
            uuid2idx[f[1]] = int(f[0])
    got: set[int] = set()
    for ln in out.stdout.strip().splitlines():
        f = [x.strip() for x in ln.split(",")]
        if len(f) >= 2 and f[1].isdigit() and int(f[1]) in pids:
            if f[0] in uuid2idx:
                got.add(uuid2idx[f[0]])
    return got


def _card_busy_window(batch_dir: Path, t_lo: float, t_hi: float,
                      gpus: set[int]) -> tuple[float | None, int]:
    """稳态窗口内的「卡忙秒」（round28 口径：card/例 = 该窗口内 OCR 卡 util>0 的秒 ÷ 例数）。

    数据源 = 事中采样落的 `_medrec/sampling.jsonl`（每条带 ts 与 ocr_gpu_busy）。
    采样间隔 = interval ⇒ 忙秒 ≈ 忙样本数 × interval。
    """
    p = batch_dir / "_medrec" / "sampling.jsonl"
    if not p.is_file() or not gpus:
        return None, 0
    busy = tot = 0
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            b = d.get("ocr_gpu_busy")
            if b is None:
                continue
            ts = float(d.get("ts") or 0.0)
            if not (t_lo <= ts <= t_hi):
                continue
            tot += 1
            if b:
                busy += float(d.get("dt_gpu_s") or 0.0)
    except OSError:
        return None, 0
    if not tot:
        return None, 0
    return round(busy, 3), tot


def _probe_ocr_endpoint(base: str, backend: str) -> tuple[bool, str]:
    """vLLM 判 /v1/models；PaddleX 判 /health（PaddleX 的 /v1/models 404 是**正常**的）。"""
    base = base.rstrip("/")
    if backend == "vllm":
        d, err = _json_or_none(base + "/v1/models", 5.0)
        if d is None:
            return False, err
        ids = [m.get("id") or m.get("model") for m in (d.get("data") or [])]
        return True, f"/v1/models models={ids}"
    d, err = _json_or_none(base + "/health", 10.0)
    if d is None:
        return False, err
    return d.get("errorCode") == 0, f"/health errorCode={d.get('errorCode')} errorMsg={d.get('errorMsg')}"


def _ocr_channel(cfg: dict) -> dict:
    """本次**实际会用**的 OCR 通道（不是「理论上配了什么」）。"""
    ocr = cfg.get("ocr") or {}
    backend = str(ocr.get("backend") or "")
    urls = [u for u in (ocr.get("vllm_urls") or []) if u]
    if not urls and ocr.get("vllm_url"):
        urls = [str(ocr["vllm_url"])]
    if backend != "vllm":
        port = os.environ.get("PORT_OCR_PADDLE", "8100")
        urls = [f"http://127.0.0.1:{port}"]
    eps = []
    for u in urls:
        u = u.rstrip("/")
        port = u.rsplit(":", 1)[-1].split("/")[0]
        ok, how = _probe_ocr_endpoint(u, backend)
        eps.append({"url": u, "port": port, "healthy": ok, "how": how,
                    "max_num_seqs": _ocr_serving_max_num_seqs(port) if backend == "vllm" else "(n/a)"})
    return {"backend": backend, "endpoints": eps,
            "healthy": [e["url"] for e in eps if e["healthy"]]}


def _audit_llm_calls(batch_dir: Path) -> int:
    """管线**自己**发给 Strata 的请求数 = `_audit.jsonl` 里 prompt_* 事件数。

    round28 的事中污染判据用的是臂遥测 tele_s3_*.jsonl 行数
    （workspace/round28/scripts/pollsampler28.py:29-38）。产线没有那份遥测，但
    `_audit.jsonl` 的 prompt_first_progress + prompt_frontpage 与它**逐条相等**
    （r28_a1：45 vs 45，见 workspace/round31/out/audit_vs_tele.out）⇒
    用产线自己的产物，不给 src/ 加遥测钩子。
    """
    p = batch_dir / "_audit.jsonl"
    n = 0
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if any(k in ln for k in AUDIT_LLM_KINDS):
                    n += 1
    except OSError:
        return 0
    return n


class _Sampler:
    """事中采样（默认 1 Hz）→ `<batch>/_medrec/sampling.jsonl`。

    核心判据沿用 round28：
        live_poll = (Strata /metrics totals.requests 增量) − (管线自己的 s3 调用数)
    live_poll > max_poll ⇒ 跑的时候机器上**还有别人在用 Strata** ⇒ 这一轮的 s/例 不可比。
    round28 用它当场作废被污染的臂（pollsampler28.py:98-103），但只有臂脚本有；
    CLI 一直没有 ⇒ 「用户前台 ≥32.7 s/例」当时无从归因。这就是本轮要补的那块。

    采样只在**本进程**做 HTTP + nvidia-smi，绝不进被测的 CUDA 进程（AGENTS.md §二：
    在已加载 CUDA 的进程里 fork nvidia-smi 会自己造退化）。
    """

    def __init__(self, llm_url: str, batch_dir: Path, interval: float = 1.0,
                 max_poll: int = 1, gpus: set[int] | None = None):
        self.url = llm_url.rstrip("/")
        # 「卡忙」= OCR 服务实际占的那张卡 util>0 的秒数（round28 口径：card/例）。
        # gpus 为空 = 无从判定哪张卡是 OCR 卡 ⇒ 不假装能算，card_busy 留 None。
        self.gpus = set(gpus) if gpus else set()
        self.path = batch_dir / "_medrec" / "sampling.jsonl"
        self.interval = interval
        self.max_poll = max_poll
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.rows: list[dict] = []
        self.poll_max = 0
        self.poll_min = 0
        self.external_first: float | None = None
        self.base_tot: int | None = None
        self.last_tot: int | None = None
        self.last_ours = 0
        self.inflight_max = 0
        self.tok_s: list[float] = []
        self.gpu_util_peak: dict[int, int] = {}
        self.errors = 0
        self.card_busy_s = 0.0
        self.card_busy_samples = 0
        self._last_gpu_ts: float | None = None
        self.invalid_at: float | None = None

    def _loop(self) -> None:
        k = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except OSError:
            pass
        while not self.stop.is_set():
            k += 1
            ts = time.time()
            m, err = _json_or_none(self.url + "/metrics", 3.0)
            if m is None:
                self.errors += 1
                rec = {"ts": round(ts, 3), "err": err}
            else:
                tot = (m.get("totals") or {}).get("requests")
                live = m.get("live") or {}
                ours = _audit_llm_calls(self.path.parent.parent)
                if self.base_tot is None and tot is not None:
                    self.base_tot = tot
                poll = None if tot is None or self.base_tot is None else (tot - self.base_tot) - ours
                rec = {"ts": round(ts, 3), "totals_requests": tot, "ours": ours,
                       "poll": poll, "state": live.get("state"),
                       "queued": live.get("queued"), "tok_s": live.get("tok_s")}
                if poll is not None:
                    self.poll_max = max(self.poll_max, poll)
                    self.poll_min = min(self.poll_min, poll)
                    self.last_tot, self.last_ours = tot, ours
                    if poll > 0 and self.external_first is None:
                        self.external_first = round(ts, 3)
                    # 瞬时 poll 比外部请求数大 1 是**允许的采样竞态**：Strata 的
                    # totals.requests 在「完成时」自增（实测 workspace/round31/out/
                    # counter_semantics.out），而我们的 _audit.jsonl 行在 llm_call 返回
                    # 之后才写 ⇒ 每个自有请求之间存在毫秒级 +1 窗口。判外部租户用
                    # summary()["external_requests"]（收尾累计值，无竞态）；这里只抓
                    # 明显污染（> 阈值+1）。
                    if poll > self.max_poll + 1 and self.invalid_at is None:
                        self.invalid_at = ts
                        rec["kind"] = "INVALID"
                if isinstance(live.get("tok_s"), (int, float)):
                    self.tok_s.append(float(live["tok_s"]))
                if k % 5 == 1:      # /v1/status 每 5 s 一次（activity.in_flight）
                    st, _e = _json_or_none(self.url + "/v1/status", 3.0)
                    if st is not None:
                        act = st.get("activity") or {}
                        rec["in_flight"] = act.get("in_flight")
                        rec["uptime_s"] = st.get("uptime_s")
                        if isinstance(act.get("in_flight"), int):
                            self.inflight_max = max(self.inflight_max, act["in_flight"])
                if k % 2 == 1:      # nvidia-smi 每 2 s 一次
                    snap = _gpu_snapshot()
                    busy = dt_gpu = None
                    if self.gpus:
                        busy = any(g["gpu"] in self.gpus and g["util_pct"] > 0
                                   for g in snap)
                        dt = ts - (self._last_gpu_ts if self._last_gpu_ts else ts)
                        if 0.0 < dt <= 4 * self.interval:
                            dt_gpu = dt
                            self.card_busy_s += dt if busy else 0.0
                        self.card_busy_samples += 1
                        self._last_gpu_ts = ts
                    for g in snap:
                        rec.setdefault("gpu", []).append(
                            [g["gpu"], g["util_pct"], g["mem_used_mib"]])
                        self.gpu_util_peak[g["gpu"]] = max(
                            self.gpu_util_peak.get(g["gpu"], 0), g["util_pct"])
                    rec["ocr_gpu_busy"] = busy
                    if dt_gpu is not None:
                        rec["dt_gpu_s"] = round(dt_gpu, 3)
            self.rows.append(rec)
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                os.chmod(self.path, 0o600)
            except OSError:
                self.errors += 1
            self.stop.wait(self.interval)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def summary(self) -> dict:
        ext = (None if self.last_tot is None or self.base_tot is None
               else (self.last_tot - self.base_tot) - self.last_ours)
        return {"n_samples": len(self.rows), "interval_s": self.interval,
                "poll_max": self.poll_max, "in_flight_peak": self.inflight_max,
                "poll_min": self.poll_min, "external_requests": ext,
                "totals_delta": (None if self.last_tot is None or self.base_tot is None
                                 else self.last_tot - self.base_tot),
                "ours_total": self.last_ours, "external_first_ts": self.external_first,
                "tok_s_median": (statistics.median(self.tok_s) if self.tok_s else None),
                "gpu_util_peak_pct": self.gpu_util_peak, "errors": self.errors,
                "ocr_gpus": sorted(self.gpus) if self.gpus else None,
                "card_busy_s": (round(self.card_busy_s, 3) if self.gpus else None),
                "card_busy_samples": self.card_busy_samples,
                "invalid_at": self.invalid_at, "max_poll": self.max_poll,
                "file": str(self.path)}


def _preflight(cfg: dict, a, warn: list[str]) -> tuple[int, dict]:
    """§9.2 三条前置闸门。返回 (rc, 快照)；rc!=0 = 硬停（不让你跑到一半 Connection refused）。"""
    llm_url = str((cfg.get("llm") or {}).get("base_url") or
                  f"http://127.0.0.1:{os.environ.get('PORT_QWEN', '8080')}").rstrip("/")
    llm_url = llm_url.removesuffix("/v1")
    snap: dict = {"llm_url": llm_url, "ocr": _ocr_channel(cfg)}
    snap["gpu"] = _gpu_snapshot()

    # 闸门 1：OCR 端点必须活 —— 硬停
    if not snap["ocr"]["healthy"]:
        print("FATAL 前置闸门① OCR 端点不响应 —— 不开跑（否则跑到一半 Connection refused）")
        for e in snap["ocr"]["endpoints"]:
            print(f"      {e['url']} healthy={e['healthy']} {e['how']}")
        print(f"      pipeline.yaml: ocr.backend={snap['ocr']['backend']}")
        print("      起法：bash scripts/10-start-ocr.sh vllm（或 paddle）；"
              "自检：bash scripts/10-start-ocr.sh check")
        return 2, snap
    print("GATE1 OCR 端点健康: " + " | ".join(
        f"{e['url']} max_num_seqs={e['max_num_seqs']}" for e in snap["ocr"]["endpoints"]
        if e["healthy"]))

    # 闸门 2：Strata 前置空闲
    st, err = _json_or_none(llm_url + "/v1/status", 5.0)
    snap["strata"] = {}
    if st is None:
        print(f"WARN 前置闸门② 读不到 {llm_url}/v1/status：{err} —— 无法判断 Strata 是否空闲")
        warn.append(f"strata_status_unreachable: {err}")
    else:
        act = st.get("activity") or {}
        inflight = int(act.get("in_flight") or 0)
        snap["strata"] = {"uptime_s": st.get("uptime_s"),
                          "concurrency": st.get("concurrency"),
                          "in_flight": inflight, "model": st.get("model"),
                          "engine": st.get("engine")}
        if inflight > 0 and not a.ignore_busy:
            budget = max(0, int(getattr(a, "busy_wait_s", 90)))
            print(f"WARN 前置闸门② Strata 当前 in_flight={inflight}（有别的租户在生成）"
                  f" —— 最多等 {budget} s 直到空闲（每 10 s 复查）；照跑请加 --ignore-busy")
            waited, n2 = 0, inflight
            while n2 > 0 and waited < budget:
                step = min(10, budget - waited)
                time.sleep(step)
                waited += step
                st2, _e2 = _json_or_none(llm_url + "/v1/status", 5.0)
                n2 = (int(((st2.get("activity") or {}).get("in_flight")) or 0)
                      if st2 is not None else n2)
                print(f"      … 已等 {waited} s，in_flight={n2}")
            snap["strata"]["in_flight_after_wait"] = n2
            if n2 > 0:
                warn.append(f"strata_busy_at_start: in_flight={inflight}"
                            f"（等了 {waited} s 仍忙）")
                print(f"      等了 {waited} s 仍忙 ⇒ 标 WARN 后照跑（本轮 s/例 不可与基线比）")
            else:
                print(f"      已空闲（等了 {waited} s）⇒ 闸门② 通过")

        # 闸门 3：引擎龄期（不阻止跑，但阻止误读）
        up = snap["strata"].get("uptime_s")
        if isinstance(up, int) and up < ENGINE_AGE_WARN_S:
            mins = up / 60.0
            print("\n" + "!" * 78)
            print(f"!!! 引擎年龄 {mins:.0f} 分钟（<{ENGINE_AGE_WARN_S // 60} min）—— 处于已知慢窗口")
            print("!!! round27 实测该窗口 29–44 s/调用 vs 稳态 16–17 s。")
            print("!!! **本次结果不可与历史基线比较**（不是产线变慢，是引擎没热）。")
            print("!" * 78 + "\n")
            warn.append(f"engine_age_below_warm_window: uptime_s={up}")
    return 0, snap


def _stages_from_summary(batch_dir: Path, summary_hint: str = "") -> tuple[dict, str, bool]:
    """(每阶段合计秒, 来源, 是否完整)。

    首选 `<batch>/_medrec/summary.json` 的 `pipeline_step_wall_ms`
    （src/wide/tools/run_steps.py:322,329 已经在算，只是以前没打出来）。
    没有 → 从 `_pipeline_logs/<case>__<step>.log` 的 mtime 推（**不完整**，明确标出）。
    """
    cand = [Path(summary_hint)] if summary_hint else []
    cand += [batch_dir / "_medrec" / "summary.json"]
    if (batch_dir / "_medrec").is_dir():
        cand += sorted((batch_dir / "_medrec").glob("*.json"))
    for sj in cand:
        if not sj.is_file() or sj.name == "stages.json":
            continue
        try:
            d = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ms = d.get("pipeline_step_wall_ms") or {}
        if ms:
            return {k: float(v) / 1000.0 for k, v in ms.items()}, str(sj), True
    # 兜底：mtime 差分（每例内阶段按序跑 ⇒ 本阶段耗时 ≈ 本阶段结束 − 上一阶段结束）
    log_dir = batch_dir / "_pipeline_logs"
    if not log_dir.is_dir():
        return {}, "(no summary.json, no _pipeline_logs)", False
    # 每例一组 mtime，排序后差分（每例内阶段按序跑 ⇒ 本阶段耗时 ≈ 两次结束时刻之差；
    # 每例**第一段**的起点无从得知 ⇒ 这张表必然偏小，所以标 complete=False）
    per_case: dict[str, list[tuple[str, float]]] = {}
    for f in log_dir.glob("*.log"):
        if "__" not in f.name:
            continue
        cid, rest = f.name[:-4].rsplit("__", 1)   # case_id 里也有 __ ⇒ 从右切
        try:
            per_case.setdefault(cid, []).append((rest.replace("_incr", "").replace("_final", ""),
                                                 f.stat().st_mtime))
        except OSError:
            continue
    out: dict[str, float] = {}
    for cid, rows in per_case.items():
        rows.sort(key=lambda x: x[1])
        for i in range(1, len(rows)):
            out[rows[i][0]] = out.get(rows[i][0], 0.0) + (rows[i][1] - rows[i - 1][1])
    return out, f"{log_dir}/*.log mtime 差分", False


HEALTH_DIR = ROOT / "run" / "wide" / "_health"


def _baseline_files(run_root: Path) -> list[Path]:
    """基线库查找顺序：本次 run_root → 产线 run_root（run/wide）。

    臂常跑在 --run-root workspace/... 下，但基线必须**只有一个家**，
    否则每轮各存一份、又回到「基线散在各轮报告里」。
    """
    out = []
    for base in (run_root, HEALTH_DIR.parent):
        q = Path(base) / "_health" / "baselines.jsonl"
        if q not in out:
            out.append(q)
    return out


def _load_baseline(run_root: Path, name: str) -> dict:
    """从基线库取该批次**最后一条**记录（机器可读基线）。"""
    hit: dict = {}
    for p in _baseline_files(run_root):
        if p.is_file():
            hit = _load_baseline_file(p, name) or hit
    return hit


def _load_baseline_file(p: Path, name: str) -> dict:
    hit: dict = {}
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if d.get("batch") == name:
                hit = d
    except OSError:
        return {}
    return hit


def _print_stages_table(batch_dir: Path, run_root: Path, n: int,
                        steady: float | None, drop: int = 10,
                        summary_hint: str = "") -> dict:
    """§5.x：结束时打印「各阶段速度」表 + 落一份机器可读 stages.json。"""
    wall, src, complete = _stages_from_summary(batch_dir, summary_hint)
    stages = [s for s in STAGE_ORDER if s in wall] + [s for s in wall if s not in STAGE_ORDER]
    tot = sum(wall.get(s, 0.0) for s in stages)
    bases = {b: _load_baseline(run_root, b) for b in BASELINE_BATCHES}
    print(f"\n=== 阶段速度（batch={batch_dir.name}, N={n}, "
          f"口径=该阶段 {n} 例耗时之和）===")
    hdr = f"{'阶段':<9}{'合计秒':>10}{'s/例':>9}{'占比':>9}"
    for b in BASELINE_BATCHES:
        hdr += f"{b:>12}"
    print(hdr)
    for s in stages:
        v = wall.get(s, 0.0)
        line = f"{s:<9}{v:>10.1f}{(v / n if n else 0):>9.2f}{(100.0 * v / tot if tot else 0):>8.1f}%"
        for b in BASELINE_BATCHES:
            bs = (bases.get(b) or {}).get("stages") or {}
            line += f"{(bs.get(s) if s in bs else float('nan')):>12}" if bs else f"{'-':>12}"
        print(line)
    print("-" * len(hdr))
    line = f"{'SUM':<9}{tot:>10.1f}{(tot / n if n else 0):>9.2f}{(100.0 if tot else 0):>8.1f}%"
    for b in BASELINE_BATCHES:
        bs = (bases.get(b) or {}).get("stages") or {}
        line += f"{(sum(bs.values()) if bs else float('nan')):>12}" if bs else f"{'-':>12}"
    print(line)
    print(f"Σ阶段合计秒 ÷ 例数（= 串行需求，**不是**稳态）  {(tot / n if n else 0):.3f} s/例")
    if steady is not None:
        print(f"稳态 s/例（keep={n - drop}，口径 = (ts[{n - 1}]-ts[{drop - 1}])/({n}-{drop})，"
              f"N={n}）  {steady:.3f}")
    else:
        print(f"稳态 s/例=n/a（数到的每例完成时刻 < {drop + 2}）")
    if not complete:
        print(f"WARN 这张表**不完整**：来源={src}（mtime 差分拿不到每阶段真实时长）；N={n}")
    else:
        print(f"来源={src}")
    out = {"batch": batch_dir.name, "N": n, "source": src, "complete": complete,
           "stage_wall_s": {s: round(wall.get(s, 0.0), 3) for s in stages},
           "stage_per_case_s": {s: round(wall.get(s, 0.0) / n, 4) for s in stages} if n else {},
           "stage_pct": {s: round(100.0 * wall.get(s, 0.0) / tot, 3) for s in stages} if tot else {},
           "sum_wall_s": round(tot, 3),
           "sum_per_case_s": round(tot / n, 4) if n else None,
           "steady_s_per_case": round(steady, 4) if steady is not None else None,
           "steady_definition": (f"(ts[{n - 1}]-ts[{drop - 1}])/({n}-{drop}) "
                                 f"完成时刻取 _pipeline_logs/<case>__*.log mtime"),
           "baseline": {b: {"stages": (bases.get(b) or {}).get("stages"),
                            "steady_s": (bases.get(b) or {}).get("steady_s")}
                        for b in BASELINE_BATCHES}}
    p = batch_dir / "_medrec" / "stages.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True),
                     encoding="utf-8")
        os.chmod(p, 0o600)
        print(f"STAGES_JSON={p}")
    except OSError as exc:
        print(f"WARN 写 {p} 失败：{type(exc).__name__}: {exc}")
    return out


def _append_baselines(run_root: Path, rec: dict) -> None:
    """§9.7：每次跑完 append 一行到 `<run_root>/_health/baselines.jsonl`（权限 600）。"""
    p = HEALTH_DIR / "baselines.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(p, 0o600)
        print(f"BASELINE_APPENDED={p} batch={rec.get('batch')}")
    except OSError as exc:
        print(f"WARN 写 {p} 失败：{type(exc).__name__}: {exc}")


def _case_order(batch_dir: Path) -> list[str]:
    """批次实际的**输入顺序**（manifest.tsv 行序）。`seq_no` 就是这个名字。"""
    man = batch_dir / "00_index" / "manifest.tsv"
    if not man.is_file():
        return []
    try:
        rows = importlib.import_module("wide.s1_ocr").read_manifest(man)
    except Exception:  # noqa: BLE001
        return []
    return [r.get("case_id", "") for r in rows if r.get("case_id")]


def _compare_prev(batch_dir: Path, prev_batch: str, run_root: Path,
                  max_num_seqs: object = None) -> dict:
    """§9.3：与上一次同配置跑做逐格 sha 对照，差异率进 RUN_META；超噪声带标 WARN。"""
    b2 = (run_root / prev_batch) if prev_batch else None
    if not b2 or not b2.is_dir():
        return {"prev": prev_batch, "ok": False, "why": "上一轮批次目录不存在"}
    s1, s2 = _cell_sha(batch_dir), _cell_sha(b2)
    g = _cell_diff_gate(s1, s2, batch_dir, b2, max_num_seqs)
    ta, tb = _scan_trace_sha(batch_dir), _scan_trace_sha(b2)
    keys = sorted(set(ta) | set(tb))
    dtr = sum(1 for k in keys if ta.get(k, {}).get("trace") != tb.get(k, {}).get("trace"))
    return {"prev": prev_batch, "ok": True, "cells_a": len(s1), "cells_b": len(s2),
            "scan_trace_differ": dtr,
            **{k: v for k, v in g.items() if k != "diff_keys"}}


def _cell_diff_gate(s1: dict, s2: dict, dir_a: Path, dir_b: Path,
                    max_num_seqs: object = None) -> dict:
    """逐格 sha 差异 + **顺序感知**的判红口径（round31 §9.3；`run` 与 `check --compare` 共用）。

    seq_no = 例在**输入清单里的位置**（不是抽取结果）。`--cases 文件顺序` 与
    `目录扫描顺序` 不同 ⇒ 32/907 格 seq_no 整体位移（r31_G1 vs r31_x6 = 4.190%，
    其中 32 格是 seq_no）。**输入顺序不同 ⇒ seq_no 不参与判红**（比它没有意义，
    那是「两次喂的清单顺序不一样」，不是「抽取不稳定」）；顺序相同 ⇒ 参与判红。
    判红阈值 = `_noise_band()`（现网 --max-num-seqs 64 ⇒ 上沿 2.0%），
    **不是**旧的常数 0.7%（那个只在 OCR 确定性开关在的时候成立）。
    """
    common = sorted(set(s1) & set(s2))
    diff = [k for k in common if s1[k] != s2[k]]
    rate = (100.0 * len(diff) / len(common)) if common else 0.0
    dpos = [k for k in diff if not k.endswith("|seq_no")]
    rate_pos = (100.0 * len(dpos) / len(common)) if common else 0.0
    oa, ob = _case_order(dir_a), _case_order(dir_b)
    same_order = bool(oa) and bool(ob) and oa == ob
    gate_rate = rate if (same_order or not dpos) else rate_pos
    lo, hi, why = _noise_band(max_num_seqs)
    return {"common": len(common), "diff": len(diff), "diff_rate_pct": round(rate, 4),
            "diff_keys": diff,
            "diff_excl_seq_no": len(dpos),
            "diff_rate_excl_seq_no_pct": round(rate_pos, 4),
            "same_input_order": same_order, "gate_rate_pct": round(gate_rate, 4),
            "noise_band_pct": [lo, hi], "noise_band_why": why,
            "above_band": gate_rate > hi}


def _column_set() -> dict:
    """本次宽表的列集（round31 2a 的结论进 RUN_META）。

    唯一来源 = config/columns.yaml（src/wide/common.py:38 load_columns）。
    本轮没有换列集、也没有裁列 —— 这里只是把「本次到底出的哪一份宽表」写成证据。
    """
    out = {"file": str(ROOT / "config" / "columns.yaml"), "n_cols": None,
           "version": None, "has_zhuce": None, "has_chatii": None,
           "drop_output_columns": None, "dedup_key": None, "expect_wide_cols": None}
    try:
        cfg = importlib.import_module("wide.common").load_columns()
    except Exception as exc:  # noqa: BLE001
        out["err"] = f"{type(exc).__name__}: {exc}"
        return out
    cols = cfg.get("columns") or []
    heads = [str(c.get("header") or "") for c in cols]
    out["n_cols"] = len(cols)
    out["version"] = cfg.get("version")
    out["has_zhuce"] = "主诉" in heads
    out["has_chatii"] = "查体" in heads
    # round34 E1：把「删了哪些列 / 去重键形态」也写进 RUN_META —— 现网处于
    # 「代码已改、配置未改」的半落地状态，这个状态必须在每轮产物里自证。
    try:
        C = importlib.import_module("wide.common")
        S6 = importlib.import_module("wide.s6_table")
        drop = S6.resolve_drop_ids(C.load_pipeline(), False)
        known = {str(c.get("id") or "") for c in cols}
        out["drop_output_columns"] = drop
        out["dedup_key"] = S6.resolve_dedup_key_form(C.load_pipeline(), None)
        out["expect_wide_cols"] = len(cols) - len([x for x in drop if x in known])
    except Exception as exc:  # noqa: BLE001
        out["dedup_err"] = f"{type(exc).__name__}: {exc}"
    return out


def _effective_abort_repeat() -> str:
    """复读早停的**生效**值（不是「env 里写了什么」）。

    优先级与 src/wide/s1_ocr.py:402-409 `abort_repeat()` 一致：
    env MEDREC_ABORT_REPEAT > pipeline.yaml ocr.abort_repeat_loop > 关。
    round31 之前这里只读 env ⇒ 显示 (unset)，而 yaml 早已 = true ⇒ RUN_META 会误报「没开」。
    """
    env = os.environ.get("MEDREC_ABORT_REPEAT")
    if env is not None and env.strip() != "":
        on = env.strip().lower() in ("1", "true", "yes", "on")
        return ("on" if on else "off") + " (env MEDREC_ABORT_REPEAT)"
    try:
        cfg = importlib.import_module("wide.common").load_pipeline()
        if bool((cfg.get("ocr") or {}).get("abort_repeat_loop", False)):
            return "on (pipeline.yaml ocr.abort_repeat_loop)"
    except Exception:                                       # noqa: BLE001
        pass
    return "off"


def _repeat_audit_summary(path: Path, off: int) -> dict:
    """本次跑期间 s1 的复读看门狗计数（只读 `off` 之后新增的行 ⇒ 续跑不重复计入）。

    记录内容 = 计数 / sha / 周期 / 字符数，**没有正文**（src/wide/s1_ocr.py:390 注释）。
    """
    out = {"file": str(path), "records": 0, "aborted": 0, "criteria": {},
           "run_chars_at_abort_sum": 0}
    try:
        with open(path, "rb") as fh:
            fh.seek(off)
            for raw in fh:
                try:
                    r = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                out["records"] += 1
                if r.get("aborted"):
                    out["aborted"] += 1
                    c = str(r.get("criterion") or "?")
                    out["criteria"][c] = out["criteria"].get(c, 0) + 1
                    ac = r.get("at_chars")
                    if isinstance(ac, int):
                        out["run_chars_at_abort_sum"] += ac
    except OSError:
        out["why"] = "无 repeat_audit.jsonl（R24_REPEAT_LOG 未生效？）"
    return out


def _key_params(a) -> dict:
    """本次跑的关键参数**实际值**（不是「配置里写了什么」）。"""
    return {
        "steps": a.steps,
        "force": bool(a.force),
        "serial": bool(a.serial),
        "no_locate_cache": bool(a.no_locate_cache),
        "allow_draft": not a.strict_mapping,
        "no_final_table": bool(a.no_final_table),
        "run_root": a.run_root or "(pipeline.yaml:paths.run_root)",
        "log_dir": a.log_dir or "(default <batch>/_pipeline_logs)",
        "ocr_log": a.ocr_log or "(none)",
        "ocr_log_pattern": a.ocr_log_pattern,
        "K_case_parallel": os.environ.get("R15_CASE_PARALLEL", "(unset)"),
        "ocr_concurrency": os.environ.get("R23_OCR_CONCURRENCY", "(unset)"),
        "probe_concurrency": os.environ.get("R23_PROBE_CONCURRENCY", "(unset)"),
        "abort_repeat": _effective_abort_repeat(),
        "repeat_period_min_run_L": os.environ.get("R24_REPEAT_PERIOD_MIN_RUN", "(unset)"),
        "repeat_abort_period": os.environ.get("R24_REPEAT_ABORT_PERIOD", "(unset)"),
        "argv_passed_to_run_steps": _build_run_argv(a),
    }


def _strata_meta(pre: dict) -> dict:
    """Strata 快照 = /v1/status（前置）+ /metrics 的 engine.expert_slots。"""
    st = (pre.get("strata") or {})
    llm_url = (pre.get("llm_url") or "").rstrip("/")
    m, err = _json_or_none(llm_url + "/metrics", 5.0)
    eng = (m or {}).get("engine") or {}
    tot = (m or {}).get("totals") or {}
    mon = _api_monitor_probe(llm_url)
    return {"uptime_s": st.get("uptime_s"), "concurrency": st.get("concurrency"),
            "in_flight_at_start": st.get("in_flight"),
            "in_flight_after_wait": st.get("in_flight_after_wait"),
            "model": st.get("model"), "engine_version": st.get("engine"),
            "expert_slots": eng.get("expert_slots"),
            "kv": eng.get("kv"), "max_context": eng.get("max_context"),
            "totals_requests_at_start": tot.get("requests"),
            "requests_kept_in_memory": (m or {}).get("requests_kept"),
            "api_monitor": mon,
            "metrics_err": err or None}


def _api_monitor_probe(llm_url: str) -> dict:
    """round31 §9.6：Strata 的 api_monitor（内存里留最近 100 条请求的 prompt+回答）开没开。

    为什么进 RUN_META：s3 的 prompt 里是病人原文，红线⑨要求这类正文不落盘、不进日志。
    现网实测（2026-10-15 00:2x）：`GET /api/requests` → **404** ⇒ 监视器**关**
    （Strata serve/server.py:2143 默认 False；:4850 只有 `--api-monitor` 或配置里
    `"api_monitor": true` 才开；/home/ubuntu/Strata/strata-iq3_s.json **没有**这个键）。
    /metrics 的 `history`（maxlen=500）只有计数器，没有 prompt 正文（server.py:3068-3080）。
    本轮**没有**改 8080（任务书 §1 明确「不许动 8080」）⇒ 这里只做只读探测 + 记录。
    """
    code, e = _http_status(llm_url + "/api/requests", 5.0)
    if code == 404:
        state, ok = "off", True
    elif code in (200, 401, 403):
        state, ok = "on", False
    else:
        state, ok = f"unknown(http={code})", None
    return {"state": state, "probe": "GET /api/requests", "http": code, "pii_safe": ok,
            "err": e or None,
            "how_to_turn_off": ("已是关的；要人执行：从 /home/ubuntu/Strata/strata-iq3_s.json "
                                "删掉 \"api_monitor\": true（或加 false）后 ~/strata-ctl.sh restart "
                                "—— 本轮任务书不许动 8080，故未执行")}



def _write_run_meta(batch_dir: Path, a, cfg: dict, pre: dict, planned: list[str],
                    n: int, wall: float, rc: int, samp, cmp_res: dict,
                    stages: dict, warnings: list[str], log_before: int,
                    log_after: int, card: dict | None = None,
                    repeat_audit: dict | None = None) -> Path:
    """每次跑落一份环境快照 <batch>/_medrec/RUN_META.json（权限 600）。

    动机：2.7 s/例 达成过三次、也崩过 >=32.7 s/例，而「跑的时候机器上有什么」
    没有任何记录。这份快照让「这一轮的数能不能和历史比」变成可判定。
    """
    p = batch_dir / "_medrec" / "RUN_META.json"
    rec = {
        "ts": time.strftime("%F %T"), "epoch": int(time.time()),
        "batch": batch_dir.name, "rc": int(rc), "wall_s": round(wall, 3),
        "run_root": str(batch_dir.parent),
        "cases_planned": n, "cases": (planned[:200] if planned else []),
        "column_set": _column_set(),
        "key_params": _key_params(a),
        "strata": _strata_meta(pre),
        "ocr": ({**(pre.get("ocr") or {}),
                 "repeat_audit": repeat_audit} if repeat_audit
                else (pre.get("ocr") or {})),
        "gpu_snapshot_at_start": pre.get("gpu") or _gpu_snapshot(),
        "ocr_requests": ({"log": a.ocr_log, "pattern": a.ocr_log_pattern,
                          "lines_before": log_before, "lines_after": log_after,
                          "delta": (log_after - log_before)
                          if (log_before >= 0 and log_after >= 0) else None}
                         if a.ocr_log else {"log": "(none)"}),
        "sampling": samp.summary() if samp else {"enabled": False},
        "card_busy": card,
        "config_fp": _config_fp(a, planned),
        "stages": stages,
        "compare_prev": cmp_res,
        "warnings": list(warnings),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=1, sort_keys=True),
                     encoding="utf-8")
        os.chmod(p, 0o600)
        print(f"RUN_META={p} warnings={len(rec['warnings'])}")
    except OSError as exc:
        print(f"WARN 写 {p} 失败：{type(exc).__name__}: {exc}")
    return p

def _config_fp(a, case_order: list[str] | None = None) -> str:
    """可比性指纹 = 影响速度/产物的参数集合（RUN_META 里存一份，跨轮判「同配置」用）。

    `case_order` = **例的输入顺序**（manifest 顺序）。必须进指纹：
    `seq_no` 是例在输入清单里的位置，`--cases 文件顺序` 与 `目录扫描顺序` 不同 ⇒
    32/907 格 seq_no 整体位移（round31 r31_F1 vs r31_w16 = 3.859%，其中 32 格是 seq_no）。
    那不是噪声，是两次跑的**输入不同** —— 自动对照不该把它们配成一对。
    """
    kp = _key_params(a)
    keep = {k: kp[k] for k in ("steps", "force", "no_locate_cache", "allow_draft", "serial",
                               "K_case_parallel", "ocr_concurrency", "probe_concurrency",
                               "abort_repeat")}
    if case_order:
        keep["case_order_sha"] = hashlib.sha1(
            "\n".join(case_order).encode("utf-8")).hexdigest()[:12]
    return json.dumps(keep, sort_keys=True, ensure_ascii=False)


def _fp_match(stored: object, want: str) -> bool:
    """指纹相同 = 两边**共有的键**全部相等。

    共有的键（而不是全部键）⇒ 本轮之前落盘的 RUN_META 没有 `case_order_sha`，
    仍然能和本轮配对（只是少一道输入顺序校验），不会因为加字段就把历史基线全部作废。
    """
    try:
        s = json.loads(str(stored))
        w = json.loads(want)
    except (TypeError, ValueError):
        return str(stored) == want
    keys = set(s) & set(w)
    return bool(keys) and all(s.get(k) == w.get(k) for k in keys)


def _find_prev_batch(run_root: Path, batch_dir: Path, a, n: int = 0,
                     case_order: list[str] | None = None) -> str:
    """§9.3：自动找「上一次同配置跑」= 指纹相同、**例数相同**、mtime 最新的另一个批次。

    例数必须相同：拿 2 例的 smoke 去比 40 例，共同格只有几十格，差异率毫无意义
    （round31 smoke2 踩过）。要跨例数比就显式给 --compare-with。
    """
    fp = _config_fp(a, case_order)
    best = None
    for meta in run_root.glob("*/_medrec/RUN_META.json"):
        b = meta.parent.parent
        if b.resolve() == batch_dir.resolve():
            continue
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not _fp_match(d.get("config_fp"), fp):
            continue
        if n and d.get("cases_planned") not in (n, None):
            continue
        try:
            m = meta.stat().st_mtime
        except OSError:
            m = 0.0
        if best is None or m > best[0]:
            best = (m, b.name)
    return best[1] if best else ""


def cmd_stages(a) -> int:
    """只读：对一个已有批次重打「各阶段速度」表（跑完的/跑一半的都能查）。"""
    b = _batch_dir_of(a, a.batch)
    if not b.is_dir():
        print(f"ERROR 批次目录不存在：{b}")
        return 2
    n = 0
    cases: list[str] = []
    for sj in ([b / "_medrec" / "summary.json"]
               + sorted((b / "_medrec").glob("*.json")) if (b / "_medrec").is_dir() else []):
        if not sj.is_file() or sj.name == "stages.json":
            continue
        try:
            cs = json.loads(sj.read_text(encoding="utf-8")).get("cases") or []
        except (OSError, ValueError):
            cs = []
        if cs:
            cases = list(cs)
            n = len(cs)
            break
    if not cases:
        man = b / "00_index" / "manifest.tsv"
        if man.is_file():
            with open(man, encoding="utf-8") as fh:
                cases = [r.get("case_id", "") for r in csv.DictReader(fh, delimiter="\t")
                         if r.get("case_id")]
            n = len(cases)
    log_dir = b / "_pipeline_logs"
    if not cases and log_dir.is_dir():
        cases = sorted({p.name[:-4].rsplit("__", 1)[0] for p in log_dir.glob("*__*.log")})
    ts = _completion_times(log_dir, cases, 0.0)
    drop = DROP_FIRST
    steady = None
    if len(ts) >= drop + 2:
        steady = (ts[-1] - ts[drop - 1]) / (len(ts) - drop)
        n = n or len(ts)
    print(f"=== medrec stages {b.name}  {time.strftime('%F %T')} ===")
    _print_stages_table(b, _run_root(a), n or len(ts), steady, drop,
                        getattr(a, "summary", ""))
    return 0


def cmd_run(a) -> int:
    if getattr(a, "advanced", False):
        print(ADVANCED_HELP)
        return 0
    rc, why = _resolve_input(a)
    if rc != 0:
        print(why)
        return rc
    if a.run_root:
        os.environ["MEDREC_RUN_ROOT"] = str(Path(a.run_root).resolve())
        _patch_run_root_in_process(str(Path(a.run_root).resolve()))
    cfg = importlib.import_module("wide.common").load_pipeline()
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    # 重名保护：批次名是**从目录名派生**的时候才拦。显式 --batch = 续跑语义，照旧放行。
    dup = a.batch_derived and batch_dir.exists()
    if dup and not a.dry_run:
        print(f"ERROR 批次目录已存在：{batch_dir}\n"
              f"      接着跑（跳过已完成阶段）：medrec run {a.dir} --batch {a.batch}\n"
              f"      另起一轮：medrec run {a.dir} --batch <新名>\n"
              f"      —— 重名不自动加后缀：上一轮产物是证据，要复用它请显式写 --batch")
        return 2
    # ── round31 §5：--detach = 真后台化（setsid + 打印日志路径）。
    #    默认**不** detach：§5 要求「`bash medrec run <目录>` 结尾能看到那张阶段速度表」，
    #    默认就后台化会让那条命令立刻返回、表看不见的。默认改做「脱离父 shell 的
    #    作业控制」（os.setsid()，见下），既躲开 AGENTS.md §五.1 的「父 shell 退出被回收」，
    #    又保持前台可见。要真 fire-and-forget：`medrec run --detach ...`。
    if getattr(a, "detach", False) and not os.environ.get("MEDREC_DETACHED"):
        log_dir = Path(a.log_dir) if a.log_dir else batch_dir / "_medrec"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"run_{a.batch}.log"
        cmd = ([sys.executable, "-u", str(Path(__file__).resolve())]
               + sys.argv[1:] + ["--no-detach"])
        fh = open(log, "w", encoding="utf-8")
        os.chmod(log, 0o600)
        subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, env={**os.environ, "MEDREC_DETACHED": "1"})
        print(f"DETACHED batch={a.batch} 日志={log}")
        print(f"NEXT 查阶段速度表：medrec stages {a.batch}"
              + (f" --run-root {a.run_root}" if a.run_root else ""))
        print(f"NEXT 跟日志：tail -f {log}")
        return 0
    prod_index = Path(cfg["paths"]["run_root"]) / "allmap" / "00_index" / "manifest.tsv"
    prod_md5 = _md5(prod_index) if prod_index.is_file() else "(missing)"
    if a.steps.strip():
        rc, why, ids = _ensure_manifest(cfg, batch_dir, a)
        if rc != 0:
            print(f"FATAL {why}")
            return rc
        print(f"  {why}")
        a.manifest_ids = ids
    planned = _planned_cases(batch_dir, a)
    if not a.ocr_log:
        procs = _proc_by_cmd("vllm", "--port", os.environ.get("PORT_OCR_VL", "8110"))
        tgt = _log_target(procs[0][0]) if procs else ""
        a.ocr_log = tgt if tgt and Path(tgt).is_file() else ""
        if not a.ocr_log:
            print(f"NOTE 找不到 8110 的日志（--ocr-log 留空 → JSON 里 ocr_requests 计数不可用）")
    if not a.json_out:
        a.json_out = str(batch_dir / "_medrec" / "summary.json")
    argv = _build_run_argv(a)
    print("MEDREC run")
    if a.dir_root:
        print(f"  扫描目录={a.dir_root}（位置参数；**不用** pipeline.yaml:paths.data_root）")
    elif a.manifest_from:
        print(f"  扫描目录=（本轮不扫目录：例清单从 --manifest-from {a.manifest_from} 播种）")
    elif a.cases or a.case:
        print("  扫描目录=（本轮不扫目录：例来自 --case/--cases）")
    else:
        print(f"  扫描目录={cfg['paths']['data_root']}（没给目录参数 → 用 pipeline.yaml:paths.data_root）")
    print(f"  run_root={cfg['paths']['run_root']} batch={a.batch}"
          f"{'（派生自目录名）' if a.batch_derived else ''} → {batch_dir}")
    print(f"  cases={len(planned)}"
          + ("（--case/--cases 过滤后）" if (a.cases or a.case) else "（扫描目录里的全部例）"))
    print(f"  产线索引={prod_index} md5={prod_md5}（只读，medrec 不写它）")
    print(f"  K={os.environ.get('R15_CASE_PARALLEL','(unset)')} "
          f"CONC={os.environ.get('R23_OCR_CONCURRENCY','(unset)')} "
          f"PCONC={os.environ.get('R23_PROBE_CONCURRENCY','(unset)')} "
          f"(from scripts/config.env)")
    print(f"  ocr_log={a.ocr_log or '(none)'} json_out={a.json_out}")
    print(f"  mapping={'draft allowed (--allow-draft)' if not a.strict_mapping else 'strict (confirmed only)'}")
    print(f"  locate_cache={'OFF (--no-locate-cache)' if a.no_locate_cache else 'ON (production default)'}")
    if a.dry_run:
        print("  DRY-RUN wide.tools.run_steps " + " ".join(argv))
        print(f"  DRY-RUN 产物将落在 {batch_dir}/00_index … 06_table/wide.csv —— 本次不写盘、不建目录")
        if dup:
            print(f"  DRY-RUN 注意：{batch_dir} 已存在 → 正式跑会被重名保护拒绝（rc=2）。"
                  f"要续跑写 `medrec run {a.dir} --batch {a.batch}`，要另起一轮换 `--batch <新名>`")
        return 0

    # ── round31 §9.2 前置闸门：① OCR 端点必须活（硬停）② Strata 前置空闲 ③ 引擎龄期
    warn_list: list[str] = []
    rc_gate, pre = _preflight(cfg, a, warn_list)
    if rc_gate != 0:
        return rc_gate

    # ── round31 §5：默认脱离父 shell 的作业控制（AGENTS.md §五.1「nohup & 不够」）。
    #    只在**输出不是终端**时做（= 被脚本/agent 驱动）；人在终端里跑时保持前台语义，
    #    Ctrl-C 仍然有效。要强制：--setsid / --no-setsid。
    want_setsid = getattr(a, "setsid", None)
    if want_setsid is None:
        want_setsid = not sys.stdout.isatty()
    if want_setsid:
        if os.getpgrp() == os.getpid():
            # 已经是会话首进程（setsid nohup … & 起的，或被 systemd 直接拉起）——
            # 再调一次 setsid 必然 EPERM，这不是错误，别打成一堆红字。
            print("SETSID 已是会话首进程，无需再脱离（父 shell 退出不会回收本轮）")
        else:
            try:
                os.setsid()
                print("SETSID 本轮已脱离父 shell 的作业控制（父 shell 退出不会回收本轮）")
            except OSError as exc:
                print(f"NOTE os.setsid() 失败：{type(exc).__name__}: {exc}（不影响产线）")

    Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
    RS = importlib.import_module("wide.tools.run_steps")
    if a.run_root:  # 让每个阶段子进程也看到同一个 run_root
        shim = str(ROOT / "bin" / "medrec_stage.py")
        real = RS.subprocess

        def rewrite(cmd) -> list:
            """[py, -u, -m, wide.X, ...] → [py, -u, medrec_stage.py, wide.X, ...]

            run_steps 起的每条阶段命令都是 `python -u -m wide.<mod> ...`
            （src/wide/tools/run_steps.py:127 与 :228）—— 模块名在**下标 3**，
            不是 2（下标 2 是 "-m"）。
            """
            cmd = list(cmd)
            if len(cmd) >= 4 and cmd[2] == "-m" and str(cmd[3]).startswith("wide."):
                return [cmd[0], cmd[1], shim, cmd[3]] + cmd[4:]
            return cmd

        class _Shim:
            STDOUT = subprocess.STDOUT
            DEVNULL = subprocess.DEVNULL
            PIPE = subprocess.PIPE
            Popen = staticmethod(subprocess.Popen)
            CalledProcessError = subprocess.CalledProcessError

            @staticmethod
            def run(cmd, **kw):
                cmd = rewrite(cmd)
                return subprocess.run(cmd, **kw)

            @staticmethod
            def call(cmd, **kw):
                return subprocess.call(rewrite(cmd), **kw)

            @staticmethod
            def check_output(cmd, **kw):
                return subprocess.check_output(cmd, **kw)

            log_lines = staticmethod(real.log_lines) if hasattr(real, "log_lines") else None

        RS.subprocess = _Shim

    t0 = time.time()
    # 整轮 OCR 请求数 = 日志行数增量（起止各数一次）。
    # summary.json 里的 total_ocr_requests 是**每步增量之和**，K=24 下同一批请求被
    # 多个 worker 的窗口重复计入（实测 cli25c 26813 vs 真实 1151）—— 不能当请求数用。
    log_before = RS.log_lines(a.ocr_log, a.ocr_log_pattern) if a.ocr_log else -1
    # ── round31 §9.5：让「复读早停命中了几次」每次跑都留痕。
    #    不设 R24_REPEAT_LOG 时 PeriodicWatcher 照样判、照样断连，但**命中几次没人知道**
    #    ⇒ 落库的那笔收益（稳态 −9.7%）无法在后续跑里被验证。记录只有计数/sha/周期，无正文。
    ra_path = Path(os.environ.get("R24_REPEAT_LOG", "").strip()
                   or batch_dir / "_medrec" / "repeat_audit.jsonl")
    os.environ["R24_REPEAT_LOG"] = str(ra_path)
    os.environ.setdefault("R24_REPEAT_AUDIT", "1")
    ra_off = ra_path.stat().st_size if ra_path.exists() else 0
    print(f"REPEAT_AUDIT L={os.environ.get('R24_REPEAT_PERIOD_MIN_RUN', '(unset)')} "
          f"abort={os.environ.get('R24_REPEAT_ABORT_PERIOD', '(unset)')} "
          f"早停={_effective_abort_repeat()} → {ra_path}")

    # ── round31 §5/§9.1：事中采样（默认开）—— 「跑的时候机器上还有谁」只有这样才能归因
    samp = None
    if not getattr(a, "no_sample", False):
        ocr_gpus = _ocr_gpu_indices(pre.get("ocr") or {})
        samp = _Sampler(pre["llm_url"], batch_dir, interval=float(a.sample_interval),
                        max_poll=int(a.max_poll), gpus=ocr_gpus)
        samp.start()
        print(f"SAMPLING 每 {a.sample_interval}s → {samp.path}"
              f"（OCR 卡={sorted(ocr_gpus) if ocr_gpus else 'unknown ⇒ 卡忙不测'}）")
    rc = RS.main(argv)
    if samp is not None:
        samp.stop.set()
        if samp.thread:
            samp.thread.join(timeout=30)
    wall = time.time() - t0
    log_after = RS.log_lines(a.ocr_log, a.ocr_log_pattern) if a.ocr_log else -1
    if log_before >= 0 and log_after >= 0:
        print(f"OCR_REQ {a.ocr_log_pattern!r} delta={log_after - log_before} "
              f"({log_before} → {log_after} lines in {a.ocr_log})")
    n = len(planned) or _n_cases(a)
    print(f"MEDREC rc={rc} wall={wall:.2f}s cases={n} s_per_case_wall={wall / max(1, n):.3f}")
    steady, ts = _print_steady(batch_dir, a, planned, n, wall, t0)
    _report_table(batch_dir)
    now_md5 = _md5(prod_index) if prod_index.is_file() else "(missing)"
    print(("WARN 产线索引 md5 变了 " + f"{prod_index} {prod_md5} → {now_md5}") if now_md5 != prod_md5
          else f"ALLMAP 产线索引未改动 {prod_index} md5={now_md5}")
    print(f"NEXT 产物：{batch_dir}/00_index … 06_table/  （宽表 {batch_dir}/06_table/wide.csv）")
    print(f"NEXT 核对：medrec check {a.batch}"
          + (f" --compare <上一轮同配置 batch>" if not a.dry_run else ""))

    # ── round31 §5.x：结束时必打「各阶段速度」表（+ 机器可读 stages.json）
    run_root = Path(cfg["paths"]["run_root"])
    stages = _print_stages_table(batch_dir, run_root, n, steady, DROP_FIRST)

    # ── round31 §9.3：跑完自动与上一次同配置跑做逐格 sha 对照
    prev = (a.compare_with or _find_prev_batch(run_root, batch_dir, a, n, planned))
    mns = next((e.get("max_num_seqs") for e in (pre.get("ocr") or {}).get("endpoints", [])
                if e.get("max_num_seqs")), None)
    cmp_res = _compare_prev(batch_dir, prev, run_root, mns)
    if cmp_res.get("ok"):
        rate = cmp_res["gate_rate_pct"]
        lo, hi = cmp_res["noise_band_pct"]
        verdict = ("FAIL 超出同配置噪声带上沿" if rate > hi
                   else ("OK 落在同配置噪声带内（高于地板 %.1f%%，仅诊断）" % lo if rate > lo
                         else "OK 落在同配置噪声带内"))
        print(f"COMPARE {prev}: 共同格={cmp_res['common']} 差异={cmp_res['diff']} "
              f"差异率={rate:.3f}%（噪声带 {lo}–{hi}%；{cmp_res['noise_band_why']}）⇒ {verdict}"
              f"；scan_trace 不同的例数={cmp_res['scan_trace_differ']}")
        if cmp_res.get("diff_excl_seq_no", 0) != cmp_res.get("diff", 0):
            print(f"COMPARE 其中 seq_no（= 例在输入清单里的位置，非抽取结果）占 "
                  f"{cmp_res['diff'] - cmp_res['diff_excl_seq_no']} 格；"
                  f"两次**输入顺序不同** ⇒ seq_no 不参与判红，"
                  f"判红口径差异率={cmp_res['gate_rate_pct']:.3f}%"
                  f"（全量={cmp_res['diff_rate_pct']:.3f}%）")
        if rate > hi:
            warn_list.append(f"cell_sha_diff_rate_above_noise_band: {rate:.3f}% "
                             f"> 上沿 {hi}%（vs {prev}；口径=输入顺序相同时含 seq_no，"
                             f"不同时去 seq_no）")
        # 地板**不判 WARN**（见 _noise_band 文档）：同配置 64 对里 15 对 >1.0%，
        # 判红会让「连续 3 次无 warning」永远不可能满足。地板只写进 RUN_META 供诊断。
        # scan_trace 变化只记录、不判 WARN：§6a.3 的判据是「逐格 sha 差异率」，
        # 定位页序本身在 locate 缓存开/关时会变，判成 warning 会把噪声变成红灯。
    else:
        print(f"COMPARE 跳过：{cmp_res.get('why')}"
              "（--compare-with <batch> 可显式指定上一轮；首轮无从对照）")

    # ── round31 §9.1：采样结果 + 卡忙（round28 口径）→ warnings
    card: dict | None = None
    busy: float | None = None
    nwin = 0
    if samp is not None:
        s = samp.summary()
        print(f"SAMPLING samples={s['n_samples']} poll_max={s['poll_max']}"
              f"（瞬时；外部请求阈值 {s['max_poll']}）"
              f"external_requests={s['external_requests']}"
              f"（Strata 计数 +{s['totals_delta']} − 我方审计 {s['ours_total']}）"
              f" in_flight_peak={s['in_flight_peak']} "
              f"tok_s_median={s['tok_s_median']} card_busy_s(全程)={s['card_busy_s']} "
              f"errors={s['errors']}")
        if s["external_requests"] and s["external_requests"] > s["max_poll"]:
            warn_list.append(
                f"strata_external_requests: 本次跑期间有 "
                f"{s['external_requests']} 个**不是本管线发的** Strata 请求完成"
                f"（首个 @ts={s['external_first_ts']}）⇒ 引擎 concurrency.serving=1，"
                f"每个外部请求都会插进我们的队列 ⇒ 本轮 s/例 不可与基线比")
        elif s["invalid_at"]:
            warn_list.append(f"strata_contaminated_during_run: poll_max={s['poll_max']} "
                             f"> {s['max_poll']} ⇒ 本轮 s/例 不可与历史基线比")
        if s["errors"]:
            warn_list.append(f"sampling_errors: n={s['errors']}")
        if len(ts) >= DROP_FIRST + 2 and s.get("ocr_gpus"):
            busy, nwin = _card_busy_window(batch_dir, ts[DROP_FIRST - 1], ts[-1],
                                           set(s["ocr_gpus"]))
            card = {"gpus": s["ocr_gpus"], "steady_window_s": busy,
                    "samples_in_window": nwin,
                    "s_per_case": (round(busy / (len(ts) - DROP_FIRST), 4)
                                   if busy is not None else None),
                    "definition": "稳态窗口内 OCR 卡 util>0 的采样秒 ÷ (N-drop)"}
            print(f"CARD_BUSY {card['definition']} = {card['s_per_case']}"
                  f"（窗口内忙 {busy}s / 样本 {nwin}）")
        else:
            why = ("完成时刻不足" if len(ts) < DROP_FIRST + 2 else "OCR 卡号未知")
            print(f"CARD_BUSY 不测：{why}")
        if busy is None:
            warn_list.append("card_busy_not_measurable")
    else:
        warn_list.append("card_busy_not_measurable: --no-sample（事中采样关闭）")

    if steady is None:
        warn_list.append("steady_not_measurable")
    if not n:
        warn_list.append("no_planned_cases")
    if rc != 0:
        warn_list.append(f"run_steps_rc={rc}")

    # ── round31 §9.1 / §9.7：RUN_META + 基线 append
    ra = _repeat_audit_summary(ra_path, ra_off)
    try:
        os.chmod(ra_path, 0o600)
    except OSError:
        pass
    print(f"REPEAT_ABORT 断连={ra['aborted']}/{ra['records']} 条 OCR 响应 "
          f"判据={ra.get('criteria') or '无'} "
          f"L={os.environ.get('R24_REPEAT_PERIOD_MIN_RUN', '(unset)')} "
          f"→ {ra_path}")
    _write_run_meta(batch_dir, a, cfg, pre, planned, n, wall, rc, samp, cmp_res,
                    stages, warn_list, log_before, log_after, card,
                    ra)
    _append_baselines(run_root, {
        "ts": time.strftime("%F %T"), "batch": batch_dir.name, "N": n,
        "steady_s": (round(steady, 4) if steady is not None else None),
        "card_busy_s": ((card or {}).get("steady_window_s")),
        "card_busy_per_case": ((card or {}).get("s_per_case")),
        "wall_s": round(wall, 3),
        "stages": stages.get("stage_wall_s"),
        "stages_sum_s": stages.get("sum_wall_s"),
        "ocr_requests": ((log_after - log_before)
                         if (log_before >= 0 and log_after >= 0) else None),
        "config_fp": _config_fp(a, planned),
        "meta": {"warnings": warn_list, "rc": int(rc),
                 "ocr": [e["url"] for e in (pre.get("ocr") or {}).get("endpoints", [])]}})
    if warn_list:
        print(f"WARN 本次 warnings={len(warn_list)}：")
        for w in warn_list:
            print(f"  - {w}")
    else:
        print("WARNINGS 空（前置闸门 / 事中采样 / 一致性对照 都没报警）")
    return int(rc)


def _print_steady(batch_dir: Path, a, planned: list[str], n: int, wall: float,
                  t0: float) -> tuple[float | None, list[float]]:
    """第二个口径：稳态 s/例。两个数并排打，定义写在同一行，别再拿 7.368 去比 2.672。

    wall   = 整批墙钟 ÷ 例数（含 locate、增量出表、收尾全量 s5/s6）—— round25 的 7.368 就是这个。
    steady = 每例完成时刻排序后 (最后例 − 第 drop_first 例) ÷ (N − drop_first)。
             完成时刻 = `_pipeline_logs/<case>__<step>.log` 的 mtime，每例取最大 = 该例最后一步结束。
             N=40 / drop_first=10 时与测速口径（workspace/round25/scripts/steady25.py:6-8
             「(第 N 例完成 − 第 N−30 例完成)/30，丢前 10 例」）**同式**。
    """
    drop = DROP_FIRST
    log_dir = Path(a.log_dir) if a.log_dir else batch_dir / "_pipeline_logs"
    ts = _completion_times(log_dir, planned, t0)
    if len(ts) < drop + 2:
        print(f"  s_per_case_steady=n/a（本轮只数到 {len(ts)}/{n} 个每例完成时刻；"
              f"--serial 跑法没有 per-case 日志，或这是续跑（多数例本轮没重跑））")
        return None, ts
    w = ts[-1] - ts[drop - 1]
    print(f"  s_per_case_steady={w / (len(ts) - drop):.3f} "
          f"(W_steady={w:.2f}s/(N-{drop}) drop_first={drop} N={len(ts)})")
    print(f"  口径 s_per_case_wall=整批墙钟/例数（含 locate+出表+收尾）；"
          f"s_per_case_steady=(最后例完成−第{drop}例完成)/(N−{drop})，"
          f"完成时刻取 {log_dir}/<case>__*.log 的 mtime")
    return w / (len(ts) - drop), ts


def _patch_run_root_in_process(run_root: str) -> None:
    """把 paths.run_root 的覆盖装进**本进程**的 wide.common.load_pipeline。

    run_steps.main() 自己调 C.load_pipeline()（src/wide/tools/run_steps.py:377），
    只改 medrec_cli 里那份 cfg 不够 —— 它会照 pipeline.yaml 的默认 run_root 去找
    <run_root>/<batch>/00_index/manifest.tsv。产线全部模块都是 C.load_pipeline()
    （没有 from wide.common import load_pipeline），所以改模块属性就能全覆盖。
    阶段**子进程**由 bin/medrec_stage.py 做同样的事。
    """
    C = importlib.import_module("wide.common")
    orig = C.load_pipeline

    def load_pipeline(path=None):  # noqa: D103
        cfg = orig(path)
        cfg["paths"]["run_root"] = run_root
        return cfg

    C.load_pipeline = load_pipeline


def _ensure_manifest(cfg: dict, batch_dir: Path, a) -> tuple[int, str, list[str]]:
    """缺 00_index/manifest.tsv 时按产线口径补上（run_steps 硬要求它）。

    产线唯一的生成路径是 wide.s1_ocr --batch B --discover
    （src/wide/s1_ocr.py:1349-1353 → discover(cfg["paths"]["data_root"])）。
    这里直接调同一对函数（discover + write_manifest），不另造第二套清单逻辑。
    `medrec run <目录>` 只把 discover 的**根**换成传入的目录，别的都不换。
    --manifest-from 给另一个 batch 的现成清单 → 只挑本次 cases 用到的行，省一次全扫。
    """
    S1 = importlib.import_module("wide.s1_ocr")
    manifest = batch_dir / "00_index" / "manifest.tsv"
    prod_index = Path(cfg["paths"]["run_root"]) / "allmap" / "00_index" / "manifest.tsv"
    if manifest.is_file():
        try:
            ids = [r.get("case_id", "") for r in S1.read_manifest(manifest) if r.get("case_id")]
        except Exception:  # noqa: BLE001
            ids = []
        return 0, f"manifest 复用（本轮不重扫）{manifest} sha256={_sha256(manifest)}", ids
    if manifest.resolve() == prod_index.resolve():
        return 2, (f"MANIFEST 拒写：{manifest} 就是产线索引（只读）。"
                   "换个 --batch 名，别用 allmap。"), []
    dry = bool(getattr(a, "dry_run", False))

    if a.manifest_from:
        src = Path(a.manifest_from)
        if not src.is_file():
            cand = Path(cfg["paths"]["run_root"]) / a.manifest_from / "00_index" / "manifest.tsv"
            if not cand.is_file():
                return 2, f"MANIFEST missing: {a.manifest_from}（也不是 {cand}）", []
            src = cand
        rows = S1.read_manifest(src)
        idx = {r["case_id"]: r for r in rows}
        want = _case_list(a)
        picked = want if want else list(idx)
        miss = [c for c in picked if c not in idx]
        if miss:
            return 2, f"MANIFEST 源里没有这些 case_id：{miss[:5]}（n={len(miss)}，源 {src}）", []
        if dry:
            return 0, (f"manifest 将播种于 {manifest}（dry-run，未写盘）"
                       f" src={src} cases={len(picked)}"), picked
        manifest.parent.mkdir(parents=True, exist_ok=True)
        S1.write_manifest(manifest, [idx[c] for c in picked])
        return 0, f"manifest seeded cases={len(picked)} src={src}", picked

    root = Path(a.dir_root) if getattr(a, "dir_root", "") else Path(cfg["paths"]["data_root"])
    origin = "位置参数 <目录>" if getattr(a, "dir_root", "") else "pipeline.yaml:paths.data_root"
    if not root.is_dir():
        return 2, f"MANIFEST 无法生成：扫描根不存在或不是目录 {root}（来源 {origin}）", []
    rows = S1.discover(root)
    if not rows:
        return 2, (f"MANIFEST 无法生成：discover({root}) 返回 0 例 —— "
                   "该目录下没有 *.pdf？case_id 按「相对扫描根的目录层级」算（院区/病例），"
                   "换到含 PDF 的那一层再跑"), []
    ids = [r.get("case_id", "") for r in rows if r.get("case_id")]
    # round34 E4：退化 case_id 硬闸 —— 扫描根本身就是病例目录时 rel.parent="." ⇒
    # case_id="." ⇒ 产物直接落进**阶段根**（01_ocr/_manifest.json、01_ocr/.done）互相覆盖。
    deg = sorted({str(x).strip() for x in ids if str(x).strip() in ("", ".", "..")})
    if deg:
        return 2, (f"MANIFEST 拒写：discover({root}) 出了退化 case_id {deg} —— 扫描根本身就是"
                   "病例目录（叶子目录）时 rel.parent='.'，case_id 变成 '.'，产物会落进阶段根"
                   "并互相覆盖（round33 C2 首跑 922 s 作废 / round34 E4）。"
                   "改法：扫描根抬到院区或更上层，或用 --case <规范 case_id> --batch TAG"), []
    dup = sorted({x for x in ids if ids.count(x) > 1})
    if dup:
        return 2, (f"MANIFEST 拒写：case_id 重复 {dup[:5]}（n={len(dup)}）—— "
                   "同一 case_id 的产物会互相覆盖，先修扫描根"), []
    if dry:
        return 0, (f"manifest 将生成于 {manifest}（dry-run，未写盘）"
                   f" 扫描根={root} 预计例数={len(rows)}"), ids
    manifest.parent.mkdir(parents=True, exist_ok=True)
    S1.write_manifest(manifest, rows)
    return 0, (f"manifest 新生成 扫描根={root}（{origin}）cases={len(rows)} "
               f"→ {manifest} sha256={_sha256(manifest)}"), ids


def _n_cases(a) -> int:
    return len(_case_list(a))


def _case_list(a) -> list[str]:
    out = list(getattr(a, "case", None) or [])
    if a.cases:
        try:
            out += [x.strip() for x in Path(a.cases).read_text(encoding="utf-8").splitlines()
                    if x.strip()]
        except OSError:
            pass
    return out


def _planned_cases(batch_dir: Path, a) -> list[str]:
    """本轮**实际会跑**的例清单 —— 与 run_steps 同口径，s/例 的分母用它。

    src/wide/tools/run_steps.py:181-183：cases = [r["case_id"] for r in manifest
    if not want or r["case_id"] in want]。所以分母是「manifest ∩ --case/--cases」，
    不是 --cases 文件的行数（清单里没有的例会被静默跳过，那样 s/例 会虚高）。
    """
    want = set(_case_list(a))
    if getattr(a, "manifest_ids", None) is not None:
        # dry-run：manifest 还没落盘，用刚 discover 出来的例数（round26 §7.2 要求先打印发现多少例）
        return [c for c in a.manifest_ids if not want or c in want]
    man = batch_dir / "00_index" / "manifest.tsv"
    if man.is_file():
        try:
            rows = importlib.import_module("wide.s1_ocr").read_manifest(man)
        except Exception:  # noqa: BLE001
            return sorted(want)
        ids = [r.get("case_id", "") for r in rows if r.get("case_id")]
        return [c for c in ids if not want or c in want]
    return sorted(want)


def _completion_times(log_dir: Path, cases: list[str], t0: float) -> list[float]:
    """每例完成时刻 = 该例各阶段日志 mtime 的最大值；只认本轮（mtime ≥ t0）的。"""
    out: list[float] = []
    if not log_dir.is_dir():
        return out
    for cid in cases:
        newest = 0.0
        for f in log_dir.glob(glob.escape(cid) + "__*.log"):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
        if newest >= t0:
            out.append(newest)
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(prog="medrec", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="一条命令跑完整链并出宽表",
                       description="跑一批：`medrec run <病历目录>` —— 日常只需要这一个参数。\n"
                                   "批次名缺省 = 目录名；产物 <run_root>/<批次名>/06_table/wide.csv。\n"
                                   "按 case_id 跑（round25 用法）：`medrec run --cases FILE --batch TAG`。",
                       epilog=ADVANCED_HELP,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    r.add_argument("dir", nargs="?", default="",
                   help="病历目录（下面按 院区/病例/*.pdf 摆）—— 日常只需要这一个参数，不用 flag")
    # round26 §5.2/§7.8：help 第一段只留「日常」两个旋钮，其余分组往下放，调试旋钮 SUPPRESS 到 epilog
    daily = r.add_argument_group("日常", "跑一批只需要这些（`medrec run <病历目录>` 一个参数就够）")
    daily.add_argument("--dry-run", action="store_true",
                       help="只打印将要执行的参数（不写盘、不建目录）")
    daily.add_argument("--advanced", action="store_true",
                       help="列出调试/对照旋钮后退出")
    pick = r.add_argument_group("指定跑哪些例 / 批次名", "不给 <目录> 时才需要")
    pick.add_argument("--batch", default="",
                      help="批次名 → <run_root>/<batch>/；不给就用目录名（重名直接拒绝，不静默加后缀）")
    pick.add_argument("--cases", default="", help="case_id 列表文件，一行一个（不给目录时用）")
    pick.add_argument("--case", action="append", default=[],
                      help="单个 case_id，可重复（不给目录时用）")
    pick.add_argument("--steps", default=STEPS_ALL,
                      help=f"默认 {STEPS_ALL}；给子集 = 续跑/只重出表")
    where = r.add_argument_group("产物落点 / 计数来源", "默认都对，一般不用给")
    where.add_argument("--run-root", default="",
                       help="覆盖 config/pipeline.yaml 的 paths.run_root（默认就是它，产物 run/wide/）")
    where.add_argument("--manifest-from", default="",
                       help="新 batch 缺 00_index/manifest.tsv 时，从另一个 batch（名或路径）的清单播种；"
                            "不给就现扫 <目录>（没给目录才退回 config/pipeline.yaml:paths.data_root）")
    where.add_argument("--ocr-log", default="",
                       help="OCR 请求计数日志（默认自动找 8110 进程的 stdout）")
    where.add_argument("--json-out", default="",
                       help="默认 <run_root>/<batch>/_medrec/summary.json")
    # 调试 / 对照旋钮：不进 --help 主表（round26 §5.2），列在 help 末段 epilog 与 `run --advanced`
    r.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--serial", action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--no-final-table", action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--strict-mapping", action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--no-locate-cache", action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--ocr-log-pattern", default="POST /v1/chat/completions", help=argparse.SUPPRESS)
    r.add_argument("--log-dir", default="", help=argparse.SUPPRESS)
    # round31 §5/§9：后台化 + 事中采样 + 前置闸门 + 一致性对照
    obs = r.add_argument_group("可观测 / 守卫（round31 起默认开）",
                               "跑的时候机器上有什么 —— 以前没记录，所以 2.7 与 32.7 无从归因")
    obs.add_argument("--no-sample", action="store_true",
                     help="关掉事中采样（默认开：每秒采 Strata /metrics、每 2 s 采 GPU）")
    obs.add_argument("--sample-interval", type=float, default=1.0,
                     help="事中采样间隔秒（默认 1.0）")
    obs.add_argument("--max-poll", type=int, default=0,
                     help="事中污染阈值：跑期间完成的**外部** Strata 请求数 > 此值 ⇒ 本轮标 WARN"
                          "（默认 0 = 零容忍；Strata concurrency.serving=1，round31 实测"
                          "1 个外部请求就把稳态从 2.50 抬到 3.29 s/例）")
    obs.add_argument("--busy-wait-s", type=int, default=90,
                     help="前置闸门②：Strata 忙时最多等这些秒（默认 90，每 10 s 复查）；0 = 不等")
    obs.add_argument("--ignore-busy", action="store_true",
                     help="前置闸门②：Strata 当前有人在用也照跑（默认打印警告并等 30 s）")
    obs.add_argument("--compare-with", default="",
                     help="跑完与哪个批次做逐格 sha 对照（不给 = 自动找上一次同配置跑）")
    obs.add_argument("--detach", action="store_true", default=False,
                     help="真后台跑（setsid + 打印日志路径后立刻返回）；表用 `medrec stages <batch>` 看")
    obs.add_argument("--no-detach", action="store_false", dest="detach", help=argparse.SUPPRESS)
    obs.add_argument("--setsid", action="store_true", default=None,
                     help="强制脱离父 shell 作业控制（默认：stdout 不是终端时自动做）")
    obs.add_argument("--no-setsid", action="store_false", dest="setsid", help=argparse.SUPPRESS)
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("check", help="只读核对一个批次的产物（命中数 / 页数 / 日期 / 逐格 sha）")
    c.add_argument("batch", help="批次名，或直接给批次目录")
    c.add_argument("--run-root", default="", help="覆盖 config/pipeline.yaml 的 paths.run_root")
    c.add_argument("--compare", default="",
                   help="另一个批次名/目录 → 逐格 sha 差异率 + 噪声带判红"
                        "（OCR 确定性开关在：0.4–0.7%%；现网 --max-num-seqs 64：1.0–2.0%%）")
    c.add_argument("--cell-sha-out", default="",
                   help="逐格 sha 落点（默认 <batch>/_medrec/cell_sha.json，只落 sha 不落正文）")
    c.add_argument("--only-cases", dest="cases", default="",
                   help="只统计/比对这个 case_id 清单文件里的例（等价性验证用）")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("stages", help="只读：重打一个批次的「各阶段速度」表（跑一半的也能查）")
    s.add_argument("batch", help="批次名，或直接给批次目录")
    s.add_argument("--run-root", default="", help="覆盖 config/pipeline.yaml 的 paths.run_root")
    s.add_argument("--summary", default="",
                   help="指定含 pipeline_step_wall_ms 的 JSON（默认找 <batch>/_medrec/*.json）")
    s.set_defaults(func=cmd_stages)

    d = sub.add_parser("doctor", help="只读体检：8110 / 8080 / 显存 / config.env / cases")
    d.add_argument("--cases", default="", help="顺带校验 case 列表")
    d.add_argument("--batch", default="", help="顺带校验该 batch 的产物目录可写")
    d.add_argument("--run-root", default="", help="配合 --batch 校验")
    d.set_defaults(func=cmd_doctor)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
