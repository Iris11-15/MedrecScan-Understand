# -*- coding: utf-8 -*-
"""F3 批量驱动：把「一份一份跑」改成「一批一起跑」。

**为什么必须批量跑 —— 实测数据：**

    f3_escalate.py 单份跑   74.8 s   completion 吞吐  73 tok/s
    旧整篇基线（8 并发）    122.3 s  completion 吞吐 243 tok/s
                                      ↑ **3.3× 的差距全在并发度上**

单份跑时每份只有 2 次调用、一条生成流，GPU 有 74 秒基本在空转。
vLLM 的连续批处理要靠**同时有多个请求在飞**才吃得满。
按 token 量算，升级只要 42,110 tokens/份 —— 折算成钱不值一提，
**贵的是等待**。

这个脚本做三件事：
    阶段一  逐份跑规则层（0.25 s/份，纯 CPU，不碰模型）
    阶段二  把所有份的升级请求**一起**投给 vLLM（并发 N）
    阶段三  逐份 merge、落盘，并报出真实吞吐

**规模**：本脚本默认按 `F3_CHUNK`（200）例**分块**跑，每块一个子进程。
原因：单份在内存里留 ≈351 KB（段落 265 KB + 请求体 86 KB），
373,233 份就是 ≈125 GB。分块后单块约 70 MB，而每块约 480 个请求
远超 8 个 worker —— **吞吐不变，内存降三个数量级**。`--chunk 0` 关掉。

用法：
    python3 f3_batch.py --cases /home/ubuntu/medrec/jobs/batch_0923_1350/compiled \\
        --outdir /tmp/f3_batch --workers 8 --limit 20
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import f3_escalate as E      # noqa: E402
import f3_fill_policy as fp    # noqa: E402
import route_f3 as R         # noqa: E402

NEED = ("compiled.json", "sections_text.json", "tables.json")


def _piece_secs(sections, piece):
    if not piece:
        return sections
    k, off, ln = piece
    out = dict(sections)
    t = str(sections.get(k) or "")
    out[k] = "[F3 PIECE %d..%d of %d]" % (off, min(off + ln, len(t)), len(t)) + chr(10) + t[off:off + ln]
    return out


def find_cases(root, limit=0):
    """root 下面（含自身）所有同时具备三件套的目录。同一病人去重。"""
    root = Path(root)
    hits, seen = [], set()
    cands = [root] if all((root / n).is_file() for n in NEED) else []
    cands += sorted(p for p in root.rglob("*") if p.is_dir())
    for d in cands:
        if not all((d / n).is_file() for n in NEED):
            continue
        if d.name in seen:
            continue
        seen.add(d.name)
        hits.append(d)
        if limit and len(hits) >= limit:
            break
    return hits


def find_window(root, offset=0, limit=0):
    """★ 分块取第 offset..offset+limit 例（0 = 不限制）。

    为什么要分块：实测单份要在内存里留 ~351 KB（段落 265 KB + 请求体 86 KB），
    **373,233 份 ≈ 125 GB**，一次装下必然 OOM。但并发要的是「同时有足够多请求
    在飞」，不是「一次装下全部病例」：200 份≈480 个请求，8 个 worker 吃不完。
    所以分块不掉吞吐，内存却从 125 GB 降到 ~70 MB。

    这里先多取 offset+limit 例再切掉前 offset 例 —— 因为 find_cases 是
    「凑够 limit 就 break」，直接传 offset 给它没法表达"从第 offset 个开始"。
    顺序由 find_cases 保证稳定（root 自身在前，其余按 sorted 路径），
    所以相邻两块既不重叠也不漏。
    """
    cases = find_cases(root, (limit + offset) if limit else 0)
    return cases[offset:] if offset else cases


def default_chunk():
    """分块大小。默认 200 —— 见 `run_chunks` 里的账。"""
    try:
        return int(os.environ.get("F3_CHUNK", "200"))
    except ValueError:
        return 200


def run_chunks(a, n_total):
    """把 n_total 例拆成每块 ≤`--chunk` 例，各开一个子进程跑。

    ★ 为什么分块必须住在 **f3_batch.py 自己**，而不是调用方：
      `medrec route` 是**直接**调本脚本的（不走 `batch_run.sh`）——
      分块只写在 `batch_run.sh` 里的话，那条路仍然会把全部病例装进内存。
      **内存是在这里分配的，保证就该在这里。**

    ★ 为什么用**子进程**而不是在一个进程里循环：
      单份要在内存里留 ≈351 KB（段落 265 KB + 请求体 86 KB）。
      在本进程里循环的话 `prepared` / `secs` / `jobs` 还是会跨块累积，
      除非把整个 `main()` 重构成「清空 + 重来」，而那种重构更容易出错。
      每块开一个新进程，内存随块结束归还操作系统，是最硬的保证。
      代价只是每块重算一次 6 个文件的指纹（毫秒级）。

    ★ 与 `batch_run.sh` 的外层循环**不冲突**：两边都读 `F3_CHUNK`。
      外层传 `--limit 200` 时这里看到的就是 200 例，
      `len(cases) > chunk` 不成立，不会再拆一层。
    """
    chunk = a.chunk
    if chunk <= 0:                    # 关掉分块 = 一块装下（别在这里除零）
        chunk = max(n_total, 1)
    nchunk = (n_total + chunk - 1) // chunk
    print(f"分块：{n_total} 例 → {nchunk} 块 × ≤{chunk} 例"
          f"（每块独立进程，内存不跨块累积；单块约 {chunk * 351 / 1024:.0f} MB）",
          flush=True)
    base = [sys.executable, str(Path(__file__).resolve()),
            "--cases", str(a.cases), "--outdir", str(a.outdir),
            "--workers", str(a.workers), "--model", a.model,
            "--small-model", a.small_model, "--timeout", str(a.timeout),
            "--chunk", "0"]                       # ★ 0 = 子进程不再自我拆分
    if a.skip_done:
        base.append("--skip-done")
    bad = 0
    for i in range(nchunk):
        off = a.offset + i * chunk
        # ★ 子进程的 `--limit` 必须是**这一块剩下的例数**，不能一律给 `chunk`。
        #   因为 `find_window(root, off, n)` = `find_cases(root, off+n)[off:]` ——
        #   它是从**整个 root**里取，不受父进程 `--limit` 的约束。
        #   最后一块传 `chunk` 就会越界读：实测 `--limit 22 --chunk 5` 时
        #   第 5 块（offset=20）读到了 5 例（第 20..24 例），
        #   而请求只到第 22 例 —— **多算了 3 例，且不报错**。
        left = n_total - i * chunk
        print(f"\n===== 分块 {i + 1}/{nchunk}  offset={off} limit={min(chunk, left)}"
              f" =====", flush=True)
        r = subprocess.run(base + ["--offset", str(off),
                                   "--limit", str(min(chunk, left))],
                           env=dict(os.environ, F3_CHUNK_CHILD="1"))
        if r.returncode != 0:
            bad += 1
            print(f"  ✗ 第 {i + 1} 块退出码 {r.returncode}", flush=True)
    if bad:
        print(f"\n✗ {bad}/{nchunk} 块非零退出 —— 补齐后重跑即可"
              f"（`--skip-done` 幂等，已完成的不会重算）")
        sys.exit(1)
    print(f"\n✓ {nchunk} 块全部完成")


def stage1(case, variables, alias):
    """跑规则层 + 切批。返回 (rule_out, plans) 或 None。"""
    sec = R.expand_sections(
        R.load_sections(str(case / "sections_text.json"),
                        str(case / "compiled.json")), alias)
    tbl = json.loads((case / "tables.json").read_text(encoding="utf-8"))
    out, _ = R.run(sec, tbl, variables, expand=False)
    esc = E.escalatable(sorted(c for c, r in out.items() if r["escalate"]),
                        variables)
    if not esc:
        return out, [], sec, []
    small, big, _did = E.split_for_execution(esc, variables, quiet=True)
    oversize = []
    plans = []
    reqs = []
    # ★ 每一层按**它自己那个模型**的上下文上限切批。
    #   8101 的 Qwen3-8B 是 16384，8102 的 27B 是 32768 ——
    #   两边都按 32768 切会切出 29,724 tokens 的批，送给 8B 就是 400。
    small_over = []
    if small:
        plans += E.plan_batches(small, variables, sec, alias, tier="small",
                                max_len=E.small_max_len(),
                                oversize_out=small_over)
        # ★ 小模型**装不下**的列，改投大模型 —— 不是丢掉。
        #   小模型是「快的那条路」，不是「唯一的路」：16384 装不下的，
        #   32768 装得下。实测这一条值 **75 列**（覆盖率 89.6% → 90.1%）。
        if small_over:
            big = list(big) + small_over
    if big:
        plans += E.plan_batches(big, variables, sec, alias, tier="big",
                                oversize_out=oversize)
    for b in plans:
        schema, system, user, info = E.build_request(
            b["cols"], variables, _piece_secs(sec, b.get("piece")), alias, keys=b["keys"],
            per_section_cap=1 << 30, strict=not b.get("partial"),
            max_len=E.small_max_len() if b.get("tier") == "small" else None)
        reqs.append({"schema": schema, "system": system, "user": user,
                     "info": info, "max_tokens": b["max_tokens"],
                     "cols": b["cols"], "tier": b.get("tier", "big")})
    return out, reqs, sec, oversize


WHITESPACE = " \t\r\n"


def code_fingerprint():
    """代码指纹 —— 从磁盘上这几个文件的 sha256 前 8 位算出来。

    ★ 为什么需要它：**「包是最新的」不能靠假设。** 这一路已经踩过两次：
        · `test_escalate.py` 报 12/12 而不是 13/13（服务器上是旧包）
        · `F3_SPLIT=off` 不生效，因为 `f3_escalate.py` 是旧的
      两次都是「以为传上去了，其实没有」。

    **指纹从文件本身算，不用单独的 VERSION 文件** ——
    那样文件永远不会过期，也不会有「VERSION 说 A、代码是 B」的情况。
    每次跑批在日志开头打出来，就不用再猜服务器上是哪一版。
    """
    h = hashlib.sha256()
    # ★ 覆盖**所有能影响答案的文件**，不只是主脚本。
    #   原来只算 route_f3 + f3_escalate + f3_batch + variables_f3 ——
    #   而 `f3_prompt_gen.py` 决定 schema 和 system 提示词（**直接改模型答案**），
    #   `f4_columns.json` 决定宽表列顺序。少算它们，就会出现
    #   「指纹说是同一版、实际行为已经变了」——**那比没有指纹更坏**。
    for f in ("route_f3.py", "f3_escalate.py", "f3_batch.py",
              "f3_prompt_gen.py", "variables_f3.json", "f4_columns.json"):
        p = HERE / f
        h.update(p.read_bytes() if p.is_file() else b"<missing>")
    return h.hexdigest()[:8]


def looks_degenerate(content):
    """输出退化成一片空白 —— 8B 在 guided decoding 下的已知失效模式。

    实测 5/98 个请求：写完第一个字段（甚至 6 个字段）之后就开始狂吐换行，
    一直吐到 `max_tokens`。`finish_reason` 于是报 `length`，
    **看着像"被截断"，其实是退化** —— 调大 max_tokens 只会让它吐更多空行。

    判据：一半以上是空白字符。正常的 JSON 输出空白占比不到三成。
    """
    s = str(content or "")
    if len(s.strip()) < 20:
        return True
    ws = sum(1 for c in s if c in WHITESPACE)
    return ws / max(len(s), 1) > 0.5


def post(url, body, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=dict({"Content-Type": "application/json"}, **E._auth_hdr()))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, help="含病例子目录的根目录")
    ap.add_argument("--outdir", default="/tmp/f3_batch")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 例")
    # ★ 分块：一次只把 N 例装进内存。
    #   实测单份要在内存里留 ~351 KB（段落 265 KB + 请求体 86 KB）——
    #   **373,233 份就是 ~125 GB**，一次装下根本跑不动。
    #   但并发的需求是「同一时刻有足够多请求在飞」，不是「一次装下所有病例」：
    #   200 份就有约 480 个请求，远超 8 个 worker —— **分块不影响吞吐**。
    ap.add_argument("--offset", type=int, default=0,
                    help="跳过前 N 例（配合 --limit 分块跑，控制内存）")
    ap.add_argument("--chunk", type=int, default=default_chunk(),
                    help="每块最多 N 例，超出就拆成多个子进程跑（0 = 不分块）。"
                         "默认读 F3_CHUNK，再退到 200")
    ap.add_argument("--skip-done", action="store_true",
                    help="跳过 outdir 里已经有结果的病例（续跑用）")
    ap.add_argument("--model", default=os.environ.get("QWEN_MODEL", "qwen-27b"))
    ap.add_argument("--small-model", default=os.environ.get("QWEN_SMALL_MODEL",
                                                            "qwen-small"))
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # ★ 动手之前先记下 outdir 里原有的 *.json。
    #   实测教训：`/tmp/f3b` 里有旧版本留下的结果（`state:"模型"`），
    #   于是每个分块都报「非法值 ['模型']」，看着像本次的 bug，其实是残留。
    pre_existing = sorted(p.name for p in outdir.glob("*.json"))
    # ★ 但**分块的子进程不算残留**：兄弟块写进同一个 outdir 是设计如此
    #   （否则每块都会喊「outdir 里有 N 个不是本次写的」，实测用户已清过目录
    #   却被警告 4 次）。只有最外层那个进程才该提醒。
    in_chunk = os.environ.get("F3_CHUNK_CHILD") == "1"
    variables = json.loads((HERE / "variables_f3.json").read_text(encoding="utf-8"))
    alias = R.load_alias()

    # ★ 日志第一行就报代码指纹 —— 「服务器上是哪一版」不再靠猜。
    print(f"== F3 batch  代码指纹 {code_fingerprint()}  "
          f"F3_SPLIT={E.split_policy()}")
    cases = find_window(a.cases, a.offset, a.limit)
    print(f"找到 {len(cases)} 例（同病人已去重）"
          + (f"  [offset={a.offset} limit={a.limit or '∞'}]"
             if a.offset or a.limit else ""))
    if not cases:
        return
    # ★ 内部分块：必须在 `--skip-done` **之前**拦。
    #   子进程的 `--offset` 是相对**未过滤**列表的下标，父进程先按 skip-done
    #   过滤再算 offset 就会错位（跳过一例，或重复跑一例，都不报错）。
    if a.chunk and len(cases) > a.chunk:
        run_chunks(a, len(cases))
        return
    # ★ 续跑：outdir 里已经有 <名字>.json 的病例直接跳过。
    #   原 batch_run.sh 靠 `structured.json 存在` 判断完成 —— 换了 F3 之后
    #   那个标记不再产出，续跑判据必须跟着换，否则每次都会从头重跑。
    if a.skip_done:
        done = {p.stem for p in Path(a.outdir).glob("*.json")}
        before = len(cases)
        cases = [c for c in cases if c.name not in done]
        print(f"  续跑：跳过已完成的 {before - len(cases)} 例，剩 {len(cases)} 例")
        if not cases:
            print("全都跑过了，收工")
            return

    # ── 阶段一：规则层 + 切批（CPU，很便宜）────────────────────────
    t0 = time.time()
    prepared, jobs, secs = [], [], {}
    n_rule = n_esc = n_over = 0
    for i, case in enumerate(cases):
        try:
            rule_out, reqs, sec, oversize = stage1(case, variables, alias)
        except SystemExit as ex:
            print(f"  [跳过] {case.name}: {str(ex)[:110]}", flush=True)
            continue
        except Exception as ex:                                # noqa: BLE001
            print(f"  [跳过] {case.name}: {type(ex).__name__}: {str(ex)[:110]}",
                  flush=True)
            continue
        prepared.append((case, rule_out))

        # [PB-FIX5] 没有升级请求的病例也要落盘，否则整例静默消失
        if not reqs:
            try:
                final, st = E.merge(rule_out, {}, sec, variables)
                # R3/R4（2026-10-07 临床需求）：出血转化 / 出血性梗死 不算脑出血。
                # 判定逻辑在 f3_fill_policy.apply_exclusions，这里只接线。
                vetoed = fp.apply_exclusions(final, sec)
                st["excluded"] = len(vetoed)
                if vetoed:
                    print(f"    [排除规则] {case.name[:22]} 阳性被推翻: {vetoed}",
                          flush=True)
                # R1（2026-10-07）：病史逐条标来源（出院诊断/出院其他诊断/既往诊断）。
                # 拆分逻辑在 route_f3.attribute_history_sources，这里只接线。
                R.attribute_history_sources(final)
                (outdir / f"{case.name}.json").write_text(
                    json.dumps({"items": final, "stats": st,
                                "case": str(case)},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8")
                print(f"    （无升级请求，已直接落盘 {case.name[:22]}）", flush=True)
            except Exception as _e:
                print(f"    !! 无请求例落盘失败 {case.name[:22]}: {_e}", flush=True)
            continue
        secs[len(prepared) - 1] = sec
        n_rule += len(rule_out)
        n_esc += sum(len(r["cols"]) for r in reqs)
        n_over += len(oversize)
        for j, r in enumerate(reqs):
            jobs.append((len(prepared) - 1, j, r))
    t1 = time.time()
    print(f"阶段一（规则层+切批）{t1-t0:.1f} s：{len(prepared)} 例，"
          f"{n_rule} 列，需升级 {n_esc} 列 → {len(jobs)} 个请求", flush=True)
    if n_over:
        print(f"  ⚠️ 另有 {n_over} 列**单批装不下**（依赖的段合计超过单批容量），"
              f"本轮不给模型，落待确认。硬塞会丢掉部分来源段 —— 宁可留空。",
              flush=True)

    if not jobs:
        print("没有需要升级的列，收工")
        return

    # 小模型端点活着吗？**活才分层** —— 分段会各发一遍（跨模型去重做不到），
    # 实测 +64% tokens。端点不在就退回大模型，既不用付这份开销也不会丢数据。
    small_url = E._small_chat_url()
    small_ok = E.endpoint_alive(small_url)
    url = E._chat_url()
    if not small_ok:
        print(f"  · 小模型端点 {small_url} 无响应 → **不分层**（避免 +64% 白开销），"
              f"全部走 {a.model}", flush=True)
    results = {}
    tok_p = tok_c = 0
    n_fail = 0
    n_retry = 0          # 小模型层退化 → 大模型救回来的批数
    t2 = time.time()
    n_small = sum(1 for _c, _j, r in jobs if r.get("tier") == "small")
    print(f"阶段二：{len(jobs)} 个请求 × {a.workers} 并发"
          f"（小模型层 {n_small} 个 → "
          f"{a.small_model if small_ok else a.model}，"
          f"大模型层 {len(jobs)-n_small} 个 → {a.model}）", flush=True)
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        fut = {}
        for idx, (ci, j, r) in enumerate(jobs):
            use_small = r.get("tier") == "small" and small_ok
            # ★ `max_tokens` 不能超过**剩余上下文**：vLLM 的约束是
            #   `prompt_len + max_tokens <= max_model_len`。
            #   小模型层 prompt 最高 13,144，而 max_model_len 只有 16,384 ——
            #   按字段数给的 max_tokens（几千）会把它顶出去，输出被截成半截 JSON。
            #   实测 4/98 个请求就是这么挂的（报错位置全卡在 char≈1046）。
            lim = r["info"]["max_model_len"]
            room = lim - r["info"]["est_tokens"] - 64
            max_tok = max(256, min(r["max_tokens"], room))
            body = {"model": a.small_model if use_small else a.model,
                    "messages": [{"role": "system", "content": r["system"]},
                                 {"role": "user", "content": r["user"]}],
                    "temperature": 0.0, "max_tokens": max_tok,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "response_format": {"type": "json_schema",
                                        "json_schema": {"name": "f3_fields",
                                                        "strict": True,
                                                        "schema": r["schema"]}}}
            fut[ex.submit(post, small_url if use_small else url, body,
                          a.timeout)] = (ci, j, r, max_tok, body, use_small)
        done = 0
        # ★ 每完成一个请求就刷一次进度，而且**完成一例就落盘一例**。
        #   原来只在 done%10==0 时打印、最后才统一落盘 —— 配上 `| tail`
        #   就变成"跑十分钟一个字都不出"，看起来像卡死（用户实际遇到过一次）。
        left = {}
        for ci, _j, _r in jobs:
            left[ci] = left.get(ci, 0) + 1
        tot = {"rule": 0, "model": 0, "neg": 0, "derived": 0, "pending": 0,
               "excluded": 0}
        for f in as_completed(fut):
            ci, j, r, max_tok, body0, was_small = fut[f]
            done += 1
            d = None
            content = ""
            retry_saved = False
            try:
                d = f.result()
                ch = (d.get("choices") or [{}])[0]
                content = str((ch.get("message") or {}).get("content") or "")
                if looks_degenerate(content):
                    raise ValueError(f"输出退化（{len(content)} 字符里过半是空白）")
                try:
                    ans = json.loads(content)
                except Exception as ex_js:
                    # ★ 被截断时先救回已经完整的条目，别让前半批真值陪葬。
                    _fr0 = ((d or {}).get("choices") or [{}])[0].get("finish_reason")
                    _sal, _ok = _repair_truncated(content)
                    if _ok and _sal is not None:
                        ans = _sal
                        _u0 = d.get("usage") or {}
                        tok_p += _u0.get("prompt_tokens", 0)
                        tok_c += _u0.get("completion_tokens", 0)
                        print("  [截断抢救] 例%s 批%s: finish_reason=%s completion=%s"
                              " -> 救回 %d/%d 条（其余按待确认）"
                              % (ci, j, _fr0, _u0.get("completion_tokens"),
                                 len(_sal), len(r.get("cols") or [])))
                    else:
                        raise ex_js
                u = d.get("usage") or {}
                tok_p += u.get("prompt_tokens", 0)
                tok_c += u.get("completion_tokens", 0)
            except Exception as ex2:                           # noqa: BLE001
                ans = {}
                # ★ 小模型层出错 → **立刻用大模型重试一次**。
                #   分层的前提是「快的那层出错时不能丢数据」，
                #   而不是"希望它别出错"。实测 8B 有 5/98 的退化率，
                #   这 5 批的列若不救，就会静默变成待确认。
                if was_small and small_ok and d is not None:
                    try:
                        b2 = dict(body0)
                        b2["model"] = a.model
                        d2 = post(url, b2, a.timeout)
                        c2 = str(((d2.get("choices") or [{}])[0]
                                  .get("message") or {}).get("content") or "")
                        if not looks_degenerate(c2):
                            ans = json.loads(c2)
                            u2 = d2.get("usage") or {}
                            tok_p += u2.get("prompt_tokens", 0)
                            tok_c += u2.get("completion_tokens", 0)
                            retry_saved = True
                            n_retry += 1
                    except Exception:                          # noqa: BLE001
                        pass
                if not retry_saved:
                    n_fail += 1
                    # ★ 把**原始输出**留下来。只报 `JSONDecodeError` 是查不动的：
                    #   得看到底是模型写错、被截、还是退化成了一堆换行。
                    #   实测 5/98 就是靠它定性的（`length` + 1046 字符里约 1000 个空行），
                    #   **和"max_tokens 超了剩余上下文"完全不是一回事**。
                    why = ""
                    try:
                        ch = (d or {}).get("choices") or [{}]
                        fr = ch[0].get("finish_reason")
                        u = (d or {}).get("usage") or {}
                        why = (f"finish_reason={fr} prompt={u.get('prompt_tokens')} "
                               f"completion={u.get('completion_tokens')} "
                               f"内容 {len(content)} 字符")
                        fdir = Path(a.outdir) / "_failed"
                        fdir.mkdir(parents=True, exist_ok=True)
                        (fdir / f"c{ci}_b{j}.txt").write_text(
                            f"# {why}\n# max_tokens={max_tok} "
                            f"max_model_len={r['info']['max_model_len']}\n"
                            f"{'-' * 60}\n{content}", encoding="utf-8")
                    except Exception:                          # noqa: BLE001
                        pass
                    print(f"  [请求失败] 例{ci} 批{j}: {type(ex2).__name__}: "
                          f"{str(ex2)[:70]}  {why}", flush=True)
            results.setdefault(ci, {}).update(ans)

            # 这一例的请求全回来了 → 立刻 merge 落盘（不用等整批）
            left[ci] -= 1
            if left[ci] == 0:
                case, rule_out = prepared[ci]
                final, st = E.merge(rule_out, results.get(ci, {}),
                                    secs.get(ci), variables)
                # R3/R4（2026-10-07 临床需求）：出血转化 / 出血性梗死 不算脑出血。
                vetoed = fp.apply_exclusions(final, secs.get(ci))
                st["excluded"] = len(vetoed)
                if vetoed:
                    print(f"      [排除规则] {case.name[:22]} 阳性被推翻: {vetoed}",
                          flush=True)
                # R1（2026-10-07）：病史逐条标来源。逻辑在 route_f3，这里只接线。
                R.attribute_history_sources(final)
                for k in tot:
                    tot[k] += st[k]
                (outdir / f"{case.name}.json").write_text(
                    json.dumps({"items": final, "stats": st, "case": str(case)},
                               ensure_ascii=False, indent=2), encoding="utf-8")
                el = time.time() - t2
                # ★ `st` 里除了各项计数还有一个 `n` —— 用 sum(st.values()) 会把列数算成两倍。
                ncols = st["n"]
                print(f"    {done}/{len(jobs)} 请求  {el:5.0f} s  "
                      f"{(tok_p+tok_c)/max(el,1e-9):5.0f} tok/s  "
                      f"完成 {case.name[:22]}（待确认 {st['pending']}/{ncols}）",
                      flush=True)
    t3 = time.time()

    n = tot["rule"] + tot["model"] + tot["neg"] + tot["derived"] + tot["pending"]
    # ★ 落盘的状态必须是**三态之一**。`来源层` 才是出处。
    #   曾经这里写成 `"模型"`，下游按 阳性/阴性/待确认 消费时会静默漏掉整批列。
    #
    # ★ 只扫**本次真正写出的**那些文件 —— 原来扫 outdir 下所有 `*.json`，
    #   于是上一版留下的旧结果会被算到本次头上（实测 `/tmp/f3b` 就是）。
    #   残留文件本身仍然值得提醒，所以下面单独报一次。
    bad_states = set()
    wrote = 0
    # ★ 「来源段整段缺失」按**缺的是哪些段**聚合。实测这一条占全部待确认的 45%
    #   （106 例 / 498 列里 4980 格），而它到底该「补 section_alias 别名」还是
    #   「承认文书就没这一页」，从总数上看不出来 —— 只有把段名列出来才能办事。
    sec_miss = {}
    for case, _rule_out in prepared:
        f = Path(outdir) / f"{case.name}.json"
        if not f.is_file():
            continue
        wrote += 1
        try:
            for r in (json.loads(f.read_text(encoding="utf-8"))
                      .get("items") or {}).values():
                if r.get("state") not in ("阳性", "阴性", "待确认"):
                    bad_states.add(str(r.get("state")))
                if r.get("state") == "待确认":
                    why = str((r.get("证据") or {}).get("why") or "")
                    if "来源段整段缺失" in why:
                        m = re.search(r"来源段整段缺失（缺 ([^）]*)）", why)
                        key = m.group(1) if m else "(未标注段名)"
                        sec_miss[key] = sec_miss.get(key, 0) + 1
        except Exception:                                      # noqa: BLE001
            pass
    wall = t3 - t0
    esc_wall = t3 - t2
    print()
    print("=" * 66)
    print(f"完成 {len(prepared)} 例 / {n} 列，总墙钟 {wall:.1f} s")
    print(f"  token   prompt {tok_p:,}  completion {tok_c:,}  合计 {tok_p+tok_c:,}")
    print(f"  吞吐    合计 {(tok_p+tok_c)/max(esc_wall,1e-9):.0f} tok/s   "
          f"completion {tok_c/max(esc_wall,1e-9):.0f} tok/s")
    print(f"  merge   规则层 {tot['rule']}  模型 {tot['model']}  "
          f"阴性(模型/缺失语义) {tot['neg']}  派生重算 {tot['derived']}")
    print(f"  排除规则 R3/R4 推翻的阳性 {tot['excluded']} 列"
          f"（出血转化 / 出血性梗死 不算脑出血）")
    print(f"  仍未定  {tot['pending']}（{tot['pending']/max(n,1):.1%}）"
          f"   覆盖率 {(n-tot['pending'])/max(n,1):.1%}")
    if sec_miss:
        tm = sum(sec_miss.values())
        print(f"  ⚠️ {tm} 格（{tm/max(n,1):.1%}）是**来源段整段缺失**。"
              f"下面按「缺的是哪些段」聚合：")
        for k, v in sorted(sec_miss.items(), key=lambda kv: -kv[1])[:12]:
            print(f"       {v:6d} 格   缺 {k}")
        print("       ⚠️ 段缺失有**两种**成因、修法相反，靠下面的产出率区分："
              "「段没被切出来」→ 补 section_alias 别名；"
              "「段里本来就没字」→ 修 OCR。别默认是前者。")
    # ---- OCR 文字产出率 ----------------------------------------------------
    #   为什么放在这儿：段缺失的两种成因里，「段里本来就没字」只能靠这个数字看出来。
    #   实测那份真实文书（宁波二院）：11 个无文字块，**光病案首页 p1 就占 3 个**，
    #   于是首页字段连标签都没有 —— 26 个首页列（安吉 1911 格）全空，而补别名救不了。
    #   （原来这段话写在「缺哪些段」的标题里，直接断言是别名问题，是错的。）
    try:
        import f3_ocr_yield as _Y
        _rows, _fails = _Y.audit_many([c for c, _ in prepared])
        if _rows:
            _lines, _stat = _Y.summarize(_rows)
            for _ln in _lines:
                print("  " + _ln)
            if _stat.get("p1"):
                print("       → 这属于 OCR 层（不是规则层）："
                      "先开 MEDREC_OCR_ORIENT=1 + server 版模型，"
                      "详见包里的 填充率修复 文档 §10")
        for _p, _why in _fails[:3]:
            print(f"       （产出率跳过 {_p}：{_why}）")
    except Exception as _e:                                    # noqa: BLE001
        print(f"  （OCR 产出率审计不可用：{type(_e).__name__}: {_e}）")
    if n_fail:
        print(f"  ⚠️ **{n_fail}/{len(jobs)} 个请求失败** —— 这些批的列全落在待确认里，"
              f"不要当成「模型说原文没有」")
    if n_retry:
        print(f"  · 小模型层有 {n_retry} 个批**输出退化成空白**，已用大模型重试救回"
              f"（这是 8B 在 guided decoding 下的失效模式，不是数据问题）")
    if bad_states:
        print(f"  ✗ **落盘状态里出现非法值 {sorted(bad_states)}** —— "
              f"三态只能是 阳性/阴性/待确认，出处看「来源层」")
    if pre_existing and not in_chunk:
        print(f"  · outdir 里本来就有 {len(pre_existing)} 个 *.json（不是本次写的）"
              f"，本次写出 {wrote} 个。做 A/B 对比时**用干净目录** —— "
              f"否则比的是混合结果。")
    per = esc_wall / max(len(prepared), 1)
    print()
    print(f"  每例升级墙钟 {per:.1f} s（{a.workers} 并发）")
    print(f"  → 373,233 份外推 {373233*per/3600:,.0f} h")
    print("=" * 66)
    if n_fail or bad_states:
        sys.exit(1)


if __name__ == "__main__":
    main()


# ===== 截断抢救（B 补丁）=====
# 输出是 [{列1},{列2},...]。被截断时**只收成对完整的条目**：按 `},{` 切开，
# 首尾各补一个花括号再各自 json.loads，两端都成功才算数。
# 绝不做 rstrip("}") 那种修法 —— 那会把最后一个半截对象当成合法值用，是假数据。
def _next_obj(s):
    """返回 (对象文本, 结束位置)。字符串内的花括号不算层级。"""
    i, depth, in_str, esc, n = 0, 0, False, False, len(s)
    while i < n:
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[:i + 1], i + 1
        i += 1
    return None, i


def _repair_truncated(content, max_n=400):
    """从被截断的 JSON 数组里救出已完整的对象。返回 (列表, 是否成功)。"""
    s = (content or "").strip()
    if not s.startswith("["):
        return None, False
    body = s[1:]
    out, k = [], 0
    while True:
        seg = body[k:]
        p = seg.find("{")
        if p < 0:
            break
        obj, end = _next_obj(seg[p:])
        if not obj:
            break
        try:
            out.append(json.loads(obj))
        except Exception:
            break
        k += p + end
        if len(out) > max_n:
            break
    return (out, True) if out else (None, False)
