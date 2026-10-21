#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/run_steps.py —— 单例/多例的整链跑法 + 逐段计时（2026-10-09 新增）。

为什么加它：上一轮 s4 rc=3 / s6 rc=1 的直接原因是**临时拼出来的命令行漏了
--allow-draft**，而 s4/s6 的退出码又让「产物是上一轮留下的」看起来像正常。
整链跑法固化成一个文件，标志位就不会再丢，逐段耗时也才有统一口径。

用法：
    python -m wide.tools.run_steps --batch smoke --case 奉化__20230728_008 --allow-draft
    python -m wide.tools.run_steps --batch smoke --case-file cases.txt --force
    python -m wide.tools.run_steps --batch smoke --case X --steps locate,s1 --json-out t.json

计时口径（写进 --json-out）：每段 wall ms + rc + PaddleX 日志行数增量（= OCR 请求数）。
OCR 日志路径用 --ocr-log 指定；不给就不统计请求数（不猜日志在哪）。
"""
from __future__ import annotations

import argparse
import io
import json
import queue
import subprocess
import re
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from wide import common as C  # noqa: E402

# 段名 → (模块, 额外参数来源)
STEPS = ("locate", "s1", "s2", "s3", "s4", "s5", "s6")
FORCE_OK = {"s1", "s2", "s3", "s4", "s5"}   # s6 是纯汇总，没有 --force（传了会 rc=2）
MODULE = {"locate": "wide.tools.locate_pages", "s1": "wide.s1_ocr",
          "s2": "wide.s2_preprocess", "s3": "wide.s3_extract",
          "s4": "wide.s4_normalize", "s5": "wide.s5_qc", "s6": "wide.s6_table"}


def log_lines(path: str, pattern: str = "POST /layout-parsing") -> int:
    """OCR 请求数口径：数 OCR 服务日志里匹配 `pattern` 的行（默认 PaddleX `/layout-parsing`）。

    2026-10-10：OCR 可以是 vLLM（8110），它的访问日志行是 `POST /v1/chat/completions`
    → 用 --ocr-log-pattern 换口径，否则 vLLM 批次会数出 0 次请求。

    早先用 wc -l 整行数，会把健康检查的 `GET /v1/models` 也算成请求 —— 数虚高。

    2026-10-19 round48 T1：整读改**偏移游标**（现网 96 MB 日志整读单价 0.1995 s，每例
    每步 2 次 ⇒ 240 例 574 s；且 `sum(1 for line in f)` 持 GIL ⇒ 24 worker 互相串行）。
    ★ 返回值必须与整读【逐次全等】—— 下游拿它当 OCR 请求数，数错则所有账全错。
      等价性靠 `_scan_chunk`：universal newlines + 尾行只进返回值不进游标。
    ★ 轮转兜底：inode 变 / size 缩 / 文件头 48 B 变 ⇒ 全量重扫，并把已轮转段的累计值
      带进游标 ⇒ 返回值连续、不跳变、不重置。证据与实测见 workspace/round48/JOURNAL.md。
    """
    if not path or not pattern:
        return -1
    try:
        st = Path(path).stat()
    except OSError:
        return -1
    pat_b = pattern.encode("utf-8", "replace")
    with _CURSOR_LOCK:
        cur = _LOG_CURSOR.setdefault((path, pattern), {"ino": st.st_ino, "off": 0,
                                                       "count": 0, "head": None})
        with open(path, "rb") as f:
            head = f.read(48)
            cold = cur["head"] is None
            rotated = (cur["ino"] != st.st_ino or st.st_size < cur["off"]
                       or (not cold and head != cur["head"]))
            f.seek(cur["off"] if not (rotated or cold) else 0)
            data = f.read()
        n, tl, adv = _scan_chunk(data, pat_b)
        cur["off"] = adv if rotated else cur["off"] + adv
        cur["count"] += n          # 轮转时旧段的累计值留在 count 里 ⇒ 不重置
        cur["ino"], cur["head"] = st.st_ino, head
        return cur["count"] + tl


_CURSOR_LOCK = threading.Lock()
_LOG_CURSOR: dict[tuple[str, str], dict] = {}


def _scan_chunk(data: bytes, pat_b: bytes) -> tuple[int, int, int]:
    """(完整行命中数, 未闭合尾行是否命中, 可安全推进的字节数)。

    行分隔按文本模式 = `\\r\\n` | `\\r` | `\\n`（现网日志实测有 429 个孤立 `\\r`）。
    未闭合尾行只计入返回值、不进游标 ⇒ 重扫它也不会重复累加。
    末尾孤立的 `\\r` 可能是 `\\r\\n` 的前半 ⇒ 不消费，留给下次判定。
    """
    cut = data.rfind(b"\n") + 1
    i = data.rfind(b"\r")
    if i >= cut:
        cut = i + 1 if i + 1 < len(data) else i
    head = data[:cut]
    if b"\r" in head:               # 罕见：只在这些段里再按 \r 切一次
        n = sum(sum(1 for t in s.split(b"\r") if pat_b in t) if b"\r" in s
                else (1 if pat_b in s else 0) for s in head.split(b"\n"))
    else:
        n = sum(1 for s in head.split(b"\n") if pat_b in s)
    return n, (1 if data[cut:] and pat_b in data[cut:] else 0), cut


def raw_log_lines(path: str) -> int:
    """任务书字面口径：`wc -l /tmp/paddlex_ocr.log` 的整行数（含 GET / 启动噪声）。

    两个口径都记，报告里对照 —— 差值就是日志里的非 OCR 行数。
    """
    if not path:
        return -1
    p = Path(path)
    if not p.exists():
        return -1
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


# ── 流水线（2026-10-10 round8 第 2 件）─────────────────────────────────────
# 现状：`for s in steps: subprocess.call` = 整批串行，七阶段之和 == 墙钟（零重叠）。
# s1 吃 GPU0、s3 吃 Strata(GPU1/2/3) —— 两块不同硬件，重叠不抢任何东西。
LANE_A = ("locate", "s1", "s2")   # GPU0 / OCR
LANE_B = ("s3", "s4", "s5")       # Strata / 抽取


# ── 5 号补丁（round15）：批级 00_index/page_index.json 只有一个写者 = run_steps ──
# 根因：src/wide/tools/locate_pages.py:967-976 对批级 page_index.json 读-改-写、无锁；
#       例级并行（2a）下 K 个 locate 进程各写一份「自己看到的 existing」→ 后写的整份覆盖先写的
#       → 实测 r15p1p 只剩 6/10、r15p2p 只剩 5/10 例，s1 读不到本例条目 → 回退扫前 3 页
#       → 首页没 OCR → 交付表命中数从 226/240 掉到 189/170（红线⑦意义上的「少抽了」）。
# 改法：locate 只写自己的分片 00_index/page_index/<case>.json（跨进程无共享文件，天然无锁）；
#       批级文件由本进程（单写者 + threading.Lock）在每例 locate 之后与收尾各合并一次。
_PI_LOCK = threading.Lock()


_PI_CACHE: dict = {}


def _pi_part(cid: str, entry: dict) -> str:
    """单个 case 条目在整份 json 里的序列化片段。

    json.dumps 对 dict 的输出 = 各项单独 dumps 再用 ", " 连接 ⇒ 单独 dumps 出来的
    片段与它在整份 dumps 里的字节完全相同 ⇒ 按 cases 键序拼串 = json.dumps(整份) 逐字节等价。
    """
    return (json.dumps(cid, ensure_ascii=False) + ": "
            + json.dumps(entry, ensure_ascii=False))


def _pi_write(fh, meta: dict, cases: dict, parts: dict) -> None:
    """流式落盘。原实现每例 json.dumps(整份) 再写（29.6 MB，且整段在 _PI_LOCK 里）。

    分块 join：既不构造 22.8 MB 的整份 str，也不对整份再做一次 UTF-8 编码；
    写出的字节与 json.dumps({**meta, "cases": cases}, ensure_ascii=False) 逐字节相同。
    """
    fh.write("{")
    first = True
    for k, v in meta.items():
        if not first:
            fh.write(", ")
        first = False
        fh.write(json.dumps(k, ensure_ascii=False) + ": "
                 + json.dumps(v, ensure_ascii=False))
    if not first:
        fh.write(", ")
    fh.write('"cases": {')
    chunk: list = []
    first = True
    for cid, e in cases.items():
        if not first:
            chunk.append(", ")
        first = False
        chunk.append(parts.get(cid) or _pi_part(cid, e))
        if len(chunk) >= 512:
            fh.write("".join(chunk))
            chunk = []
    if chunk:
        fh.write("".join(chunk))
    fh.write("}}")


def _pi_serialize(meta: dict, cases: dict, parts: dict) -> str:
    """_pi_write 的字符串视图（只给对照/夹具用；唯一实现是 _pi_write）。"""
    buf = io.StringIO()
    _pi_write(buf, meta, cases, parts)
    return buf.getvalue()


def _pi_read_shard(sp: Path) -> tuple[str, dict]:
    try:
        blob = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        C.warn(f"分片 {sp.name} 读不动 → 跳过")
        return "", {}
    return (blob.get("case_id") or sp.stem), (blob.get("entry") or {})


def open_private(path, encoding: str = "utf-8"):
    """以 **0600** 创建/覆盖产物文件。

    必须用 O_CREAT 直接带 mode：先 open() 再 chmod() 中间有个窗口，文件已经以
    umask 的 664 落盘了。`_pipeline_logs/*.log` 的文件名里带 case_id，很多院区的
    case_id 就是患者姓名 ⇒ 664 = 同机任何用户可读病历（红线⑨「产物 600」）。
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "w", encoding=encoding)


