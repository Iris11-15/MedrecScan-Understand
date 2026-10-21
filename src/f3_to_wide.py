#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 f3_batch 的逐份结果拼成**宽表** —— 也就是登记表真正的交付形态。

为什么需要它：
    `f3_batch.py` 每份病历出一个 JSON（`{items: {列: {值, state, 来源层, 证据}}}`）。
    373,233 份就是 373,233 个 JSON —— **那不是登记表，那是中间产物**。
    登记表是一张宽表：一行一个病人，498 列。

    列顺序和 `f4_table.py` 的 Sheet1 保持一致（`f4_columns.json` 的 `flat`），
    这样 F3 的产物能**直接替换** F4 那条老链路，下游不用改。

输出（每个 sheet 都有意义）：
    Sheet1    1 行 1 人：住院号 + 姓名 + 498 列的值。给程序读。
    三态统计  每列在整批上的 阳性/阴性/待确认 计数。给质量把关用。
    溯源      长表：每格一行（患者/列名/状态/来源层/值/依据）。给核对用。
    异常清单  ★ 4 张表：文件对账 / 高空值行 / 零命中列 / 口径。给**办事**用。
    取值说明  本批的渲染规则（阴性各类型渲染成什么）。给下游对账用。

★ 2026-10-03 的修复（详见 f3_fill_policy.py 的模块头）：
    原写法 `str((items.get(c) or {}).get("值") or "")` 只写「值」，
    而阴性/待确认的「值」是 None → 落成空串 → **40,000 格空格里 99.7%
    其实是有答案或有状态的**。现在按 state 分别渲染，并加了写盘门禁。
"""
import argparse
import collections
import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import f3_fill_policy as fp
except ImportError:                                          # noqa: BLE001
    print("[x] 缺 f3_fill_policy.py —— 渲染规则在它里面，不能没有。"
          "把它和 f3_to_wide.py 放在同一目录。")
    sys.exit(2)

# R1 的条目切分口径只有一份实现（route_f3.split_history_entries）。
# 在这里另写一个正则 = 拆分前后对不上账的根源，所以宁可缺了就说缺。
try:
    import route_f3 as R3
except ImportError:                                          # noqa: BLE001
    R3 = None

# ── R1 病史来源列（2026-10-07 临床需求）────────────────────────────
# ★ 只**追加**在 498 列之后；既有 498 列的填充与判定逻辑一行没改。
#   条目与来源由 route_f3.attribute_history_sources() 写进
#   `证据["来源条目"]`；这里只负责把它摊成格子。
#   名字与 route_f3.HISTORY_SOURCE_COLS 一一对应，改一边必须改另一边。
HISTORY_SRC_COLS = (
    ("病史来源-出院诊断", "出院主要诊断"),
    ("病史来源-出院其他诊断", "出院其它诊断"),
    ("病史来源-既往诊断", "既往其他病史"),
)
HISTORY_SRC_NAMES = tuple(c for c, _o in HISTORY_SRC_COLS)


def load_flat(path):
    """f4_columns.json 的 flat = 498 列的**权威顺序**"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(d, dict) and isinstance(d.get("flat"), list):
        return [str(x) for x in d["flat"]]
    if isinstance(d, list):
        return [str(x) for x in d]
    raise SystemExit(f"认不出 {path} 里的列清单")


# NIHSS 总分可能叫这些名字。**这是一个真实的未知**：F6 的 `arbiter.py` 不在
# 这个工作区里，我看不到它的输出 schema —— 所以不猜一个名字，而是多路兜底 +
# 兜底失败时**明确报出来**（宁可说"读不到"，也不要静默填 0）。
NIHSS_TOTAL_KEYS = ("total_final", "total_laya", "total", "NIHSS", "nihss", "总分")


