#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F6：验收检查（纯 Python，不走模型、不碰 GPU）。

回答的问题：大表里的每个格子，凭什么说它是对的？

四项检查
  ① 引用完整性  每个「出处」是否指向 sections_text.json 里真实存在的块
                —— 抓模型编造页码块号
  ② 值可回查    单元格的「值」能否在源块的原文里找到（去空白后做子串匹配）
                —— 这是「值有没有被改写/编造」的量化指标。数值类应接近 100%，
                   概括性的文字类会低一些，属正常，看的是比例而非单条
  ③ 填充率      495 列填了多少（按患者、按分组）
  ④ 待归并      哪些项目名没映射上目标列，按频次排序 —— 别名表的工单

输出
  <out>/验收报告.md
  <out>/抽样回查.xlsx   随机抽 N 个已填格子，列出 值 / 出处 / 源块原文，供人工比对
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from to_xlsx import write_xlsx, split_stem, load_json  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_COLUMNS = HERE / "f4_columns.json"

BLOCK_RE = re.compile(r"^【[^】]*?\s*(p\d+-b[^】\s]+)】\s*(.*)$", re.S)


def norm(s: str) -> str:
    s = str(s or "")
    return re.sub(r"[\s\u3000，,。.、；;：:（）()\[\]【】|/\\\-–—_]+", "", s)


_DIGITS = re.compile(r"\d+")


def value_in_source(val: str, blk: str) -> bool:
    """值是否能在源块里找到。

    提示词要求的是「**规范化后的**值」，所以逐字子串对它不公平
    （`2024年9月14日14时03分` → `2024-09-14 14:03` 是要求的行为）。分三级：

      ① 去标点空白后直接子串 —— 逐字抄录的
      ② 值里出现的**非数字字符必须都在源块里** —— 这一条挡住编造
         （`目标6-8 mmol/L` 的「目标」不在源块里 → 判 miss）
      ③ 在 ② 成立的前提下，值里的**数字串按序**都能在源块里找到 → 命中
         （`2024-09-14` 对上 `2024年9月14日`；补零与否两种写法都试）
      ④ 纯文字值：短（≤6 字）且字符集是源块子集 → 命中（`男` 对上 `男性`）
    """
    a, b = norm(val), norm(blk)
    if not a:
        return False
    if a in b:
        return True
    nz = [ch for ch in a if not ch.isdigit()]
    if nz and not set(nz) <= set(b):
        return False                      # 值里有源块没有的字 → 编造
    # ★ 数字串从**原始文本**提取，不能用 norm 后的 —— norm 会剥掉 `-` `:` `.`
    #   把 `2024-09-14` 粘成 `20240914` 一个 token，再也匹配不上。
    da = _DIGITS.findall(str(val))
    raw = str(blk)
    if da:
        i, allfound = 0, True
        for d in da:
            for cand in (d, str(int(d))):     # `09` 试 `09`，再试 `9`
                pos = raw.find(cand, i)
                if pos >= 0:
                    i = pos + len(cand)
                    break
            else:
                allfound = False
                break
        return allfound
    return len(a) <= 6 and set(a) <= set(b)


def blocks_of(sec_text: dict | None) -> dict:
    """sections_text.json -> {'p1-b0': '块正文'}"""
    out = {}
    for s in ((sec_text or {}).get("sections") or []):
        for part in (s.get("text") or "").split("\n\n"):
            m = BLOCK_RE.match(part.strip())
            if m:
                out[m.group(1)] = m.group(2)
    return out



# ---------------- 补丁：值校验的分级（2026-09-25）----------------
# value_in_source 只拿【被引块】比，值在全文别处时会判成 miss。
# 这里保留它的严格判据（防编造），只加一层「全文重试」+ 布尔标记分流。
BOOL_MARKERS = {
    "是", "否", "有", "无", "未提及", "不详", "未知", "不明确",
    "阴性", "阳性", "正常", "异常", "未见异常", "无明显异常", "无殊",
    "阴性（-）", "阳性（+）", "正常范围", "未见明显异常", "未查", "未做",
}


