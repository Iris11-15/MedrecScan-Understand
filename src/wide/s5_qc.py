#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段⑤ s5_qc.py —— 校验、每字段缺失率、异常值检测、变量字典。

输入   run/wide/<batch>/04_norm/<case_id>/fields.json
       run/wide/<batch>/02_text/<case_id>/_manifest.json（空白页数）
输出   run/wide/<batch>/05_qc/<case_id>/qc.json          该例的字段级 flag
       run/wide/<batch>/05_qc/_summary/missing_rates.csv 每字段缺失率
       run/wide/<batch>/05_qc/_summary/variable_dictionary.csv
       run/wide/<batch>/05_qc/_summary/outliers.csv      异常/冲突 flag（不含 missing，缺失看 missing_rates.csv）
       run/wide/<batch>/_audit.jsonl

红线②：任何填充率变化都要能追到具体改动 —— 所以这里只**报告**，不回填、不改值。
round 10 第1.5件：新增两类 flag，专门治「来源静默」——
  * no_source_label      —— 有值但 source_label 缺失（qc_flags.csv + outliers.csv +
                            missing_rates.csv 的 flag_kinds 列给出列分布）
  * provenance_rejected  —— s4 来源守卫判坏的格（原值长度/哈希在 fields.json:raw_rejected）
每轮报告必须附「无 source_label 的格数 + 列分布」（docs/REPORT_TEMPLATE.md）。
PII：字典与示例值一律走 common.scrub_for_log 打码。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wide import common as C  # noqa: E402

STAGE = "05_qc"