def load_nihss(indir, total_key=None):
    """读 F6 的结果目录 → {病例名: {"total","src","esc"}}。

    总分取法（按顺序）：
      ① `--nihss-total-key` 指定的字段
      ② `NIHSS_TOTAL_KEYS` 里第一个是数字的字段
      ③ 兜底：把 `items` 里**每一项**的 `level` 相加 —— 只要每一项都有 level
         （缺一项就不加，免得把「部分和」当成总分）
    """
    keys = ([total_key] if total_key else []) + list(NIHSS_TOTAL_KEYS)
    out = {}
    for p in sorted(Path(indir).glob("*.json")):
        if p.name.startswith("_"):
            continue
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            continue
        if not isinstance(j, dict):
            continue
        name = p.stem
        for suf in (".final", ".nihss", ".f6", ".score"):
            if name.endswith(suf):
                name = name[: -len(suf)]
                break
        total = None
        for k in keys:
            v = j.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total = int(v)
                break
        items = j.get("items") if isinstance(j.get("items"), dict) else None
        esc = src = None
        if items:
            lv = [(r or {}).get("level") for r in items.values()]
            if total is None and lv and all(isinstance(x, (int, float)) for x in lv):
                total = int(sum(lv))
            esc = sum(1 for r in items.values()
                      if "qwen" in str((r or {}).get("final_source") or "").lower())
            seen = {(r or {}).get("final_source") for r in items.values()}
            seen.discard(None)
            src = "/".join(sorted(str(s) for s in seen)) or None
        if total is None:
            continue
        out[name] = {"total": total, "src": src, "esc": esc}
    return out