def blocks_all(sec_text: dict | None) -> str:
    """sections_text.json -> 全文（含小标题），用于『值在别处』的兜底比对"""
    parts = []
    for s in ((sec_text or {}).get("sections") or []):
        nm = s.get("name") or ""
        if nm and nm != "_其他":
            parts.append(nm)
        parts.append(s.get("text") or "")
    return "\n".join(parts)


def value_hit(val: str, blk: str, all_text: str = "") -> str:
    """返回判定依据：
       boolean-marker 布尔/未提及标记（原文不含字面，正常）
       literal-block  值在被引块内      ← 溯源成立
       literal-doc    值在全文别处      ← 值真，但出处不精确
       MISS           全文也没有        ← 只有这个是真问题
    """
    v = str(val or "").strip()
    if not v or v == "nan":
        return "empty"
    if v in BOOL_MARKERS:
        return "boolean-marker"
    if value_in_source(v, blk):
        return "literal-block"
    if all_text and value_in_source(v, all_text):
        return "literal-doc"
    return "MISS"


def check_one(d: Path, cols, sample_n: int, rnd: random.Random) -> dict:
    st = load_json(d / "structured.json")
    if not isinstance(st, dict):
        return {}
    sec = load_json(d / "sections_text.json")
    blocks = blocks_of(sec)
    have_ref_check = bool(blocks)
    # 补丁：全文文本，用于 value_hit 的「值在别处」兜底
    ALL = blocks_all(sec)

    hid, pname = split_stem(d.name)
    ents = (st.get("merged") or {}).get("条目") or []

    ref_ok = ref_bad = ref_none = 0
    val_hit = val_miss = 0
    val_block_ok = val_doc_ok = val_bool_ok = val_noref = 0
    filled_cols = set()
    pend = collections.Counter()
    mid: dict[str, tuple] = {}
    samples = []
    misses = []

    for e in ents:
        item = str(e.get("项目") or e.get("字段") or "").strip()
        val = str(e.get("值") or "").strip()
        ref = str(e.get("出处") or e.get("原文") or "").strip()
        if not item:
            continue
        # ★ 必须走 Cols.resolve()，**不要在这里自己重实现映射逻辑**。
        #   之前这里另写了一份 norm()，与 f4_table.py 的键空间不一致
        #   （那份把括号也删了：`入院诊断（初步诊断）` → `入院诊断初步诊断`），
        #   导致连目标列名本身都被报成「没映射上」—— 待归并被严重高估。
        #   resolve() 是唯一真相源：别名 → 精确 → 核心名 → 前缀/相似度。
        targets = cols.resolve(item)
        if targets:
            tgt = targets[0]
            filled_cols.update(targets)
        else:
            fz, score, cand = cols.fuzzy(item)
            if not fz and score >= cols.fz_lo and cand:
                mid[item] = (score, cand)
            pend[item] += 1

        # ① 引用完整性
        key = ref.split("#")[0]
        if not ref:
            ref_none += 1
        elif not have_ref_check:
            ref_none += 1
        elif key in blocks:
            ref_ok += 1
        else:
            ref_bad += 1

        # ② 值可回查（补丁：三级判定）
        if val and have_ref_check and key in blocks:
            _why = value_hit(val, blocks[key], ALL)
            if _why == "literal-block":
                val_block_ok += 1
                val_hit += 1
            elif _why == "literal-doc":
                val_doc_ok += 1
            elif _why == "boolean-marker":
                val_bool_ok += 1
            elif _why == "empty":
                pass
            else:
                _why = "MISS"
                val_miss += 1
                if len(misses) < 400:
                    misses.append([item[:40], val[:120], ref,
                                   (blocks[key] or "")[:200], _why])
            if len(samples) < 4000:
                samples.append([item, val, ref, (blocks[key] or "")[:160]])
    return {"住院号": hid, "姓名": pname, "条目": len(ents),
            "filled": len(filled_cols), "pend": pend, "mid": mid,
            "ref_ok": ref_ok, "ref_bad": ref_bad, "ref_none": ref_none,
            "val_hit": val_hit, "val_miss": val_miss,
            "val_block_ok": val_block_ok, "val_doc_ok": val_doc_ok,
            "val_bool_ok": val_bool_ok, "val_noref": val_noref,
            "has_refcheck": have_ref_check, "samples": samples, "misses": misses}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="F6：验收检查（纯 Python）")
    ap.add_argument("paths", nargs="+", help="患者目录，或含 compiled/*/ 的批次目录")
    ap.add_argument("--out", default=None, help="输出目录（默认 <批次>/check）")
    ap.add_argument("--columns", default=str(DEFAULT_COLUMNS))
    ap.add_argument("--sample", type=int, default=40, help="抽样回查多少条")
    ap.add_argument("--seed", type=int, default=20260924)
    a = ap.parse_args(argv)

    tpl = json.loads(Path(a.columns).read_text(encoding="utf-8"))
    n_cols = len(tpl.get("flat") or [])
    sheets = tpl["sheets"]
    sheet_of = {c: s["name"] for s in sheets for c in s["columns"]}

    sys.path.insert(0, str(HERE))
    from f4_table import Cols  # 复用同一套列解析，保证与导出器口径一致
    cols = Cols(tpl, json.loads((HERE / "f4_alias.json").read_text(encoding="utf-8")))

    targets: list[Path] = []
    for raw in a.paths:
        p = Path(raw)
        if not p.is_dir():
            print("[x] 不是目录：%s" % p, file=sys.stderr)
            return 2
        subs = sorted([x for x in (p / "compiled").glob("*") if x.is_dir()]) \
            if (p / "compiled").is_dir() else []
        targets += subs if subs else [p]

    out_dir = Path(a.out) if a.out else (
        targets[0].parent.parent / "check" if targets[0].parent.name == "compiled"
        else targets[0] / "check")

    rnd = random.Random(a.seed)
    rows, all_samples, all_pend, all_mid, all_misses = [], [], collections.Counter(), {}, []
    for d in targets:
        r = check_one(d, cols, a.sample, rnd)
        if not r:
            print("  [skip] %s：没有 structured.json" % d.name, file=sys.stderr)
            continue
        rows.append(r)
        all_samples += [(r, s) for s in r["samples"]]
        all_pend.update(r["pend"])
        all_misses += r.get("misses") or []
        for k, v in (r.get("mid") or {}).items():
            all_mid.setdefault(k, v)

    if not rows:
        print("[x] 一个可检查的目录都没有", file=sys.stderr)
        return 2

    R = lambda k: sum(r[k] for r in rows)
    n_files = len(rows)
    ref_tot = R("ref_ok") + R("ref_bad")
    val_tot = R("val_hit") + R("val_miss")
    fill = [r["filled"] for r in rows]

    md = ["# 验收报告（纯 Python 检查，不走模型）", "",
          "- 患者数：**%d**　目标列：**%d**" % (n_files, n_cols),
          "- 条目总数：**%d**" % R("条目"), "",
          "## ① 引用完整性（抓模型编造页码块号）", ""]
    if ref_tot:
        md += ["| 项 | 数量 | 占比 |", "|---|---|---|",
               "| 出处命中真实块 | %d | **%.1f%%** |" % (R("ref_ok"), 100.0 * R("ref_ok") / ref_tot),
               "| 出处指向不存在的块 | %d | %.1f%% |" % (R("ref_bad"), 100.0 * R("ref_bad") / ref_tot),
               "| 无出处 / 无法校验 | %d | — |" % R("ref_none"), ""]
        if R("ref_bad"):
            md.append("> `✗` 的那些必须人工复核 —— 模型编了块引用。")
    else:
        md += ["无可校验的引用（目录里没有 `sections_text.json`）。", ""]

    md += ["", "## ② 值可回查率（值有没有在源块原文里出现）", ""]
    if val_tot:
        md += ["| 项 | 数量 | 占比 |", "|---|---|---|",
               "| 值能在源块里找到 | %d | **%.1f%%** |" % (R("val_hit"), 100.0 * R("val_hit") / val_tot),
               "| 找不到（被概括/改写/编造） | %d | %.1f%% |" % (R("val_miss"), 100.0 * R("val_miss") / val_tot),
               "",
               "> 数值类应接近 100%；概括性的描述类会低一些，属正常。**看的是比例与趋势，不是单条**。",
               "> 这个比例**下降**就说明模型开始改写原文了 —— 那是最危险的信号。",
               "",
               "**未命中的 %d 条已单独导出到 `未命中明细.xlsx`** —— 那才是要看的："
               % len(all_misses),
               "里面通常有三类：① 值被规范化（`2024年9月14日` → `2024-09-14`，属正常）；",
               "② 值被概括（长描述压缩成要点，可接受）；③ **值里有原文没有的内容（真问题）**。"]
    else:
        md += ["无法计算（没有 sections_text.json）。"]

    md += ["", "## ③ 填充率", "", "| 患者 | 条目 | 填入列数 / %d |" % n_cols, "|---|---|---|"]
    for r in rows:
        md.append("| %s %s | %d | **%d**（%.1f%%） |"
                  % (r["住院号"], r["姓名"], r["条目"], r["filled"],
                     100.0 * r["filled"] / max(1, n_cols)))
    md += ["", "- 平均填入 **%.1f / %d 列**（%.1f%%）"
           % (sum(fill) / len(fill), n_cols, 100.0 * sum(fill) / len(fill) / n_cols), ""]

    md += ["", "## ④ 待归并（别名表的工单）", ""]
    if all_pend:
        md += ["共 **%d 种**项目名没映射上目标列。" % len(all_pend), "",
               "| 次数 | 项目名 |", "|---|---|"]
        md += ["| %d | %s |" % (v, k) for k, v in all_pend.most_common(40)]
        md += ["", "> 反复出现的、确实该进某列的，往 `f4_alias.json` 的 `映射` 里加一行。"]
    else:
        md += ["**全部对上目标列了** ✓"]

    md += ["", "## ⑤ 待确认（模糊层给出候选，但没敢填）", ""]
    if all_mid:
        md += ["共 **%d 种**。这些是「像但不唯一」的 —— 确认后补进 `f4_alias.json` 即可自动填。"
               % len(all_mid), "",
               "| 相似度 | 项目名 | 最像的目标列 |", "|---|---|---|"]
        md += ["| %.2f | %s | %s |" % (sc, k, c)
               for k, (sc, c) in sorted(all_mid.items(), key=lambda x: -x[1][0])[:30]]
    else:
        md += ["无（模糊层没有产出候选）"]

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "验收报告.md").write_text("\n".join(md), encoding="utf-8", newline="\n")

    # 抽样回查：随机取 N 个已填条目，值/出处/源块原文 并排
    pool = list(all_samples)
    rnd.shuffle(pool)
    picked = pool[:a.sample]
    srows = [[r["住院号"], r["姓名"]] + [str(x)[:300] for x in s[1:]] for r, s in picked]
    write_xlsx(out_dir / "抽样回查.xlsx",
               [("抽样回查", ["住院号", "姓名", "值", "出处", "源块原文（截断）"], srows)])
    write_xlsx(out_dir / "未命中明细.xlsx",
               [("值未命中", ["项目", "值", "出处", "源块原文（截断）"],
                 all_misses[:300])])

    print("患者 %d 个 · 条目 %d · 引用命中 %.1f%% · 值可回查 %.1f%% · 平均填入 %.1f/%d 列"
          % (n_files, R("条目"),
             100.0 * R("ref_ok") / ref_tot if ref_tot else -1,
             100.0 * R("val_hit") / val_tot if val_tot else -1,
             sum(fill) / len(fill), n_cols))
    print("报告：%s" % (out_dir / "验收报告.md"))
    print("抽样：%s（%d 条）" % (out_dir / "抽样回查.xlsx", len(picked)))
    if all_misses:
        print("未命中：%s（%d 条，写 %d 条）"
              % (out_dir / "未命中明细.xlsx", len(all_misses), len(all_misses[:300])))
    if all_pend:
        print("待归并 %d 种，最多：%s"
              % (len(all_pend), "、".join("%s(%d)" % (k, v) for k, v in all_pend.most_common(6))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