# round16 第7步：日期列统一成「YYYY年MM月DD日」之后，旧的 str(v)[:10] 字典序
# 会把「2024年12月29日」截成「2024年12月2」→ 同月/同日比较失效。比较一律走日期键。
DATE_YMD_RE = re.compile(r"^\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# round33 第1.2件：标准化入院时间的交付形态 = ISO，可选带分钟（不带秒、不带 T）
ISO_STD_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
# round33 第1.1件：年龄去单位后的交付形态
AGE_PLAIN_RE = re.compile(r"^\d+$")
# 这些列**必须是字符串**：一旦被当成数值处理就会掉进科学计数法 / 丢前导零
# （实测本批 493 个住院号带前导 0、128 个超过 9 位）。宽表里出现这种形态 ⇒ 必须报警。
SCI_NOTATION_RE = re.compile(r"\d(?:\.\d+)?[eE]\+?\d{1,3}")
ID_LIKE_COLS = ("adm_no", "case_no", "case_no_std", "seq_no", "id_card",
                "patient_phone", "contact_phone")


def date_key(v) -> str:
    """可排序的日期键；认不出的原样返回前 10 字符（保持旧行为）。"""
    s = str(v)
    m = DATE_YMD_RE.match(s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return s[:10]

ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
ID_CHECK = "10X98765432"
PAGE_FLAG_COL = "*"          # 页级 flag 不属于任何一列（qc_flags.csv 里 column_id 写 "*"）
# derived/normalized/computed 三类的来源是 from/rule（按构造），本来就没有 source_label，
# 不算「来路不明」—— 见 s4_normalize.py 的 fields["case_no_std"] 等。
DERIVED_STRATS = {"derived", "normalized", "computed"}


def page_flags(text_dir: Path) -> list[dict]:
    """round 7 第1件：把「哪一页被 max_tokens 截断」「哪一页是复读循环」标出来。

    落点两处：qc_flags.csv（页级 flag 台账，page 列可 grep）与 outliers.csv（和列级异常同表）。

    只报告、不改值（红线②）。命中数不变 ≠ 内容没问题 —— 143/240 格来自 llm 策略且
    **不带页级 provenance**，所以「截断页进了 prompt」这件事只能靠这里显式记账才看得见。
    """
    out: list[dict] = []
    if not text_dir.is_dir():
        return out
    for pj in sorted(text_dir.glob("page_*.json")):
        r = C.read_json(pj)
        p1 = r.get("page_no_1based")
        page = r.get("page")
        where = f"page {p1}（0-based {r.get('page')}）"
        if r.get("truncated"):
            bands = r.get("truncated_bands") or []
            out.append({"column_id": PAGE_FLAG_COL, "kind": "truncated",
                        "page": page, "page_no_1based": p1,
                        "detail": f"{where} OCR 正文撞 max_tokens 被截断"
                                  f"（截断 band {bands or [1]} / 共 {r.get('n_bands') or 1} band）"
                                  f" —— 该页不完整，不得当完整页用"})
        if r.get("repeat_loop"):
            d = r.get("repeat_loop_detail") or {}
            out.append({"column_id": PAGE_FLAG_COL, "kind": "repeat_loop",
                        "page": page, "page_no_1based": p1,
                        "detail": f"{where} 复读循环：max_run={d.get('max_run')} "
                                  f"uniq/nonempty={d.get('n_uniq')}/{d.get('n_nonempty')}"
                                  f"={d.get('uniq_ratio')} 判据={d.get('why')}"})
        if r.get("repeat_abort"):
            for a in (r.get("repeat_abort_detail") or [{}]):
                out.append({"column_id": PAGE_FLAG_COL, "kind": "repeat_aborted",
                            "page": page, "page_no_1based": p1,
                            "detail": f"{where} band {a.get('band') or 1} 复读被客户端断流砍掉："
                                      f"同一条非空行（len={a.get('line_len')} "
                                      f"sha16={a.get('line_sha16')}）已出现 "
                                      f"{a.get('n_copies')} 次 → 该页正文不完整"})
    return out


def id_card_ok(s: str) -> tuple[bool, str]:
    s = re.sub(r"\s", "", s or "")
    if not re.fullmatch(r"\d{17}[\dXx]", s):
        return False, "格式不是 17 位数字 + 校验位"
    total = sum(int(c) * w for c, w in zip(s[:17], ID_WEIGHTS))
    if ID_CHECK[total % 11] != s[17].upper():
        return False, "校验位不符（GB 11643）"
    return True, "ok"


def check_case(fields: dict, cfg: dict, cols_cfg: dict,
               other_labels: dict | None = None) -> list[dict]:
    qc = cfg["qc"]
    flags: list[dict] = []

    def flag(cid, kind, detail):
        flags.append({"column_id": cid, "kind": kind, "detail": detail})

    for col in cols_cfg["columns"]:
        cid = col["id"]
        f = fields.get(cid, {})
        v = f.get("value")
        if v is None:
            rej = f.get("raw_rejected")
            if rej:
                flag(cid, "provenance_rejected",
                     f"来源守卫判坏（reason={rej.get('reason')} strategy={rej.get('strategy')} "
                     f"source_label={rej.get('source_label')!r} 原值长度={rej.get('value_len')}）"
                     f" —— 来路不明，按红线①⑥不给值")
            if f.get("age_norm") == "unparseable":
                raw_age = f.get("pre_norm_value")
                flag(cid, "age_unparseable",
                     f"年龄原文不是数字（模板残片 / OCR 噪声），按红线⑥判缺失：{raw_age!r}")
            flag(cid, "missing", f.get("null_reason") or "无值")
            continue
        # round 10 第1.5件：有值但说不出来源 → 必须进 qc_flags.csv，不许再静默。
        # 并发之后「两趟不一致」已是常态，没有来源标注就回答不出「这一格为什么是这个值」。
        if f.get("source_label") is None and f.get("strategy") not in DERIVED_STRATS \
                and f.get("column_source") not in DERIVED_STRATS:
            flag(cid, "no_source_label",
                 f"有值但 source_label 缺失（strategy={f.get('strategy')} "
                 f"column_source={f.get('column_source')}）—— 这一格回答不出「值从哪来」")
        if col["source"] == "first_progress":
            vb = f.get("verbatim") or {}
            if vb.get("present") and vb.get("verbatim") is False:
                absent = vb.get("absent_char_count")
                flag(cid, "not_verbatim",
                     f"抽取值不是首程原文的子串（长度 {vb.get('len')}，原文中完全"
                     f"找不到的字符 {absent if absent is not None else 'unknown'} 个）—— "
                     + (absent and "值里有原文没有的字符 ⇒ 疑似改写"
                        or "值逐字来自原文但不连续 ⇒ 疑页眉页脚内联 / 同段多副本 / 跨页"))
        if cid == "id_card":
            ok, why = id_card_ok(str(v))
            if not ok:
                flag(cid, "format", why)
        if cid in ("patient_phone", "contact_phone"):
            s = re.sub(r"\D", "", str(v))
            lo, hi = qc["phone_len"]
            if not (int(lo) <= len(s) <= int(hi)):
                flag(cid, "format", f"数字位数 {len(s)} 不在 [{lo},{hi}]")
        if cid in ID_LIKE_COLS and SCI_NOTATION_RE.search(str(v)):
            flag(cid, "sci_notation",
                 f"这一列必须是字符串，现在像被转成了科学计数法：{v!r}")
        if cid == "age":
            m = re.search(r"\d+", str(v))
            if not m:
                flag(cid, "format", "取不到数字")
            else:
                a = int(m.group(0))
                if not (qc["age_min"] <= a <= qc["age_max"]):
                    flag(cid, "outlier", f"年龄 {a} 超出 [{qc['age_min']},{qc['age_max']}]")
        if cid == "los_days":
            try:
                d = int(v)
            except (TypeError, ValueError):
                flag(cid, "format", f"不是整数：{v!r}")
                continue
            if not (qc["los_min"] <= d <= qc["los_max"]):
                flag(cid, "outlier", f"住院天数 {d} 超出 [{qc['los_min']},{qc['los_max']}]")
            agree = f.get("paths_agree")
            if agree is False:
                flag(cid, "conflict", f"两条路不一致 A={f.get('path_a_frontpage')} "
                                      f"B={f.get('path_b_discharge_minus_admit')}")
            if agree is None:
                flag(cid, "single_path", f"只有一条路命中：{f.get('path_used')}")
        if cid in ("admit_time_std", "discharge_date"):
            if f.get("rule") in ("no_value", "无法解析", "cn_date 越界", "iso 越界"):
                flag(cid, "format", f"日期解析失败：{f.get('rule')}")
        # ── round33 第1.1件：年龄去单位（只剥后缀）→ 剥了什么、没剥的都记账 ──
        if cid == "age":
            how = f.get("age_norm")
            pre = f.get("pre_norm_value")
            if how == "stripped_whitespace":
                flag(cid, "age_unit_whitespace",
                     f"数字与「岁」之间有空格，已剥后缀取纯数字："
                     f"{f.get('pre_norm_value')!r} → {v!r}")
            elif how in ("stripped_y", "stripped_y_months", "decimal_truncated"):
                flag(cid, "age_unit_stripped",
                     f"年龄去单位（{how}）：{pre!r} → {v!r}")
            elif how not in ("stripped", "already_numeric"):
                flag(cid, "age_format_unexpected", f"年龄未做去单位处理（age_norm={how!r}）：{v!r}")
            if not AGE_PLAIN_RE.fullmatch(str(v)):
                flag(cid, "age_not_plain_number", f"年龄列输出不是纯数字：{v!r}")
        # ── round33 第1.3件：入院时间精度 —— 页面印到哪一级就抄到哪一级 ──
        if cid in ("admit_time_raw", "admit_time_std", "admit_time_std_dup"):
            prec = f.get("admit_time_precision")
            rawv = fields.get("admit_time_raw", {}).get("value")
            if prec == "hour":
                flag(cid, "admit_time_hour_only",
                     f"页面只印到「时」没有「分」→ 原文照抄到小时（{rawv!r}），"
                     f"标准化列只到日期；按红线⑥不补 00:00")
            elif prec == "date_only":
                flag(cid, "admit_time_no_clock",
                     f"首页入院时间没有时分（{rawv!r}）→ 标准化列只到日期；"
                     f"不补 00:00、不拿出院时间倒推")
            elif prec == "none":
                flag(cid, "admit_time_unparsable",
                     f"入院时间取不到日期（{rawv!r}）→ 标准化列填 null")
            if cid in ("admit_time_std", "admit_time_std_dup") \
                    and not ISO_STD_DATE_RE.fullmatch(str(v)):
                flag(cid, "format",
                     f"标准化入院时间不是 ISO 形态「YYYY-MM-DD[ HH:MM]」：{v!r}")
        # round16 第7步：归一化匹配不上 → 保留原值，但必须进 qc_flags.csv 台账
        if f.get("date_format_unmatched"):
            flag(cid, "date_format_unmatched",
                 f"日期格式无法归一化为「YYYY年MM月DD日」，按红线⑥保留原值：{v!r}")
        # round16 7f：ID 列尾随串已按格式归一化砍掉 → 留一条可审计的记录
        if f.get("id_tail_trimmed"):
            flag(cid, "id_tail_trimmed",
                 f"去掉尾随串 {f.get('id_tail_trimmed')!r}：{f.get('pre_trim_value')!r} → {v!r}")
        # 跨栏串栏：值里出现了**别的字段的标签**（实测奉化 008「非医嘱出院原因」
        # 取到「5. 死亡 9. 其他是否有出院31天内再住院计划 ① 1. 无 2. 有，」）。
        # 只报不改（红线②）—— 要不要截断由人裁决。
        for lab, owner in (other_labels or {}).items():
            if owner != cid and lab and lab in str(v):
                flag(cid, "bleed", f"值里含别的字段的标签「{lab}」（属于 {owner}）—— 疑似跨栏串栏")
                break

    # 跨字段一致性
    a = fields.get("patient_name", {}).get("value")
    b = fields.get("patient_name_dup", {}).get("value")
    if a is not None and b is not None and a != b:
        flag("patient_name_dup", "conflict", "第 2 列与第 8 列同名但取值不同")
    d_in = fields.get("admit_time_std", {}).get("value")
    d_out = fields.get("discharge_date", {}).get("value")
    if d_in and d_out and date_key(d_in) > date_key(d_out):
        flag("discharge_date", "conflict", f"出院({d_out}) 早于入院({d_in})")
    return flags


def summarize(batch_dir: Path, cols_cfg: dict, cfg: dict, cases: list[str]) -> dict:
    rows = {}
    per_case_flags = []
    # 标签 → 列 id（用来发现跨栏串栏）。种子来自 columns.yaml 的 candidate_labels。
    label_owner: dict[str, str] = {}
    for col in cols_cfg["columns"]:
        for lab in col.get("candidate_labels") or []:
            lab = str(lab).strip()
            if lab:
                label_owner.setdefault(lab, col["id"])
    for c in cases:
        fj = batch_dir / "04_norm" / c / "fields.json"
        if not fj.exists():
            continue
        rec = C.read_json(fj)
        fields = rec.get("fields", {})
        flags = check_case(fields, cfg, cols_cfg, label_owner)
        flags = flags + page_flags(batch_dir / "02_text" / c)
        # round54 P0-1：阶段③自报不完整的例以前对 QC 完全静默（标记没人读）。
        # 不删行、不改值（红线⑦），只让它进 qc_flags.csv 台账 —— 看得见才算数。
        for _mk, _kind in ((".incomplete", "stage3_incomplete"),
                           (".progress_failed", "stage3_progress_failed")):
            if (batch_dir / "03_extract" / c / _mk).exists():
                flags.append({"column_id": PAGE_FLAG_COL, "kind": _kind,
                              "detail": f"03_extract/{c}/{_mk} 存在 ⇒ 阶段③自报未跑完，"
                                        "首程来源的列按缺失处理，需人工确认"})
        C.write_json(batch_dir / STAGE / c / "qc.json",
                     {"case_id": c, "flags": flags,
                      "n_flags": len(flags), "mapping_version": rec.get("mapping_version")})
        per_case_flags.append((c, flags))
        for col in cols_cfg["columns"]:
            cid = col["id"]
            r = rows.setdefault(cid, {"filled": 0, "null": 0, "example": None,
                                      "flags": {}})
            v = fields.get(cid, {}).get("value")
            if v is None:
                r["null"] += 1
            else:
                r["filled"] += 1
                if r["example"] is None:
                    r["example"] = v
        for fl in flags:
            r = rows.setdefault(fl["column_id"], {"filled": 0, "null": 0, "example": None,
                                                  "flags": {}})
            r["flags"][fl["kind"]] = r["flags"].get(fl["kind"], 0) + 1

    # round 10 第1.5件：来源标注缺失的规模 + 列分布（每轮报告必附这一项）。
    n_nolabel = sum(1 for _, fls in per_case_flags
                    for fl in fls if fl["kind"] == "no_source_label")
    nolabel_by_col = {}
    for _, fls in per_case_flags:
        for fl in fls:
            if fl["kind"] == "no_source_label":
                nolabel_by_col[fl["column_id"]] = nolabel_by_col.get(fl["column_id"], 0) + 1
    n_rejected = sum(1 for _, fls in per_case_flags
                     for fl in fls if fl["kind"] == "provenance_rejected")

    out_dir = batch_dir / STAGE / "_summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    pii = C.pii_ids(cols_cfg)
    n = sum(1 for c in cases if (batch_dir / "04_norm" / c / "fields.json").exists())

    mp = out_dir / "missing_rates.csv"
    with open(mp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["column_id", "header", "source", "n_rows", "n_filled", "n_null",
                    "missing_rate", "flag_kinds", "example_masked"])
        for i, col in enumerate(cols_cfg["columns"], start=1):
            r = rows.get(col["id"], {"filled": 0, "null": 0, "example": None, "flags": {}})
            ex = r["example"]
            if col["id"] in pii and ex is not None:
                ex = C.mask_value(ex, col["id"], cfg["pii"])
            w.writerow([col["id"], col["header"], col["source"], n, r["filled"], r["null"],
                        f"{(r['null'] / n if n else 0):.4f}",
                        ";".join(f"{k}={v}" for k, v in sorted(r["flags"].items())),
                        "" if ex is None else str(ex)[:40]])
    C.set_output_mode(mp, cfg["pii"])

    vd = out_dir / "variable_dictionary.csv"
    with open(vd, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["position", "column_id", "header", "column_source", "pii",
                    "dtype", "allowed_values", "definition", "note"])
        defs = cols_cfg.get("definitions") or {}
        for i, col in enumerate(cols_cfg["columns"], start=1):
            d = defs.get(col["id"]) or {}
            w.writerow([i, col["id"], col["header"], col["source"],
                        "yes" if col.get("pii") else "no",
                        d.get("dtype") or ("integer" if col["id"] in ("seq_no","los_days") else "text"),
                        d.get("allowed", ""), d.get("definition", ""), col.get("note", "")])
    C.set_output_mode(vd, cfg["pii"])

    op = out_dir / "outliers.csv"
    with open(op, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "column_id", "kind", "detail"])
        for c, flags in per_case_flags:
            for fl in flags:
                if fl["kind"] == "missing":
                    continue          # 缺失已在 missing_rates.csv 统计，本文件只放异常/冲突
                w.writerow([c, fl["column_id"], fl["kind"], fl["detail"]])
    C.set_output_mode(op, cfg["pii"])
    qf = out_dir / "qc_flags.csv"
    with open(qf, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "column_id", "page", "page_no_1based", "kind", "detail"])
        for c, flags in per_case_flags:
            for fl in flags:
                w.writerow([c, fl["column_id"], fl.get("page", ""),
                            fl.get("page_no_1based", ""), fl["kind"], fl["detail"]])
    C.set_output_mode(qf, cfg["pii"])
    return {"missing_rates_csv": str(mp), "variable_dictionary_csv": str(vd),
            "qc_flags_csv": str(qf),
            "outliers_csv": str(op),
            "outliers_kinds": ("format/outlier/conflict/single_path/bleed/"
                               "no_source_label/provenance_rejected"),
            "n_cases": n,
            "n_no_source_label": n_nolabel,
            "no_source_label_by_column": nolabel_by_col,
            "n_provenance_rejected": n_rejected}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="阶段⑤ 校验与质控")
    ap.add_argument("--batch", required=True)
    C.add_case_args(ap)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    cfg = C.load_pipeline()
    cols = C.load_columns()
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    audit = C.Audit(batch_dir / "_audit.jsonl", STAGE)
    from wide import s1_ocr as S1
    rows = S1.read_manifest(batch_dir / "00_index" / "manifest.tsv")
    cases = [r["case_id"] for r in rows]
    want = C.wanted_cases(a.case, a.case_file)
    if want:
        cases = [c for c in cases if c in want]
    cases = [c for c in cases if (batch_dir / "04_norm" / c / "fields.json").exists()]
    s = summarize(batch_dir, cols, cfg, cases)
    audit.emit(kind="qc_summary", **s)
    C.log(f"质控汇总：{s['n_cases']} 例 → {s['missing_rates_csv']}")
    C.log(f"    来源标注：无 source_label 的格 {s['n_no_source_label']} 例次"
          f"（列分布 {s['no_source_label_by_column'] or '无'}）；"
          f"来源守卫判坏 {s['n_provenance_rejected']} 例次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