def merge_page_index(batch_dir: Path, only_case: str = "") -> tuple[int, int]:
    """把 00_index/page_index/*.json 分片合并进批级 page_index.json。返回 (例数, 分片数)。"""
    idx_dir = batch_dir / "00_index"
    shard_dir = idx_dir / "page_index"
    out_path = idx_dir / "page_index.json"
    with _PI_LOCK:
        # round45（G1）：原实现每例都重读**整份批级 json + 全部分片** ⇒ O(N)/例、整批 O(N²)。
        # 实测 N=3777 单次 1.9s（workspace/round45/probes/g1_merge_cost.py），×3778 次且整段在
        # _PI_LOCK 里 ⇒ 24 worker 全局串行 ≈7.2ks，占该批 wall 的 ~35%，随 N 线性涨 ⇒ 批内掉速。
        # 改法：本进程是单写者，合并结果缓存进内存；only_case 非空时只读**本例**分片。
        # only_case 为空（收尾）仍走全量重扫 ⇒ 与改前逐字同语义。批级 json 每次仍整份重写
        # ⇒ 紧随其后的 s1 一定能读到本例条目（产线语义不变）。
        if only_case and _PI_CACHE.get("key") == str(batch_dir):
            sp = shard_dir / f"{only_case}.json"
            if sp.exists():
                cid, entry = _pi_read_shard(sp)
                if entry:
                    _PI_CACHE["cases"][cid] = entry
                    _PI_CACHE["parts"][cid] = _pi_part(cid, entry)
                    _PI_CACHE["seen"].add(sp.name)
        else:
            meta, merged, parts, seen = {}, {}, {}, set()
            if out_path.exists():
                try:
                    blob = json.loads(out_path.read_text(encoding="utf-8"))
                    merged = blob.get("cases") or {}
                    meta = {k: v for k, v in blob.items() if k != "cases"}
                except Exception:                                        # noqa: BLE001
                    C.warn(f"{out_path.name} 读不动 → 由分片重建")
            for sp in sorted(shard_dir.glob("*.json")) if shard_dir.exists() else []:
                cid, entry = _pi_read_shard(sp)
                if entry:
                    merged[cid] = entry
                    parts[cid] = _pi_part(cid, entry)
                    seen.add(sp.name)
            _PI_CACHE.update({"key": str(batch_dir), "meta": meta,
                              "cases": merged, "parts": parts, "seen": seen})
        meta = dict(_PI_CACHE["meta"])
        n_shard = len(_PI_CACHE["seen"])
        meta.update({"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                     "source": "strip_ocr_shards", "n_shards": n_shard})
        tmp = out_path.with_name(out_path.name + ".tmp")
        with open_private(tmp) as fh:
            _pi_write(fh, meta, _PI_CACHE["cases"], _PI_CACHE.get("parts") or {})
        os.replace(tmp, out_path)
        return len(_PI_CACHE["cases"]), n_shard


def build_cmd(s: str, batch: str, case: str, a) -> list:
    """单例版命令：与整批跑法同一套标志位，只是多带 --case。"""
    py = sys.executable
    cmd = [py, "-u", "-m", MODULE[s], "--batch", batch, "--case", case]
    if a.force and s in FORCE_OK:
        cmd += ["--force"]
    if s == "s4" and a.allow_draft:
        cmd += ["--allow-draft"]
    if s == "locate" and a.no_locate_cache:
        cmd += ["--no-cache"]
    if s == "s1" and a.ocr_concurrency >= 0:
        cmd += ["--concurrency", str(a.ocr_concurrency)]
    if s == "locate" and a.probe_concurrency >= 0:
        cmd += ["--probe-concurrency", str(a.probe_concurrency)]
    for extra in a.locate_arg:
        if s == "locate":
            cmd += extra.split()
    if s == "locate" and os.environ.get("R15_RUN_ROOT"):
        # 5 号补丁：locate 的批级产物改写到 scratch（分片仍在 00_index/page_index/），
        # 真正的 00_index/page_index.json 由 merge_page_index() 单写者产出。
        cmd += ["--out", str(Path(os.environ["R15_RUN_ROOT"]) / batch
                             / "00_index" / "_locate_scratch.json")]
    return cmd


def wait_done_marker(batch_dir: Path, case: str, timeout_s: float = 900.0) -> tuple[bool, str]:
    """4.3 同步：s3 只能读**完整写完**的 02_text 产物。

    哨兵 = `02_text/<case>/.done`（s2_preprocess.py:354 在**所有** page_*.json 与
    _manifest.json 都写完之后才落这个标记；那些文件本身走 common.write_json 的
    .tmp + os.replace 原子改名）。`.incomplete` = s2 明确宣告这一例没跑完 → 直接判失败。
    轮询哨兵存在性，不用 sleep 猜固定时长。
    """
    d = batch_dir / "02_text" / case
    t0 = time.time()
    while True:
        if (d / ".incomplete").exists():
            why = ""
            try:
                why = (d / ".incomplete").read_text(encoding="utf-8")[:200]
            except OSError:
                pass
            return False, f"02_text/{case}/.incomplete（s2 未跑完）{why}"
        if (d / ".done").exists() and (d / "_manifest.json").exists():
            return True, f"sentinel 02_text/{case}/.done after {time.time() - t0:.2f}s"
        if time.time() - t0 > timeout_s:
            return False, f"等 02_text/{case}/.done 超时 {timeout_s:.0f}s"
        time.sleep(0.2)


def step_failure_reason(log: Path, limit: int = 160) -> str:
    """从单例日志尾巴取**真因**那一行（[x] / [!] / Error / Exception）。

    退出码只说「非零」，真因只在日志里 ⇒ 不取出来，报告就等于把人带偏。
    PII：身份证/手机号形状先打码再进 JSON（红线 9）。
    """
    try:
        lines = [ln.strip() for ln in
                 log.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
    except OSError:
        return "非零退出（日志读不到）"
    key = [ln for ln in lines if ln.startswith("[x]") or ln.startswith("[!]")
           or "Error" in ln or "Exception" in ln]
    pick = key[-1] if key else (lines[-1] if lines else "")
    pick = re.sub(r"\d{17}[\dXx]", lambda m: m.group(0)[:3] + "*" * 13 + m.group(0)[-2:], pick)
    pick = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", lambda m: m.group(0)[:3] + "****" + m.group(0)[-2:], pick)
    return pick[:limit] or "非零退出（日志为空）"


def pipeline_run(a, cfg, steps: list, src_root: Path) -> tuple[list, int, dict]:
    """按例流水线。返回 (records, rc_all, extra_json)。"""
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    log_dir = Path(a.log_dir) if a.log_dir else batch_dir / "_pipeline_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(log_dir, 0o700)      # 目录里全是带姓名的日志，别给 other 遍历位
    except OSError:
        pass

    from wide import s1_ocr as S1
    rows = S1.read_manifest(batch_dir / "00_index" / "manifest.tsv")
    want = C.wanted_cases(a.case, a.case_file)
    cases = [r["case_id"] for r in rows if not want or r["case_id"] in want]

    lane_a = [s for s in steps if s in LANE_A]
    lane_b = [s for s in steps if s in LANE_B]
    incremental_table = "s6" in steps

    lock = threading.Lock()
    records: list = []
    failed: list = []
    # s6 --incremental 是**批级**步骤：它的 rc 来自批级条件（missing_norm /
    # 跨院区撞车，s6_table.py:714→273），与触发它的那一例无关。以前每例记一条
    # failed ⇒ web-20261017-183205 里 2445/2449 条是同一原因的副本，看板据此把
    # 100% 的例标成 s6 失败。改成按 rc 归并成一条批级记录（case="*"，与
    # s5_final/s6_final 同一约定），并把真因从日志尾巴带进 why。
    batch_fail: dict = {}
    # ── 2a 例级并行（round14）：queue(maxsize=1) → ThreadPoolExecutor(K)
    #    K = R15_CASE_PARALLEL（默认 4）。一个 worker 跑完**一整例**：
    #    lane A 逐步串行 → 等 s2 .done 哨兵 → lane B 逐步串行 → 增量出表。
    #    例内顺序、locate 自适应环、per-case 产物目录都不动。
    case_parallel = max(1, int(os.environ.get("R15_CASE_PARALLEL", "4")))
    s6_lock = threading.Lock()      # 06_table/wide.csv 是批级产物 → 必须串行化
    lane_a_done: list = []          # 每例 lane A 完成时刻 → lane_a_ms
    # ── round45-F：s6 增量出表**按批分流**（s6_lock 是批级串行点）──────────────
    # 每例一次增量 ⇒ 单次 O(N)（重写 wide.csv + 重建 wide.xlsx）⇒ 整批 O(N²)，
    # 且整段被 s6_lock 串行 ⇒ web-20261018-102926 实测 s6 串行 19,569 s = 墙钟 91%
    # （median 5,207 ms/次 × 3,831 次）。收尾本来就有一次全量 s6_final（本文件
    # :421，除非 --no-final-table）⇒ 增量只为「跑批期间宽表可读」，不为终态正确性。
    # 现在攒够 R45_S6_EVERY 例、或距上次出表 ≥ R45_S6_MAXAGE 秒，才真跑一次。
    # 落后窗口（例数）实测写进 extra["s6_throttle"]，不猜。**回滚旧行为**：
    # R45_S6_EVERY=1 R45_S6_MAXAGE=0。红线⑦：不省任何例，收尾汇总照跑。
    s6_every = max(1, int(os.environ.get("R45_S6_EVERY", "200") or 200))
    s6_maxage = float(os.environ.get("R45_S6_MAXAGE", "120") or 120)
    s6_th: dict = {"runs": 0, "since": 0, "last_at": 0.0, "lags": [],
                   "last_case": ""}

    def record(case, step, rc, ms, reqs):
        with lock:
            records.append({"case": case, "step": step, "module": MODULE[step],
                            "rc": rc, "wall_ms": ms, "ocr_requests": reqs})
        print(f"  [{step:7s}] {case} rc={rc} {ms} ms ocr_requests={reqs} "
              f"{time.strftime('%T')}", flush=True)

    def run_one(case: str, s: str) -> tuple[int, int, int | None]:
        log = log_dir / f"{case}__{s}.log"
        before = log_lines(a.ocr_log, a.ocr_log_pattern)
        t0 = time.time()
        with open_private(log) as f:
            rc = subprocess.call(build_cmd(s, a.batch, case, a), cwd=str(src_root),
                                 stdout=f, stderr=subprocess.STDOUT)
        ms = round((time.time() - t0) * 1000)
        after = log_lines(a.ocr_log, a.ocr_log_pattern)
        reqs = after - before if before >= 0 and after >= 0 else None
        record(case, s, rc, ms, reqs)
        return rc, ms, reqs

    def s6_should_run(case: str) -> bool:
        """按批分流的闸门。True = 这次真出表；False = 攒着（落后窗口记账）。
        第一例必出一次（尽早暴露 wide.csv/闸门问题，别让一小时的批跑完才发现表是空的）。"""
        if s6_every <= 1 and s6_maxage <= 0:
            return True                     # 旧行为：每例一次
        now = time.time()
        with lock:
            s6_th["since"] += 1
            s6_th["last_case"] = case
            fire = (s6_th["runs"] == 0
                    or s6_th["since"] >= s6_every
                    or (now - s6_th["last_at"]) >= s6_maxage)
            if fire:
                s6_th["lags"].append(s6_th["since"])
                s6_th["since"] = 0
                s6_th["runs"] += 1
                s6_th["last_at"] = now
        return fire

    def run_s6_incr(case: str) -> None:
        """批级增量出表：并发例必须串行化（同一个 wide.csv / _manifest.json）。"""
        with s6_lock:
            py = sys.executable
            log = log_dir / f"{case}__s6_incr.log"
            t0 = time.time()
            with open_private(log) as f:
                rc = subprocess.call([py, "-u", "-m", MODULE["s6"],
                                      "--batch", a.batch, "--incremental"],
                                     cwd=str(src_root), stdout=f,
                                     stderr=subprocess.STDOUT)
            ms = round((time.time() - t0) * 1000)
            with lock:
                records.append({"case": case, "step": "s6_incr", "module":
                                MODULE["s6"], "rc": rc, "wall_ms": ms,
                                "ocr_requests": None})
            if rc != 0:
                why = step_failure_reason(log)
                with lock:
                    ent = batch_fail.get(rc)
                    if ent is None:
                        ent = {"case": "*", "step": "s6_incr", "rc": rc,
                               "why": why, "count": 0, "first_case_log": str(log)}
                        batch_fail[rc] = ent
                        failed.append(ent)
                    ent["count"] += 1
                    n_ent = ent["count"]
                print(f"  [s6_incr ] rc={rc} ×{n_ent} 批级失败：{why}", flush=True)

    def run_case(case: str) -> None:
        """一个 worker = 一整例。lane A / lane B 内部顺序与产线完全一致。"""
        ok = True
        for s in lane_a:
            rc, _, _ = run_one(case, s)
            if s == "locate" and rc == 0:
                # 5 号补丁：本例 locate 成功 → 立刻把分片并进批级索引，
                # 保证紧随其后的 s1 一定能读到**本例**条目（产线语义：定位成功才可用）。
                n_case, n_shard = merge_page_index(batch_dir, case)
                print(f"  [pi-merge] {case} cases={n_case} shards={n_shard}", flush=True)
            if rc != 0:
                with lock:
                    failed.append({"case": case, "step": s, "rc": rc,
                                   "why": "lane A 非零退出"})
                ok = False
                break
        if not ok:
            return
        with lock:
            lane_a_done.append(time.time())
        # 哨兵 02_text/<case>/.done 只有 s2_preprocess 会落（s2_preprocess.py:354）。
        # 本批 lane_a 里没有 s2（locate-only 这类 bench 跑法）⇒ 等它必然空耗
        # timeout_s=900 s/例，而且失败文案「等 02_text/…/.done 超时」把人推向 s2
        # —— 说谎的报错。真因是本批 steps 不含 s2，不是 s2 没跑完。
        if "s2" not in lane_a:
            print(f"  [sync   ] {case} 跳过哨兵：本批 lane_a={'+'.join(lane_a)} 无 s2，"
                  f"02_text/.done 不会存在（等它 = 每例空耗 900 s）", flush=True)
        else:
            ok, why = wait_done_marker(batch_dir, case)
            if not ok:
                with lock:
                    failed.append({"case": case, "step": "sync", "rc": 9, "why": why})
                C.warn(f"{case}: 不进 lane B —— {why}")
                return
            print(f"  [sync   ] {case} {why}", flush=True)
        for s in lane_b:
            rc, _, _ = run_one(case, s)
            if rc != 0:
                with lock:
                    failed.append({"case": case, "step": s, "rc": rc,
                                   "why": "lane B 非零退出"})
                break
        else:
            if incremental_table and s6_should_run(case):
                run_s6_incr(case)

    t0 = time.time()
    print(f"  [2a     ] case_parallel={case_parallel} (R15_CASE_PARALLEL) "
          f"cases={len(cases)}", flush=True)
    with ThreadPoolExecutor(max_workers=case_parallel,
                            thread_name_prefix="case") as ex:
        futs = {ex.submit(run_case, c): c for c in cases}
        for fu in as_completed(futs):
            exc = fu.exception()
            if exc is not None:
                with lock:
                    failed.append({"case": futs[fu], "step": "worker", "rc": 8,
                                   "why": f"worker 异常 {exc!r}"})
    if incremental_table and a.no_final_table:
        # 对照口径（不跑收尾全量 s6）⇒ 必须补一次增量，否则宽表停在最后一次分流
        run_s6_incr(s6_th["last_case"] or "*")
    if incremental_table:
        lags = s6_th["lags"]
        print(f"  [s6_throttle] every={s6_every} maxage={s6_maxage:.0f}s "
              f"runs={s6_th['runs']} 落后窗口 max={max(lags) if lags else 0} 例 "
              f"/ 收尾未出表 {s6_th['since']} 例", flush=True)
    # 5 号补丁：收尾再合并一次（locate 失败/跳过的例不会自己触发）
    n_case, n_shard = merge_page_index(batch_dir)
    print(f"  [pi-merge] 收尾 cases={n_case} shards={n_shard}", flush=True)
    # 并发下 lane_a_ms 语义变化 = 最后一例 lane A 完成的时刻
    lane_a_ms = round((max(lane_a_done) - t0) * 1000) if lane_a_done else 0
    elapsed = round((time.time() - t0) * 1000)

    # 批次收尾：全量 QC（+ 除非 --no-final-table，再跑一次全量出表）
    tail = []
    if "s5" in steps:
        py = sys.executable
        t1 = time.time()
        rc = subprocess.call([py, "-u", "-m", MODULE["s5"], "--batch", a.batch],
                             cwd=str(src_root))
        tail.append({"case": "*", "step": "s5_final", "module": MODULE["s5"], "rc": rc,
                     "wall_ms": round((time.time() - t1) * 1000), "ocr_requests": None})
        print(f"  [s5_final] rc={rc} {tail[-1]['wall_ms']} ms", flush=True)
    if "s6" in steps and not a.no_final_table:
        py = sys.executable
        t1 = time.time()
        rc = subprocess.call([py, "-u", "-m", MODULE["s6"], "--batch", a.batch],
                             cwd=str(src_root))
        tail.append({"case": "*", "step": "s6_final", "module": MODULE["s6"], "rc": rc,
                     "wall_ms": round((time.time() - t1) * 1000), "ocr_requests": None})
        print(f"  [s6_final] rc={rc} {tail[-1]['wall_ms']} ms", flush=True)
    records.extend(tail)

    tail_bad = [t for t in tail if t["rc"] != 0]
    if tail_bad:
        C.err("批次收尾失败：" + ", ".join(f"{t['step']} rc={t['rc']}" for t in tail_bad)
              + " ⇒ 宽表/QC 可能断链，整批判失败（以前收尾的 rc 不进退出码，看板全绿）")
    rc_all = 1 if (failed or tail_bad) else 0
    by_step: dict = {}
    for r in records:
        by_step.setdefault(r["step"].replace("_final", "").replace("_incr", ""), 0)
        by_step[r["step"].replace("_final", "").replace("_incr", "")] += r["wall_ms"]
    extra = {"pipeline": True, "cases": cases, "failed": failed,
             "lane_a_steps": lane_a, "lane_b_steps": lane_b,
             "pipeline_elapsed_ms": elapsed,
             "case_parallel": case_parallel,
             "lane_a_elapsed_ms": lane_a_ms,
             "pipeline_sum_step_ms": sum(r["wall_ms"] for r in records),
             "pipeline_step_wall_ms": by_step,
             "s6_throttle": {"every": s6_every, "maxage_s": s6_maxage,
                             "runs": s6_th["runs"],
                             "incr_calls": sum(1 for r in records
                                              if r["step"] == "s6_incr"),
                             "lag_cases_max": max(s6_th["lags"]) if s6_th["lags"] else 0,
                             "lag_cases_mean": (round(sum(s6_th["lags"]) / len(s6_th["lags"]), 2)
                                                if s6_th["lags"] else 0),
                             "lag_cases_unflushed": s6_th["since"]},
             "pipeline_log_dir": str(log_dir)}
    return records, rc_all, extra


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="整链跑法 + 逐段计时")
    ap.add_argument("--batch", required=True)
    ap.add_argument("--case", action="append", default=[])
    ap.add_argument("--case-file", default="")
    ap.add_argument("--steps", default=",".join(STEPS),
                    help=f"逗号分隔，默认全跑：{','.join(STEPS)}")
    ap.add_argument("--force", action="store_true", help="各阶段 --force 重跑")
    ap.add_argument("--allow-draft", action="store_true",
                    help="s4 允许 status=draft 的映射表（试跑用；正式回填必须人工确认）")
    ap.add_argument("--no-locate-cache", action="store_true",
                    help="定位阶段强制忽略缓存（测提速时用，避免缓存冒充提速）")
    ap.add_argument("--locate-arg", action="append", default=[],
                    help="额外传给 locate_pages 的参数，可重复（整串按空格拆）")
    ap.add_argument("--ocr-log", default="", help="PaddleX 日志路径，用来数 OCR 请求增量")
    ap.add_argument("--ocr-log-pattern", default="POST /layout-parsing",
                    help="OCR 日志里代表一次请求的行片段；vLLM 通道用 'POST /v1/chat/completions'")
    ap.add_argument("--json-out", default="", help="把逐段耗时写成 JSON 便于报告引用")
    ap.add_argument("--tag", default="", help="记进 JSON 的状态标签，例如 base/①cache")
    ap.add_argument("--pipeline", action="store_true",
                    help="按例流水线：lane A=locate/s1/s2（GPU0），lane B=s3/s4/s5（Strata），"
                         "两 lane 重叠；s3 内部仍逐例串行。s6 每例增量追加")
    ap.add_argument("--no-final-table", action="store_true",
                    help="流水线结束时**不**跑全量 s6（只留增量 wide.csv）—— 用来验收"
                         "「增量 wide.csv == 一次性 wide.csv」")
    ap.add_argument("--log-dir", default="",
                    help="流水线模式下每例每步的日志目录，默认 <batch>/_pipeline_logs")
    ap.add_argument("--ocr-concurrency", type=int,
                    default=int(os.environ.get("R23_OCR_CONCURRENCY", "-1") or -1),
                    help="传给 s1_ocr --concurrency（-1 = 用 pipeline.yaml 默认；"
                         "默认值取 scripts/config.env 的 R23_OCR_CONCURRENCY）")
    ap.add_argument("--probe-concurrency", type=int,
                    default=int(os.environ.get("R23_PROBE_CONCURRENCY", "-1") or -1),
                    help="传给 locate_pages --probe-concurrency（-1 = 用 pipeline.yaml 默认；"
                         "默认值取 scripts/config.env 的 R23_PROBE_CONCURRENCY）")
    a = ap.parse_args(argv)

    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    bad = [s for s in steps if s not in STEPS]
    if bad:
        C.err(f"未知段：{bad}，可选 {list(STEPS)}")
        return 2

    cfg = C.load_pipeline()
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    src_root = Path(__file__).resolve().parent.parent.parent   # .../src

    if a.pipeline:
        records, rc_all, extra = pipeline_run(a, cfg, steps, src_root)
    else:
        records, rc_all, extra = batch_run(a, cfg, steps, src_root)

    out = build_json(a, cfg, steps, records)
    out.update(extra)
    if "locate" in steps:
        add_locate_scan(a, batch_dir, out)
    out["ocr_request_metric"] = (f"primary=grep {a.ocr_log_pattern!r}; "
                                 "cross_check=wc -l whole log (see ocr_wc_delta)")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if a.json_out:
        C.write_json(Path(a.json_out), out)
    return rc_all


def batch_run(a, cfg, steps: list, src_root: Path) -> tuple[list, int, dict]:
    """原整批串行跑法（默认）。返回 (records, rc_all, extra_json)。"""
    py = sys.executable
    case_args: list[str] = []
    for c in a.case:
        case_args += ["--case", c]
    if a.case_file:
        case_args += ["--case-file", a.case_file]
    records, rc_all = [], 0
    for s in steps:
        cmd = [py, "-u", "-m", MODULE[s], "--batch", a.batch] + case_args
        if a.force and s in FORCE_OK:
            cmd += ["--force"]
        if s == "s4" and a.allow_draft:
            cmd += ["--allow-draft"]
        if s == "locate" and a.no_locate_cache:
            cmd += ["--no-cache"]
        for extra in a.locate_arg:
            if s == "locate":
                cmd += extra.split()
        before = log_lines(a.ocr_log, a.ocr_log_pattern)
        wc_before = raw_log_lines(a.ocr_log)
        t0 = time.time()
        print(f"--- {s} ({MODULE[s]}) start {time.strftime('%T')} ---", flush=True)
        print("    $ " + " ".join(cmd), flush=True)
        rc = subprocess.call(cmd, cwd=str(src_root))
        ms = round((time.time() - t0) * 1000)
        after = log_lines(a.ocr_log, a.ocr_log_pattern)
        reqs = after - before if before >= 0 and after >= 0 else None
        wc_after = raw_log_lines(a.ocr_log)
        wc_delta = wc_after - wc_before if wc_before >= 0 and wc_after >= 0 else None
        print(f"--- {s} rc={rc} {ms} ms  ocr_requests={reqs} {time.strftime('%T')} ---",
              flush=True)
        records.append({"step": s, "module": MODULE[s], "rc": rc, "wall_ms": ms,
                        "ocr_requests": reqs, "ocr_log_wc_before": wc_before,
                        "ocr_log_wc_after": wc_after, "ocr_wc_delta": wc_delta})
        rc_all = rc_all or rc

    return records, rc_all, {}


def build_json(a, cfg, steps: list, records: list) -> dict:
    return {"tag": a.tag, "batch": a.batch, "cases": a.case,
            "case_file": a.case_file, "force": a.force,
            "allow_draft": a.allow_draft, "no_locate_cache": a.no_locate_cache,
            "steps": records, "total_wall_ms": sum(r["wall_ms"] for r in records),
            "total_ocr_requests": sum(r["ocr_requests"] or 0 for r in records),
            "total_ocr_wc_delta": sum(r.get("ocr_wc_delta") or 0 for r in records),
            "columns_yaml_version": C.load_columns()["version"],
            "pipeline_yaml_version": cfg.get("version"),
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def add_locate_scan(a, batch_dir: Path, out: dict) -> None:
    """扫页记录：定位阶段的产物里就有「扫了几页/空白跳过几页/命中在哪」，
    一并进 JSON，报告引用时不必再去翻 page_index.json。"""
    idx = batch_dir / "00_index" / "page_index.json"
    if idx.exists():
        if True:
            cases = C.read_json(idx).get("cases", {})
            scan = {}
            for cid in (a.case or list(cases)):
                e = cases.get(cid) or {}
                scan[cid] = {
                    "source_pages": e.get("source_pages"),
                    "pages_scanned": e.get("pages_scanned"),
                    "pages_blank_skipped": e.get("pages_blank_skipped"),
                    "fp_hit_pages": e.get("frontpage_title_pages"),
                    "pn_hit_pages": e.get("first_progress_title_pages"),
                    "locator_source": e.get("locator_source"),
                    "locate_cache": e.get("locate_cache"),
                    "scan_trace": e.get("scan_trace"),
                    "probe": e.get("probe"),
                    "pages_skipped_kind": e.get("pages_skipped_kind"),
                }
            out["locate_scan"] = scan


if __name__ == "__main__":
    raise SystemExit(main())