def load_manifest(path):
    """返回 (src_of: {name: (dir, file)}, names: [按文件顺序的病例名])。

    ★ names 是**对账的关键**：每个 PDF = 一例，manifest 里每一行就是一个输入
      文件。少了哪一份病历，靠 names 减结果集合就能点名 —— 这比只知道
      「106 变 104」有用得多。
    """
    src_of, names = {}, []
    p = Path(path)
    if not p.is_file():
        return src_of, names
    with p.open(encoding="utf-8") as fh:
        head = fh.readline().rstrip("\n").split("\t")
        ix = {k: head.index(k) for k in ("name", "dir", "file") if k in head}
        if len(ix) != 3:
            return src_of, names
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                n = parts[ix["name"]]
                src_of[n] = (parts[ix["dir"]], parts[ix["file"]])
                names.append(n)
    return src_of, names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--indir", required=True, help="f3_batch.py 的输出目录")
    ap.add_argument("--out", required=True)
    ap.add_argument("--columns", default=str(HERE / "f4_columns.json"),
                    help="列顺序的权威来源（默认 f4_columns.json 的 flat）")
    ap.add_argument("--no-trace", action="store_true",
                    help="不写「溯源」长表（几万份时会很大）")
    ap.add_argument("--manifest", default=None,
                    help="病例名 → 来源目录/输入文件 的对照表（batch_run.sh 会写，"
                         "默认找 <indir>/../input_manifest.tsv）")
    # ── NIHSS 评分列（F6 的结果）─────────────────────────────────────
    # ★ 默认**总是**出「NIHSS评分」这一列，哪怕没给 --nihss（那时为空）。
    #   理由和「来源目录」一样：列数不能随调用方式变 —— 下游按「第 N 列」
    #   读表的代码一旦错位是静默的，而多一个空列只是少看一列。
    ap.add_argument("--nihss", default=None,
                    help="F6 的结果目录（每例一个 JSON）。给了就把总分填进 "
                         "「NIHSS评分」列；按**病例名**对齐")
    ap.add_argument("--nihss-total-key", default=None,
                    help="总分字段名（默认依次试 total_final/total_laya/"
                         "total/NIHSS/总分，再退到把逐题 level 相加）")
    ap.add_argument("--nihss-detail", action="store_true",
                    help="额外加「NIHSS来源」「NIHSS升级项」两列（默认只加一列）")
    # ── 渲染与门禁（2026-10-03 新增）─────────────────────────────────
    ap.add_argument("--fill-mode", choices=("filled", "compat"), default="filled",
                    help="filled（默认）：阴性→否/未做/未查/未用/不适用，待确认→"
                         "「待确认」，**绝不写空**。compat：旧行为，只写值")
    ap.add_argument("--no-gate", action="store_true",
                    help="跳过写盘门禁（**不推荐**：门禁拦的就是 106→104、"
                         "整行 99.4%% 空这类静默失败）")
    ap.add_argument("--no-abnormal", action="store_true",
                    help="不写「异常清单」sheet")
    ap.add_argument("--expect", type=int, default=None,
                    help="期望的病例数（没有 manifest 时用它对账；每个 PDF = 一例）")
    a = ap.parse_args()

    flat = load_flat(a.columns)
    files = sorted(glob.glob(str(Path(a.indir) / "*.json")))
    if not files:
        print(f"没找到结果文件：{a.indir}/*.json")
        sys.exit(1)
    print(f"列顺序来源 {a.columns}：{len(flat)} 列")
    print(f"渲染模式 --fill-mode={a.fill_mode}"
          + ("（旧行为：只写「值」，阴性/待确认落成空串）" if a.fill_mode == "compat"
             else "（阴性按类型渲染，待确认照写「待确认」，不写空）"))

    # ── 来源目录 / 输入文件 ──────────────────────────────────────────
    # ★ 为什么不从病例名反解：名字是把路径里的 `/` 换成了 `__`，
    #   而真名里本来就可能含 `__` —— 反解有歧义。所以走对照表。
    #   表不存在时仍然出这两列（空值），保证列数稳定，不让下游的
    #   「第 N 列是什么」随批次变。
    mani = Path(a.manifest) if a.manifest \
        else Path(a.indir).parent / "input_manifest.tsv"
    src_of, manifest_names = load_manifest(mani)
    if src_of:
        print(f"来源目录对照表 {mani}：{len(src_of)} 条")
        # ★ 对账：表指错了会**静默**把每行贴上别人的医院名 —— 这比空白糟得多。
        #   （实测容易踩：`--indir` 指到别处，而它的上一级恰好躺着另一批的对照表。）
        hit = sum(1 for f in files if Path(f).stem in src_of)
        if hit == 0:
            print(f"  ⚠️ **一条都没对上**（0/{len(files)} 例）—— 这张表多半不是"
                  f"这批的。每批的 input_manifest.tsv 在自己那个 $WORK 下。"
                  f"来源目录列会被留空，不做错误填充。")
            src_of, manifest_names = {}, []
        elif hit < len(files):
            print(f"  ⚠️ 只对上了 {hit}/{len(files)} 例 —— 剩下那几行的"
                  f"「来源目录」会是空的（不猜）。")
    else:
        print(f"⚠️ 没有来源目录对照表（{mani}）—— 「来源目录/输入文件」两列会是空的。"
              f"用 batch_run.sh 跑就会自动生成；手工跑可用 --manifest 指定。")

    nihss_of = load_nihss(a.nihss, a.nihss_total_key) if a.nihss else {}
    if a.nihss:
        print(f"NIHSS 结果目录 {a.nihss}：{len(nihss_of)} 例")
        hit = sum(1 for f in files if Path(f).stem in nihss_of)
        if not nihss_of:
            print("  ⚠️ 一个结果文件都没读到 —— 检查目录和文件后缀。")
        elif hit == 0:
            print(f"  ⚠️ **一条都没对上**（0/{len(files)} 例）—— 多半不是这批的"
                  f"（F6 的病例名必须和 F3 的一致）。NIHSS 列留空，不做错误填充。")
            nihss_of = {}
        elif hit < len(files):
            print(f"  ⚠️ 只对上 {hit}/{len(files)} 例 —— 剩下那几行 NIHSS 列会是空的。")
    else:
        print("⚠️ 没给 --nihss —— 「NIHSS评分」列会有但为空"
              "（列数固定，不随调用方式变）。")

    kinds = {c: fp.col_kind(c) for c in flat}
    rows, traces = [], []
    stat = collections.defaultdict(collections.Counter)
    per_case = []
    cols_seen = set()
    result_cases = []
    all_states, all_vals, all_whys = [], [], []
    r1_cases = []                   # [(病例名, 拆分前条数, 拆分后条数)] R1 对账
    row_blank = []                    # [(行号, 病例名, 空值率, 空值格数, 占位格数)]
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        items = d.get("items") or {}
        cols_seen |= set(items)
        case = Path(str(d.get("case") or Path(f).stem)).name
        result_cases.append(case)

        def cell(k):
            """★ 修复的落点：按 state 渲染，不再 `or ""`。

            ★ 阴性还要看 `why`（route_f3.py:1224：「阴性不是一个，是两个」）：
              有否定证据 → 否/未做/未查/未用；配置断言 → 「未提及」，
              **不冒充「没做」**。所以这里必须把 证据.why 一并取出来。
            """
            r = items.get(k) or {}
            ev = r.get("证据") or {}
            return fp.render(r.get("state"), r.get("值"),
                             kinds.get(k, fp.DEFAULT_KIND), a.fill_mode,
                             ev.get("why"))

        def raw(k):
            r = items.get(k) or {}
            return str(r.get("值") or "")

        def srccell(k):
            """R1 来源列：该来源的条目逐条列出；没标注就明说没标注。"""
            origin = dict(HISTORY_SRC_COLS)[k]
            r = items.get(origin) or {}
            ev = r.get("证据") or {}
            if "来源条目" not in ev:
                # ★ 不静默留空 —— 空格子会被读成「这个来源没有记录」。
                return "未标注来源"
            recs = ev.get("来源条目") or []
            if not recs:
                return fp.render(r.get("state"), r.get("值"), "text",
                                 a.fill_mode, ev.get("why"))
            out = []
            for rec in recs:
                t = str(rec.get("条目") or "")
                if rec.get("疑似重复来源"):
                    t += "〔疑似重复:" + str(rec["疑似重复来源"]) + "〕"
                out.append(t)
            return "；".join(out)

        nh = nihss_of.get(case) or {}
        # 住院号：病例名永远可用，比"未记录"有用（保持原行为：取不到就用病例名）
        pid = raw("病案号") or case
        lead = [pid, cell("姓名")] + list(src_of.get(case, ("", ""))) \
            + [nh.get("total", "")]
        if a.nihss_detail:
            lead += [nh.get("src") or "", nh.get("esc", "")]
        row = (lead + [cell(c) for c in flat]
               + [srccell(c) for c in HISTORY_SRC_NAMES])
        rows.append(row)
        # R1 对账：逐例记下三个来源列的条数（= 拆分后），以及旧三列的原始值
        # （= 拆分前）。真正的「拆分前后条数相等」在下面的汇总里核。
        r1_cases.append((case,
                         [((items.get(o) or {}).get("证据") or {}).get("来源条目") or []
                          for _c, o in HISTORY_SRC_COLS],
                         [R3.split_history_entries((items.get(o) or {}).get("值"))
                          if R3 else None
                          for _c, o in HISTORY_SRC_COLS]))

        # 逐格状态/值（给统计与门禁用）
        for c in HISTORY_SRC_NAMES:
            stat[c]["条目数"] += len(
                ((items.get(dict(HISTORY_SRC_COLS)[c]) or {}).get("证据")
                 or {}).get("来源条目") or [])
        for c in flat:
            r = items.get(c) or {}
            st = r.get("state") or fp.STATE_PEND
            stat[c][st] += 1
            if st == fp.STATE_NEG:
                # ★ 阴性拆两半：有否定证据 vs 配置断言。
                #   56 个零命中列的「整片阴性」就是后者 —— 这个数是
                #   「映射错还是上游没产出」之外的第三个信号，先于诊断就能看。
                g = fp.neg_grounded((r.get("证据") or {}).get("why"))
                stat[c]["阴性_配置断言" if not g else "阴性_有据"] += 1
            all_states.append(st)
            all_vals.append(r.get("值"))
            all_whys.append((r.get("证据") or {}).get("why"))

        body_cells = row[len(lead):]
        nb = sum(1 for v in body_cells if str(v).strip() == "")
        nph = sum(1 for v in body_cells if fp.is_placeholder(v))
        # ★ 「这一例抽崩了」的信号在 filled 模式下**不再是空值率**（空格已被消灭），
        #   而是**待确认率** —— 整份 PDF 失败时几乎每一格都是待确认。
        #   两个都记：compat 模式看空值率，filled 模式看待确认率。
        npd = sum(1 for c in flat
                  if (items.get(c) or {}).get("state") == fp.STATE_PEND)
        row_blank.append((len(rows) + 1, case, npd / max(len(flat), 1), npd,
                          nb / max(len(flat), 1), nb, nph))

        per_case.append((case, len(items),
                         sum(1 for r in items.values() if r.get("state") == "待确认")))
        for c in flat:
            r = items.get(c) or {}
            st = r.get("state") or "缺"
            if not a.no_trace and st in (fp.STATE_POS, fp.STATE_PEND):
                ev = r.get("证据") or {}
                src = str(ev.get("span") or ev.get("quote") or ev.get("cell")
                          or ev.get("why") or "")[:200]
                traces.append([src_of.get(case, ("", ""))[0] or case, c, st,
                               r.get("来源层", ""), str(r.get("值") or ""), src])

    print(f"读入 {len(files)} 例")
    if not rows:
        sys.exit(1)
    # ★ R1 追加了 3 列，`len(rows[0]) - len(flat)` 会把它们算进前置列里。
    lead_n = len(rows[0]) - len(flat) - len(HISTORY_SRC_NAMES)
    miss = [c for c in flat if c not in cols_seen]
    print(f"  宽表 {len(rows)} 行 × {len(flat)} 列（+{lead_n} 前置列"
          f" +{len(HISTORY_SRC_NAMES)} 列 R1 病史来源列）")
    aft = sum(sum(len(x) for x in recs) for _c, recs, _o in r1_cases)
    per_src = [sum(len(recs[i]) for _c, recs, _o in r1_cases)
               for i in range(len(HISTORY_SRC_COLS))]
    print("  R1 病史来源（拆分后）: "
          + "  ".join(f"{n}={c}" for n, c in zip(per_src, HISTORY_SRC_NAMES))
          + f"  合计 {aft} 条 / {len(r1_cases)} 例")
    if R3 is None:
        print("  R1 对账：跳过 —— 导入 route_f3 失败，拆分前条数无法按同一口径统计")
        r1_report = (None, aft, None, per_src)
    else:
        bef = sum(sum(len(x) for x in ent) for _c, _r, ent in r1_cases)
        per_col = [sum(len(ent[i]) for _c, _r, ent in r1_cases)
                   for i in range(len(HISTORY_SRC_COLS))]
        print("  R1 病史条目（拆分前）: "
              + "  ".join(f"{n}={c}"
                          for n, c in zip(per_col, [o for _c, o in HISTORY_SRC_COLS]))
              + f"  合计 {bef} 条")
        print(f"  R1 对账: 拆分前 {bef} 条 {'==' if bef == aft else '!='} 拆分后 {aft} 条"
              f" → {'没丢数据' if bef == aft else '★ 对不上，必须查'}")
        r1_report = (bef, aft, per_col, per_src)
    if miss:
        print(f"  ⚠️ 有 {len(miss)} 列**在所有结果里都没出现过**：{miss[:8]}")

    # ── 汇总 ──
    tot = collections.Counter()
    for _c, n, p in per_case:
        tot["列"] += n
        tot["待确认"] += p
    print(f"  待确认合计 {tot['待确认']}/{tot['列']} = "
          f"{tot['待确认']/max(tot['列'],1):.1%}")

    # ── 两个口径，分开报（混着报会得出相反结论）─────────────────────
    st = fp.fill_stats(all_states, all_vals, all_whys)
    print(f"  有效填充率 {st['有效填充率']:.2%}"
          f"（阳性 {st['阳性']} − 占位 {st['占位']}）/ {st['总格数']}")
    print(f"  覆盖率     {st['覆盖率']:.2%}"
          f"（阳性 {st['阳性']} + 阴性 {st['阴性']}）/ {st['总格数']}")
    print(f"  有据覆盖率 {st['有据覆盖率']:.2%}"
          f"（有据阴性 {st['阴性_有据']}；配置断言 {st['阴性_配置断言']}"
          f" 不算结论）")

    # ── 门禁：拦住"静默失败" ─────────────────────────────────────────
    expect = a.expect if a.expect is not None else (
        len(manifest_names) if manifest_names else None)
    missing_cases = []
    if manifest_names:
        got = set(result_cases)
        missing_cases = [n for n in manifest_names if n not in got]
    elif expect is not None and expect != len(rows):
        missing_cases = [f"（无 manifest，只知道期望 {expect} 例、实得 {len(rows)} 例）"]

    if manifest_names or expect is not None:
        print(f"  文件对账：源 {expect} 例 / 输出 {len(rows)} 行"
              + (f"  ⚠️ **缺 {len(missing_cases)} 例**" if missing_cases else "  ✓"))

    # 空值率或待确认率任一超阈值都算「这一例抽崩了」
    bad_rows = [r for r in row_blank
                if r[2] > fp.GATES["bad_row_rate"] or r[4] > fp.GATES["bad_row_rate"]]
    zero_hit = [c for c in flat if stat[c].get(fp.STATE_POS, 0) == 0]
    empty_in_filled = sum(1 for r in rows for v in r[lead_n:] if str(v).strip() == "") \
        if a.fill_mode == "filled" else 0

    ok, reasons = fp.check_gates(st, len(rows), zero_hit, bad_rows,
                                 missing_cases, empty_in_filled)
    if not ok:
        print("\n[x] **写盘门禁没过** —— 未写文件。原因：")
        for r in reasons:
            print(f"    · {r}")
        if a.no_gate:
            print("    （--no-gate 已给，继续写盘；这些问题是真的，不是误报）")
        else:
            print("\n  加 --no-gate 可强制写出，但**先看看上面这几条** —— "
                  "门禁拦的都是会静默毁掉交付的问题。")
            sys.exit(2)
    else:
        print("  ✓ 写盘门禁通过")

    out = Path(a.out)
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        # ★ 前置列固定 5 个：住院号 / 姓名 / 来源目录 / 输入文件 / **NIHSS评分**。
        #   第 5 列是 F6 的总分，和第 3、4 列一样**总是存在**（没给 --nihss 时为空）——
        #   列数不随调用方式变，下游按列号读表的代码才不会静默错位。
        head = ["住院号", "姓名", "来源目录", "输入文件", "NIHSS评分"]
        if a.nihss_detail:
            head += ["NIHSS来源", "NIHSS升级项"]
        ws.append(head + flat + list(HISTORY_SRC_NAMES))
        for r in rows:
            ws.append(r)
        for c in range(1, len(head) + 1):
            ws.cell(1, c).font = Font(bold=True)
            ws.cell(1, c).fill = PatternFill("solid", fgColor="DDEBF7")
        ws.freeze_panes = chr(ord("A") + len(head)) + "2"

        ws2 = wb.create_sheet("三态统计")
        ws2.append(["列名", "阳性", "阴性", "待确认", "覆盖"])
        for c in flat:
            s = stat[c]
            n = sum(s.values()) or 1
            ws2.append([c, s.get("阳性", 0), s.get("阴性", 0), s.get("待确认", 0),
                        f"{(n - s.get('待确认', 0)) / n:.0%}"])
        # R1 来源列不是三态列，单独列（计数口径是**条数**不是例数）
        ws2.append([])
        ws2.append(["—— R1 病史来源列（计数=条数，不是例数）——"])
        for c in HISTORY_SRC_NAMES:
            ws2.append([c, stat[c].get("条目数", 0), "", "", ""])
        for cell in ws2[1]:
            cell.font = Font(bold=True)

        if traces and not a.no_trace:
            ws3 = wb.create_sheet("溯源")
            ws3.append(["来源目录", "列名", "状态", "来源层", "值", "依据"])
            for t in traces:
                ws3.append(t)
            for cell in ws3[1]:
                cell.font = Font(bold=True)
            for cell in ws3["F"]:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

        # ── 异常清单（4 张表）────────────────────────────────────────
        if not a.no_abnormal:
            ws4 = wb.create_sheet("异常清单")
            for title, header, *body in fp.build_abnormal(
                    result_cases, manifest_names, rows, flat, stat,
                    bad_rows, zero_hit, st):
                c = ws4.cell(ws4.max_row + 2 if ws4.max_row > 1 else 1, 1, title)
                c.font = Font(bold=True, color="C00000")
                ws4.append(list(header))
                for cell in ws4[ws4.max_row]:
                    cell.font = Font(bold=True)
                    cell.fill = PatternFill("solid", fgColor="FCE4D6")
                for r in body:
                    ws4.append(list(r))
            ws4.column_dimensions["A"].width = 26
            ws4.column_dimensions["B"].width = 16
            ws4.column_dimensions["C"].width = 60

        # ── 取值说明 ─────────────────────────────────────────────────
        ws5 = wb.create_sheet("取值说明")
        ws5.append(["项", "值", "说明"])
        for cell in ws5[1]:
            cell.font = Font(bold=True)
        ws5.append(["渲染模式", a.fill_mode,
                    "filled=按状态渲染；compat=旧行为（只写「值」）"])
        for k, v in fp.NEG_TEXT.items():
            n = sum(1 for c in flat if kinds.get(c) == k)
            ws5.append([f"阴性→{k}", v, f"本批 {n} 列属于此类"])
        ws5.append(["待确认", "照写「待确认」",
                    "**绝不写空** —— 空单元格分不清「没有」和「没抽到」"])
        ws5.append(["占位词", " / ".join(fp.PLACEHOLDERS),
                    "看着非空其实没答案，统计时单独扣除"])
        ws5.append(["有效填充率", f"{st['有效填充率']:.2%}", "(阳性 − 占位) / 总格数"])
        ws5.append(["覆盖率", f"{st['覆盖率']:.2%}", "(阳性 + 阴性) / 总格数"])
        ws5.append(["⚠", "覆盖率 ≠ 准确率",
                    "没有人工金标准就算不出准确率，这两个数不能互相替代"])

        for s in wb.worksheets:
            s.column_dimensions["A"].width = 18
        wb.save(out)
        sheets = " / ".join(s.title for s in wb.worksheets)
        print(f"已写 {out}（{sheets}）")
    except ImportError:
        import csv
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["住院号", "姓名"] + flat + list(HISTORY_SRC_NAMES))
            w.writerows(rows)
        print(f"已写 {out}（CSV，没装 openpyxl —— 无异常清单/取值说明）")
    print()
    print("这份表就是登记表本体：一行一个病人、498 列。")
    print("「三态统计」看哪一列最不稳；「异常清单」是**要办事的清单**。")
    if a.fill_mode == "filled":
        print("空格已被消灭：阴性写成 否/未做/未查/未用，待确认照写「待确认」。")


if __name__ == "__main__":
    main()
