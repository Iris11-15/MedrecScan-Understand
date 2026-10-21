#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段⑥ s6_table.py —— 一行一例的宽表（列数 = config/columns.yaml）+ 审计 JSONL。

输入   run/wide/<batch>/04_norm/<case_id>/fields.json
       run/wide/<batch>/05_qc/<case_id>/qc.json
输出   run/wide/<batch>/06_table/wide.csv      表头 = config/columns.yaml 的 header，原样
       run/wide/<batch>/06_table/wide.xlsx
       run/wide/<batch>/06_table/qc_flags.csv  flag 单独放，不污染模板列
       run/wide/<batch>/06_table/audit.jsonl   OCR 输入 / Prompt / 输出 三哈希汇总
       run/wide/<batch>/06_table/_manifest.json
权限   全部 600（config:pii.output_file_mode）。

增量模式（--incremental）的顺序闸门（round42n）：
  原语义「manifest 里前面有例还没出 04_norm 产物就停手」保证追加顺序恒等于 manifest 顺序，
  但**一个永远出不了 fields.json 的例会把后面所有例永久堵死**（round42n 实测：
  1 例 s3 以 parse_error 结束 ⇒ 后面 1476 例已就绪也进不了表）。
  现在多一条「终态失败」放行分支：03_extract/<cid>/ 存在 且 本例上游产物最新 mtime
  距今 > --stale-min（默认 30 分钟；config:table.incremental_stale_min 可覆盖；
  --no-stale-skip 关闭 = 旧行为）⇒ 判定该例已终态但没出阶段④产物 ⇒
  **不追加行，但必须记账**：state.skipped + 06_table/_incomplete_cases.csv +
  qc_flags.csv 的 s6_skipped_stale + _manifest.json 的 cases_stale_failed。
  被跳过的例后来出了产物 ⇒ 按 manifest 顺序**整体重建** wide.csv（不追加，避免错位）。
  ⇒ 口径变更：wide.csv 与一次性写出的「逐字节相同」现在只对「未被判终态失败的例」成立。

全量模式（不带 --incremental）同样必须记账（round42n 补）：
  没进宽表的例写 06_table/_incomplete_cases.csv（missing_stage4 / not_processed）
  + qc_flags.csv 同名 flag + _manifest.json 的 cases_missing_stage4 / cases_not_processed。
  原先只有增量模式写这些，收尾那次全量跑会把它们**冲成静默**（线上实测：
  8 例缺阶段④ + 8 例未跑上游 ⇒ qc_flags.csv 里 0 条）。

退出码（2026-10-09 修）：
  0  正常出表（允许「本批未处理」的例存在，只警告不失败）
  1  真断链（阶段③有产物但阶段④没有）或表为空 —— 空表**不覆盖**已有 wide.csv
  旧行为把「本批根本没跑过①②③的例」也算成「缺阶段④」，106 例的 manifest 里
  只跑了 1 例就报 106 例缺失，且先把空表写盘再退出 —— 上一轮的产物被清空。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wide import common as C  # noqa: E402

STAGE = "06_table"

# ── round33 1.4 删列 / 1.5 去重 ────────────────────────────────────────────
# 缺键 = 开（与 s4 provenance_guard 同一约定）；对照跑法用 CLI 旗标关，正式跑批不许关。
DEFAULT_DROP_IDS = ("hospital",)          # 就诊医院 106/106 全空；标准化就诊医院 106/106 有值
CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# ── round34 Part B：判重键 = 四元组 + 空值安全网 + 跨院区撞车守卫 ──────────────
# 键形态：quad = (标准化就诊医院, 病案号, 住院号, 入院日期)  ← 交付默认
#          triple = (病案号, 住院号, 入院日期)                ← 旧键，只用于对照跑批
# 为什么加院区维度：本批实测「病案号 == 住院号」（同一编号的两份拷贝）⇒ 这个编号
# 跨院区没有全局唯一性，只靠编号撞车是迟早的事（round34 背景③）。
DEDUP_KEY_FORMS = ("quad", "triple")
DEFAULT_DEDUP_KEY_FORM = "quad"
DEDUP_COMPONENTS = ("hospital_std", "case_no", "adm_no", "date")
# 键算出来需要读的列（这几列被 drop_output_columns 删掉 ⇒ 去重必须停，不能瞎算）
KEY_SOURCE_IDS = ("hospital_std", "case_no", "adm_no", "admit_time_raw", "admit_time_std")


def resolve_dedup_key_form(cfg: dict, cli_value) -> str:
    """normalize.dedup.key 缺键 = quad（四元组）。triple 只用于对照，正式跑批不许用。"""
    v = str(cli_value or cfg.get("normalize", {}).get("dedup", {}).get("key",
                                                                       DEFAULT_DEDUP_KEY_FORM))
    if v not in DEDUP_KEY_FORMS:
        C.err(f"normalize.dedup.key={v!r} 不认识（可用 {list(DEDUP_KEY_FORMS)}）"
              f"—— 按默认 {DEFAULT_DEDUP_KEY_FORM} 跑")
        v = DEFAULT_DEDUP_KEY_FORM
    if v == "triple":
        C.warn("去重键 = 旧三元组 (病案号,住院号,入院日期)：对照跑法，正式跑批不许用"
               "（round34 Part B：交付键是四元组，含标准化就诊医院）")
    return v


def resolve_drop_ids(cfg: dict, keep_hospital: bool) -> list:
    """1.4 要丢掉的列 id。normalize.drop_output_columns 缺键 = 开。"""
    if keep_hospital:
        C.warn("--keep-hospital-column：对照跑法，本次**不删** 就诊医院 列"
               "（正式跑批不许关，见 round33 §1.4）")
        return []
    v = cfg.get("normalize", {}).get("drop_output_columns", list(DEFAULT_DROP_IDS))
    return [str(x) for x in (v or [])]


def date_part(value) -> str:
    """入院时间的【日期部分】→ yyyy-MM-dd。取不到 ⇒ ""（不进判重键）。
    时分不进键 —— OCR 的时分本来就抖，进键会导致行数漂移（round33 §1.5）。"""
    s = "" if value is None else str(value)
    m = CN_DATE_RE.search(s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = ISO_DATE_RE.search(s)
    if m:
        return m.group(0)
    return ""


def dedup_table(table: list, cids: list, ids: list, cfg: dict, key_form: str = None):
    """1.5 去重（round34 Part B）。交付键 = 四元组，规则见 _dedup_core。

    key_form=None ⇒ 读 normalize.dedup.key（缺键 = quad）。triple 只用于对照跑批。
    """
    d = cfg.get("normalize", {}).get("dedup", {})
    if not d.get("enabled", True):
        C.warn("normalize.dedup.enabled=false：对照跑法，本次**不去重**")
        return table, [], [], {}
    key_form = resolve_dedup_key_form(cfg, key_form)
    return _dedup_core(table, cids, ids, key_form, d)


def key_components(row: list, ids: list) -> dict:
    """判重键的四个分量。日期只取【日期部分】(yyyy-MM-dd)，**时分不进键**。

    日期部分优先取标准化列（已是 yyyy-MM-dd[ HH:MM]），取不到再回退原文列
    （yyyy年MM月dd日 / yyyy-MM-dd）。两条路都取不到 ⇒ ""（⇒ 该行键不完整）。
    """
    i_h = ids.index("hospital_std")
    i_c = ids.index("case_no")
    i_a = ids.index("adm_no")
    i_std, i_raw = ids.index("admit_time_std"), ids.index("admit_time_raw")
    return {"hospital_std": str(row[i_h]).strip(),
            "case_no": str(row[i_c]).strip(),
            "adm_no": str(row[i_a]).strip(),
            "date": date_part(row[i_std]) or date_part(row[i_raw])}


def _dedup_core(table: list, cids: list, ids: list, key_form: str, d: dict):
    """去重内核（quad / triple 共用）。

    · B2 交付键 = 四元组 (标准化就诊医院, 病案号, 住院号, 入院日期)；时分不进键。
    · B3 空值安全网：四个分量任一为空 ⇒ 该行不参与合并，**也不与其它空键行互并**；
      每个这样的行记一条 QC flag `dedup_key_incomplete`（行号 + 缺哪个分量）。
    · B4 跨院区撞车守卫：(病案号,住院号,入院日期) 相同但标准化就诊医院不同
      ⇒ 这些行**绝不合并**，记 `dedup_cross_hospital_collision`，并由调用方按
      normalize.dedup.cross_hospital_guard（缺键=error）决定退出码。
    · 保留规则（写死、可复核）：**整行原样**取「非空单元格更多」的那一行；
      并列时取 manifest 顺序在前的那一行。**不跨行合并字段** —— 保留行的每个
      单元格就是它自己的原值，被丢那行的值一律丢弃（不挑、不混）。
    · 丢弃不静默：dropped 行（全列）+ 逐列冲突清单 + 撞车组一起返回，调用方落盘。
    """
    missing_cols = [c for c in KEY_SOURCE_IDS if c not in ids]
    if missing_cols:
        C.err(f"去重键依赖的列不在输出列里：{missing_cols} —— 本次**不去重**"
              "（键算不出来就不能合并，红线⑦）")
        return table, [], [], {"stats": {"key_form": key_form, "disabled_reason":
                                         f"missing key columns {missing_cols}"}}

    comps = [key_components(row, ids) for row in table]
    incomplete, groups = [], {}
    for idx, comp in enumerate(comps):
        miss = [k for k in DEDUP_COMPONENTS if not comp[k]]
        if miss:
            incomplete.append({"row": idx, "case_id": cids[idx], "missing": miss,
                               "merged": False})
            continue                     # 键不完整 ⇒ 不参与合并（也不与空键行互并）
        if key_form == "quad":
            key = (comp["hospital_std"], comp["case_no"], comp["adm_no"], comp["date"])
        else:
            key = (comp["case_no"], comp["adm_no"], comp["date"])
        groups.setdefault(key, []).append(idx)

    # ── B4 跨院区撞车守卫（与键形态无关：两种键下都必须成立）────────────────
    triple_groups = {}
    for idx, comp in enumerate(comps):
        if any(not comp[k] for k in DEDUP_COMPONENTS):
            continue
        triple_groups.setdefault((comp["case_no"], comp["adm_no"], comp["date"]),
                                 []).append(idx)
    collisions, collision_idx = [], set()
    for tkey, idxs in sorted(triple_groups.items()):
        hosp = sorted({comps[i]["hospital_std"] for i in idxs})
        if len(hosp) < 2:
            continue
        collisions.append({"triple_key": list(tkey), "hospitals": hosp,
                           "rows": list(idxs), "case_ids": [cids[i] for i in idxs]})
        collision_idx.update(idxs)
        C.err(f"跨院区撞车：病案号/住院号/入院日期 {list(tkey)} 相同但院区 {hosp} 不同"
              f" ⇒ **绝不合并**（round34 B4；本批编号跨院区无全局唯一性）")

    def nonempty(i):
        return sum(1 for v in table[i] if str(v) != "")

    drop_idx, conflicts = set(), []
    for key, idxs in sorted(groups.items()):
        eligible = [i for i in idxs if i not in collision_idx]
        if len(eligible) < 2:
            continue
        ranked = sorted(eligible, key=lambda i: (-nonempty(i), i))
        keep = ranked[0]
        for i in ranked[1:]:
            drop_idx.add(i)
            diff = []
            for pos, cid in enumerate(ids):
                a, b = str(table[keep][pos]), str(table[i][pos])
                if a != b:
                    diff.append({"column_id": cid, "kept_len": len(a), "dropped_len": len(b),
                                 "kept_empty": a == "", "dropped_empty": b == "",
                                 "kept_sha8": C.sha256_text(a)[:8],
                                 "dropped_sha8": C.sha256_text(b)[:8],
                                 "resolution": "kept-row value verbatim; dropped value discarded"})
            conflicts.append({"key": list(key), "key_form": key_form,
                              "key_hospital_std": comps[keep]["hospital_std"],
                              "keep_case_id": cids[keep],
                              "drop_case_id": cids[i], "keep_row": keep, "drop_row": i,
                              "keep_nonempty": nonempty(keep), "drop_nonempty": nonempty(i),
                              "keep_rule": "whole-row; most non-empty cells wins, "
                                           "tie -> manifest order; no field-level merge",
                              "differing_columns": diff})
    kept = [row for i, row in enumerate(table) if i not in drop_idx]
    dropped = [row for i, row in enumerate(table) if i in drop_idx]
    dropped_cids = [cids[i] for i in sorted(drop_idx)]
    stats = {"key_form": key_form,
             "key_shape": ("(标准化就诊医院,病案号,住院号,入院日期)" if key_form == "quad"
                           else "(病案号,住院号,入院日期)"),
             "date_part_rule": "yyyy-MM-dd from 标准化列 else 原文列; 时分不进键",
             "groups": len(conflicts), "dropped_rows": len(dropped),
             "kept_rows": len(kept),
             "key_empty_rows": len(incomplete),
             "cross_hospital_collision_groups": len(collisions),
             "cross_hospital_guard": d.get("cross_hospital_guard", "error"),
             "keep_rule": "whole-row; most non-empty cells wins, tie -> manifest order"}
    return kept, dropped, dropped_cids, {"stats": stats, "conflicts": conflicts,
                                         "incomplete": incomplete, "collisions": collisions}


def dedup_flag_rows(info: dict) -> list:
    """把去重阶段的 QC 事件写成 qc_flags.csv 的行（B3 / B4 不许静默）。

    落点在 06_table/qc_flags.csv（s6 在 s5 之后 ⇒ 只有 s6 知道键算成什么样；
    s5 的 flag 已经全部并进这个文件，所以它仍是「一份合并后的 flag 清单」）。
    """
    rows = []
    for it in info.get("incomplete", []):
        rows.append([it["case_id"], ",".join(it["missing"]), "dedup_key_incomplete",
                     f"row={it['row']};missing={','.join(it['missing'])};merged=no"])
    for c in info.get("collisions", []):
        for cid in c["case_ids"]:
            rows.append([cid, "hospital_std", "dedup_cross_hospital_collision",
                         f"triple_key={'|'.join(c['triple_key'])};"
                         f"hospitals={'|'.join(c['hospitals'])};"
                         f"rows={','.join(str(r) for r in c['rows'])};merged=no"])
    return rows


def dedup_exit_code(dedup_info: dict) -> int:
    """B4 跨院区撞车 = 硬断言。组数非 0 ⇒ 退出码 1（不合并由 _dedup_core 保证）。

    normalize.dedup.cross_hospital_guard（缺键 = error）：
      error ⇒ rc=1；warn ⇒ 只报警不改变退出码（只用于对照，正式跑批不许关）。
    """
    st = dedup_info.get("stats", {})
    n = int(st.get("cross_hospital_collision_groups", 0) or 0)
    if not n:
        return 0
    mode = str(st.get("cross_hospital_guard", "error"))
    if mode == "warn":
        C.warn(f"跨院区撞车 {n} 组（cross_hospital_guard=warn：只报警，对照跑法）"
               " —— 这些行**没有被合并**")
        return 0
    C.err(f"跨院区撞车 {n} 组：病案号/住院号/入院日期 相同但标准化就诊医院不同 "
          "⇒ 已拒绝合并这些行，s6 以 rc=1 退出（round34 B4）。"
          "明细见 06_table/_dedup_collisions.csv")
    return 1


def write_dedup_artifacts(out_dir: Path, dropped: list, dropped_cids: list,
                         dedup_info: dict, headers: list, cfg: dict) -> None:
    """丢弃行（全列）+ 冲突清单落盘。值本身不打印到 stdout（红线⑨ PII）。

    round34 B7：冲突列额外落 sha8（核对「保留行取了谁的值」而不泄露内容）；
    撞车组 / 键不完整行**即使没丢任何行也要落** —— 否则 B3/B4 的「不许静默」落空。
    """
    p = out_dir / "_dedup_dropped.csv"
    if dropped:
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["case_id"] + headers)
            for cid, row in zip(dropped_cids, dropped):
                w.writerow([cid] + list(row))
        C.set_output_mode(p, cfg["pii"])

    pc = out_dir / "_dedup_conflicts.csv"
    with open(pc, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key_form", "key_hospital_std", "key_case_no", "key_adm_no",
                    "key_date", "keep_case_id", "drop_case_id",
                    "keep_nonempty", "drop_nonempty", "column_id",
                    "kept_len", "dropped_len", "kept_empty", "dropped_empty",
                    "kept_sha8", "dropped_sha8", "resolution"])
        for c in dedup_info["conflicts"]:
            k = c["key"]
            kh, kc, ka, kd = ((k[0], k[1], k[2], k[3]) if c.get("key_form") == "quad"
                              else ("", k[0], k[1], k[2]))
            for dc in c["differing_columns"]:
                w.writerow([c.get("key_form", ""), kh, kc, ka, kd,
                            c["keep_case_id"], c["drop_case_id"],
                            c["keep_nonempty"], c["drop_nonempty"],
                            dc["column_id"], dc["kept_len"], dc["dropped_len"],
                            dc["kept_empty"], dc["dropped_empty"],
                            dc.get("kept_sha8", ""), dc.get("dropped_sha8", ""),
                            dc.get("resolution", "")])
    C.set_output_mode(pc, cfg["pii"])

    pl = out_dir / "_dedup_collisions.csv"
    if dedup_info.get("collisions"):
        with open(pl, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["triple_key_case_no", "triple_key_adm_no", "triple_key_date",
                        "hospitals", "row_index", "case_id", "merged"])
            for c in dedup_info["collisions"]:
                tk = c["triple_key"]
                for r, cid in zip(c["rows"], c["case_ids"]):
                    w.writerow([tk[0], tk[1], tk[2], "|".join(c["hospitals"]),
                                r, cid, "no"])
        C.set_output_mode(pl, cfg["pii"])

    pk = out_dir / "_dedup_key_incomplete.csv"
    if dedup_info.get("incomplete"):
        with open(pk, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["row_index", "case_id", "missing_components", "merged"])
            for it in dedup_info["incomplete"]:
                w.writerow([it["row"], it["case_id"], "|".join(it["missing"]), "no"])
        C.set_output_mode(pk, cfg["pii"])

    C.write_json(out_dir / "_dedup.json", dedup_info)
    st = dedup_info.get("stats", {})
    C.log(f"去重（键={st.get('key_form')}）：丢 {len(dropped)} 行 / 键不完整 "
          f"{st.get('key_empty_rows', 0)} 行 / 跨院区撞车 "
          f"{st.get('cross_hospital_collision_groups', 0)} 组（值不进 stdout）")


def read_existing_table(csv_path: Path, headers: list) -> list:
    """读回已写好的 wide.csv 数据行（不含表头）。文件不存在 → []。"""
    if not csv_path.exists():
        return []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    assert rows[0] == headers, "wide.csv 表头与 config/columns.yaml 不一致 —— 拒绝增量写"
    return rows[1:]


def flags_for(batch_dir: Path, cid: str) -> list:
    qj = batch_dir / "05_qc" / cid / "qc.json"
    if not qj.exists():
        return []
    return [[cid, fl["column_id"], fl["kind"], fl["detail"]]
            for fl in C.read_json(qj).get("flags", [])]


QC_CACHE_NAME = "_qc_flags_cache.jsonl"


def flags_cached(batch_dir: Path, out_dir: Path, cids: list, cfg: dict) -> list:
    """flags_for 的缓存版：qc.json 每例只读一次，结果按例缓存（append-only）。

    qc.json 是阶段⑤产物，本例跑完后不再变。旧写法每次增量把**全部已进表例**的
    qc.json 重开重解析一遍（N=3831 ⇒ 3831 次随机小读，冷缓存 0.79–1.29 s，
    见 workspace/round45/probes/g1_flags_cold.py），×3831 次增量且整段在
    run_steps.py:288 的 s6_lock 里串行 ⇒ 批内掉速主因（O(N)/例、整批 O(N²)）。
    缓存行带 (size, mtime_ns) ⇒ qc.json 被重写/删除 ⇒ 该行作废回读，不会静默用旧 flag。
    """
    path = out_dir / QC_CACHE_NAME
    cache: dict = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    C.warn(f"{path.name} 有坏行 ⇒ 相关例回读 qc.json")
                    continue
                cache[rec["case_id"]] = (rec.get("sz"), rec.get("mt"), rec.get("flags"))
    rows, fresh = [], []
    for cid in cids:
        try:
            stt = (batch_dir / "05_qc" / cid / "qc.json").stat()
            key = (stt.st_size, stt.st_mtime_ns)
        except OSError:
            key = (None, None)
        got = cache.get(cid)
        if got is not None and (got[0], got[1]) == key:
            rows.extend(got[2] or [])
            continue
        fl = flags_for(batch_dir, cid)
        fresh.append({"case_id": cid, "sz": key[0], "mt": key[1], "flags": fl})
        rows.extend(fl)
    if fresh:
        with open(path, "a", encoding="utf-8") as f:
            for rec in fresh:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        C.set_output_mode(path, cfg["pii"])
    return rows


DEFAULT_STALE_MIN = 30.0
UPSTREAM_STAGES = ("01_ocr", "02_text", "03_extract", "04_norm", "05_qc")


def case_newest_mtime(batch_dir: Path, cid: str):
    """本例上游产物的最新 mtime；没有任何上游产物 → (None, "")。"""
    newest, reached = None, ""
    for stage in UPSTREAM_STAGES:
        d = batch_dir / stage / cid
        if not d.is_dir():
            continue
        reached = stage
        for p in d.iterdir():
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return newest, reached


def settled_failure(batch_dir: Path, cid: str, stale_min: float, now: float):
    """该例能否判定为「已终态但没出阶段④产物」。判据保守，任一不满足 → None。

    ① 有上游产物（连 01_ocr 都没有 = 本批根本没跑过，不能算失败）
    ② 03_extract/<cid>/ 目录存在（阶段③至少开始过；否则可能还在队列里）
    ③ 本例上游产物最新 mtime 距今 > stale_min 分钟（还在写文件的例绝不越过）

    stale_min is None ⇒ 关闭放行（旧行为），一律返回 None。
    """
    if stale_min is None:
        return None
    newest, reached = case_newest_mtime(batch_dir, cid)
    if newest is None:
        return None
    if not (batch_dir / "03_extract" / cid).is_dir():
        return None
    idle = now - newest
    if idle < stale_min * 60.0:
        return None
    return {"case_id": cid, "reached": reached, "idle_sec": int(idle),
            "stale_min": stale_min}


def normalize_skipped(raw) -> list:
    """state.skipped 兼容旧格式（纯 case_id 字符串）并去重。"""
    out, seen = [], set()
    for s in raw or []:
        if isinstance(s, str):
            s = {"case_id": s, "reached": "", "idle_sec": None, "stale_min": None}
        if not isinstance(s, dict) or not s.get("case_id") or s["case_id"] in seen:
            continue
        seen.add(s["case_id"])
        out.append(s)
    return out


def state_payload(appended: list, skipped: list, deduped,
                  audit_off=None, audit_n=None) -> dict:
    out = {"appended": appended, "order": "manifest", "deduped": bool(deduped),
           "skipped": skipped}
    if audit_off is not None:
        out["audit_off"] = int(audit_off)
        out["audit_n"] = int(audit_n or 0)
    return out


def write_incomplete_cases(out_dir: Path, skipped, missing_norm: list,
                           not_processed: list, pending, cfg: dict) -> None:
    """没进宽表的例逐个列出。增量跑与全量跑写同一个文件（全量跑覆盖增量跑的陈旧内容）。"""
    inc_path = out_dir / "_incomplete_cases.csv"
    with open(inc_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "kind", "reached_stage", "idle_sec", "detail"])
        for s in skipped or []:
            sm = s.get("stale_min")
            w.writerow([s["case_id"], "stale_failed", s.get("reached", ""),
                        s.get("idle_sec", ""),
                        f"判定终态失败（>{sm} 分钟无上游产物），未进宽表"])
        for cid in missing_norm:
            w.writerow([cid, "missing_stage4", "03_extract", "", "阶段③有产物但阶段④缺失"])
        for cid in not_processed:
            w.writerow([cid, "not_processed", "", "", "阶段①②③未跑（本批未处理）"])
        for cid in pending or []:
            w.writerow([cid, "stage4_pending", "03_extract", "",
                        "阶段③有产物、阶段④未出且未判终态 ⇒ 本轮未定，终局判定交给全量跑"])
    C.set_output_mode(inc_path, cfg["pii"])


def incomplete_qc_flags(missing_norm: list, not_processed: list) -> list:
    """同一批例也要进 qc_flags.csv —— 否则全量跑之后它们对 QC 完全静默（红线⑦）。"""
    rows = []
    for cid in missing_norm:
        rows.append([cid, "", "missing_stage4", "阶段③有产物但阶段④缺失，未进宽表"])
    for cid in not_processed:
        rows.append([cid, "", "not_processed", "阶段①②③未跑（本批未处理），未进宽表"])
    return rows


XLSX_NUMERIC_HEADERS = ("序号", "住院天数（天）")


EXTRACT_SOURCES = ("frontpage", "first_progress")


def empty_clinical_cids(table: list, cids: list, ids: list, cols: dict) -> list:
    """宽表里的空壳行：columns.yaml 里 source ∈ {frontpage, first_progress} 的列全空。
    派生列有值不代表抽到了东西（实测 web-20261017-183205：2434 行里 92 行空壳）。"""
    src = {c["id"]: c.get("source") for c in cols["columns"]}
    keep = [i for i, cid in enumerate(ids) if src.get(cid) in EXTRACT_SOURCES]
    if not keep:
        return []
    out = []
    for cid, row in zip(cids, table):
        if not any(row[i] is not None and str(row[i]).strip()
                   for i in keep if i < len(row)):
            out.append(cid)
    return out


def write_empty_cases(out_dir: Path, cids: list, cfg: dict) -> None:
    """空壳例单独一份清单（与 _incomplete_cases.csv 不同：这些例**在**宽表里）。"""
    p = out_dir / "_empty_cases.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "kind", "detail"])
        for cid in cids:
            w.writerow([cid, "empty_case",
                        "抽取列（首页/首程来源）全空 ⇒ 先查 03_extract/frontpage.json"])
    C.set_output_mode(p, cfg["pii"])


def write_xlsx(table: list, headers: list, xlsx_path: Path, cfg: dict) -> bool:
    """宽表 xlsx：除 序号 / 住院天数 外**全部按文本写**，并显式把 number_format 设成 "@"。

    不设文本格式 ⇒ Excel/WPS 把住院号、病案号、身份证号、电话当数值 ⇒ 显示成科学计数法、
    丢前导零、18 位身份证号掉精度（本批实测 493 个住院号带前导 0、128 个超过 9 位）。
    wide.csv 保持干净（不加 ="…" 这种 Excel 专用引号，pandas / 脚本读它不受影响）。
    """
    try:
        from openpyxl import Workbook
    except Exception as e:                                        # noqa: BLE001
        C.err(f"openpyxl 不可用，xlsx 跳过：{type(e).__name__}: {e}")
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = "wide"
    ws.append(headers)
    num_idx = {i for i, hh in enumerate(headers) if hh in XLSX_NUMERIC_HEADERS}
    for row in table:
        out = []
        for i, v in enumerate(row):
            if v == "" or v is None:
                out.append(None)
            elif i in num_idx:
                try:
                    out.append(int(str(v).strip()))
                except ValueError:
                    out.append(str(v))
            else:
                out.append(str(v))
        ws.append(out)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if (c.column - 1) not in num_idx:
                c.number_format = "@"
    wb.save(xlsx_path)
    C.set_output_mode(xlsx_path, cfg["pii"])
    return True


def write_derived(batch_dir: Path, out_dir: Path, table: list, flag_rows: list,
                  headers: list, cfg: dict, cols: dict, csv_path: Path,
                  missing_norm: list, not_processed: list, dedup_info: dict = None,
                  skipped: list = None, pending: list = None,
                  empty_cids: list = None, audit_off: int = 0,
                  audit_n: int = 0) -> tuple:
    """wide.xlsx / qc_flags.csv / audit.jsonl / _manifest.json —— 每次增量后整体重建。
    （wide.csv 才是追加写；这几个是派生物，重写比增量简单且不会错位。）"""
    flag_path = out_dir / "qc_flags.csv"
    with open(flag_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "column_id", "kind", "detail"])
        w.writerows(flag_rows)
    C.set_output_mode(flag_path, cfg["pii"])

    xlsx_path = out_dir / "wide.xlsx"
    # round43 R4：这里写的是本函数形参 table（调用方传的就是全表）。
    # 旧写法 table_all 是 run_incremental 里的局部名 ⇒ 每次增量 s6 必 NameError，
    # 后果是整轮跑批期间 wide.xlsx / audit.jsonl / _manifest.json / _incomplete_cases.csv
    # 一个都不落盘（wide.csv 是追加写，在崩之前已写完 ⇒ 看起来「只有 csv 没有 xlsx」）。
    if not write_xlsx(table, headers, xlsx_path, cfg):
        xlsx_path = None

    audit_in = batch_dir / "_audit.jsonl"
    audit_out = out_dir / "audit.jsonl"
    n_audit = 0
    # round45（G1）：_audit.jsonl 是 append-only（common.py:135 用 "a"），旧写法每次增量
    # 都从头 json.loads→scrub→json.dumps 重写整份 ⇒ O(N)/例、整批 O(N²)。
    # 实测 N=3830 单次 4.73/5.16s（workspace/round45/probes/g1_s6_incr_cost.py 两趟），
    # ×3817 次且整段在 run_steps.py:273 的 s6_lock 里串行 ⇒ 批内掉速主因。
    # 改法：state 记字节偏移，只 scrub 偏移之后的**整行**并 append；半行留到下轮。
    # 安全阀：没有偏移 / 偏移超过当前大小 / 产物不在 ⇒ 退回全量重建。
    resume = audit_off if (0 < audit_off <= audit_in.stat().st_size
                           and audit_out.exists()) else 0
    n_audit = audit_n if resume else 0
    audit_consumed = resume
    if audit_in.exists():
        with open(audit_in, "rb") as fin, open(audit_out, "ab" if resume else "wb") as fout:
            if resume:
                fin.seek(resume)
            while True:
                raw = fin.readline()
                if not raw or not raw.endswith(b"\n"):
                    break                       # 半行：本轮不吞，偏移不推进
                audit_consumed += len(raw)
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                fout.write((json.dumps(
                    C.scrub_for_log(json.loads(line), cols, cfg["pii"]),
                    ensure_ascii=False) + "\n").encode("utf-8"))
                n_audit += 1
        C.set_output_mode(audit_out, cfg["pii"])
    else:
        C.warn("没有 _audit.jsonl，审计清单为空")

    write_incomplete_cases(out_dir, skipped, missing_norm, not_processed, pending, cfg)
    write_empty_cases(out_dir, empty_cids or [], cfg)

    C.write_json(out_dir / "_manifest.json", {
        "columns": len(headers), "rows": len(table),
        "headers": headers, "columns_yaml_version": cols["version"],
        "cases_missing_stage4": missing_norm,
        "cases_not_processed": not_processed, "n_audit_records": n_audit,
        "cases_stale_failed": [s["case_id"] for s in (skipped or [])],
        "cases_stage4_pending": pending or [],
        "cases_empty_clinical": empty_cids or [],
        "wide_csv": str(csv_path), "wide_xlsx": str(xlsx_path or ""),
        "dropped_columns": drop_ids_of(cfg, headers, cols),
        "dedup": (dedup_info or {}).get("stats", {})})
    return audit_consumed, n_audit


def drop_ids_of(cfg: dict, headers_used: list, cols: dict) -> list:
    """_manifest.json 里记清楚「本次删掉了哪些 columns.yaml 里的列」（可审计）。"""
    all_headers = [c["header"] for c in cols["columns"]]
    return [h for h in all_headers if all_headers.count(h) - headers_used.count(h) > 0]


def build_rows_from_norm(batch_dir: Path, cids: list, ids: list):
    """从 04_norm/<case>/fields.json 重建这些例的行（顺序 = 传入顺序）。

    返回 (table, kept_cids)：kept_cids 是**真正出了行的例**，不是 cids 的前 len(table) 个。
    （round42n 修：旧写法 `cids[:len(table)]` 在传入整份 manifest 时会把没产物的例
    错标成已进表 ⇒ state.appended 与 wide.csv 行错位。）
    """
    table, kept = [], []
    for cid in cids:
        fj = batch_dir / "04_norm" / cid / "fields.json"
        if not fj.exists():
            continue
        fields = C.read_json(fj).get("fields", {})
        table.append([cell_value(fields, c) for c in ids])
        kept.append(cid)
    return table, kept


def run_incremental(batch_dir: Path, out_dir: Path, rows: list, headers: list,
                    ids: list, cfg: dict, cols: dict, csv_path: Path,
                    state_path: Path, drop_ids: list, no_dedup: bool,
                    dedup_key_form: str = None,
                    stale_min: float = DEFAULT_STALE_MIN) -> int:
    """按 manifest 顺序把**新增**的例追加到 wide.csv。

    顺序保证：只有「manifest 里排在前面、且已进表或已判定终态失败」的例都处理完，
    才允许追加后面的例 —— 一旦发现前面有例还没出 04_norm 产物、且还不能判定终态失败，
    就停手（不越过它写），这样追加顺序恒等于 manifest 顺序。
    判定为终态失败的例（settled_failure）不追加行、但记账后放行 —— 否则一个永远出不了
    fields.json 的例会把后面所有例永久堵死（round42n 实测：1 例卡死 ⇒ 1476 例进不了表）。
    """
    st = C.read_json(state_path) if state_path.exists() else {}
    appended = st.get("appended", [])
    skipped = normalize_skipped(st.get("skipped"))
    audit_off0, audit_n0 = int(st.get("audit_off") or 0), int(st.get("audit_n") or 0)
    # 1.4 删列：表头/列 id 在进表前就裁掉 ⇒ wide.csv 从头到尾只有裁后的列
    headers = [h for h, cid in zip(headers, ids) if cid not in drop_ids]
    ids = [cid for cid in ids if cid not in drop_ids]

    all_cids = [r["case_id"] for r in rows]
    revived = [s["case_id"] for s in skipped
               if (batch_dir / "04_norm" / s["case_id"] / "fields.json").exists()]
    rebuilt = False
    if revived:
        # 上一轮判为终态失败、这轮出了产物 ⇒ 直接追加会错位 ⇒ 按 manifest 顺序整体重建
        C.warn(f"上一轮判为终态失败的 {len(revived)} 例现在出了阶段④产物 —— "
               "按 manifest 顺序整体重建 wide.csv")
        skipped = [s for s in skipped if s["case_id"] not in set(revived)]
        table, table_cids = build_rows_from_norm(batch_dir, all_cids, ids)
        appended = list(table_cids)
        rebuilt = True
    elif st.get("deduped"):
        # wide.csv 已是去重后的成品（行数 < 例数）⇒ 从 04_norm 重建，别在成品上追加
        C.warn("上一轮已 finalize 去重；本轮从 04_norm 重建全表后再追加"
              "（批次 finalize 后又增长 ⇒ 建议改跑非增量模式）")
        table, table_cids = build_rows_from_norm(batch_dir, appended, ids)
    else:
        table = read_existing_table(csv_path, headers)
        table_cids = list(appended)
        if len(table) != len(appended):
            C.err(f"_incremental_state.json 说已追加 {len(appended)} 例，"
                  f"但 wide.csv 只有 {len(table)} 行 —— 从 04_norm 重建")
            table, table_cids = build_rows_from_norm(batch_dir, appended, ids)
    if not table and not csv_path.exists():
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(headers)
        C.set_output_mode(csv_path, cfg["pii"])
    appended_set = set(appended)

    skipped_set = {s["case_id"] for s in skipped}
    now = time.time()
    gate_cid = None
    new_rows, new_cids = [], []
    missing_norm, not_processed, pending = [], [], []
    for r in rows:
        cid = r["case_id"]
        fj = batch_dir / "04_norm" / cid / "fields.json"
        if cid in appended_set:
            continue
        if cid in skipped_set:
            # 上一轮已判终态失败 ⇒ 本轮不重复记账；它后来出了产物走上面的 revived 重建
            continue
        if fj.exists():
            fields = C.read_json(fj).get("fields", {})
            new_rows.append([cell_value(fields, c) for c in ids])
            new_cids.append(cid)
            continue
        why = settled_failure(batch_dir, cid, stale_min, now)
        if why is not None:
            # 终态失败：放行但不追加行，必须记账（state.skipped + _incomplete_cases.csv）
            skipped.append(why)
            skipped_set.add(cid)
            continue
        if (batch_dir / "03_extract" / cid / "frontpage.json").exists():
            # 放行开着 ⇒ 能走到这里的例只可能是「还在写」，不能断言断链（终局判定交给
            # 全量跑 s6）；--no-stale-skip 对照模式维持旧口径 = 直接算断链。
            if stale_min is None:
                missing_norm.append(cid)
            else:
                pending.append(cid)
        else:
            not_processed.append(cid)
        gate_cid = cid
        break                 # 顺序闸门：不越过还在跑 / 无法判定终态的例

    if rebuilt:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(table)
        C.set_output_mode(csv_path, cfg["pii"])
        C.write_json(state_path, state_payload(appended, skipped, st.get("deduped"),
                                               audit_off0, audit_n0))
    if new_rows:
        with open(csv_path, "a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(new_rows)
        C.set_output_mode(csv_path, cfg["pii"])
        appended += new_cids
        C.write_json(state_path, state_payload(appended, skipped, st.get("deduped"),
                                               audit_off0, audit_n0))

    table_all = table + new_rows
    cids_all = table_cids + new_cids
    empty_cids = empty_clinical_cids(table_all, cids_all, ids, cols)
    flag_rows = flags_cached(batch_dir, out_dir, appended, cfg)
    for s in skipped:
        reached = s.get("reached") or "未知"
        idle = s.get("idle_sec")
        flag_rows.append([s["case_id"], "", "s6_skipped_stale",
                          f"上游停在 {reached}，{idle} s 无上游产物 ⇒ 判定终态失败，未进宽表"])
    flag_rows += incomplete_qc_flags(missing_norm, not_processed)
    # 1.5 去重是**全批 finalize 步**：只有 manifest 里所有例都进表了才做，
    # 否则键相同的两行可能有一行还没追加，去重结果会随进度漂移。
    dedup_info = {}
    rows_set = set(all_cids)
    n_skipped_in = len([s for s in skipped if s["case_id"] in rows_set])
    if not no_dedup and len(appended) + n_skipped_in >= len(rows):
        kept, dropped, dropped_cids, dedup_info = dedup_table(
            table_all, cids_all, ids, cfg, dedup_key_form)
        if dropped:
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(headers)
                w.writerows(kept)
            C.set_output_mode(csv_path, cfg["pii"])
            table_all = kept
        write_dedup_artifacts(out_dir, dropped, dropped_cids, dedup_info, headers, cfg)
        flag_rows += dedup_flag_rows(dedup_info)
        empty_cids = [c for c in empty_cids if c not in set(dropped_cids)]
        C.write_json(state_path, state_payload(appended, skipped, True,
                                               audit_off0, audit_n0))
    elif not no_dedup:
        C.warn(f"去重未执行：已进表 {len(appended)} 例 + 判定终态失败 {n_skipped_in} 例"
               f" / manifest 共 {len(rows)} 例，未到 finalize 条件"
               "（全部例要么进表要么判终态失败才去重，避免结果随进度漂移）")
    flag_rows += [[c, "", "empty_case", "整例零临床字段（抽取列全空）"] for c in empty_cids]
    audit_consumed, n_audit_new = write_derived(
        batch_dir, out_dir, table_all, flag_rows, headers, cfg, cols,
        csv_path, missing_norm, not_processed, dedup_info, skipped, pending,
        empty_cids, audit_off=audit_off0, audit_n=audit_n0)
    C.write_json(state_path, state_payload(appended, skipped,
                                           bool(st.get("deduped")) or bool(dedup_info),
                                           audit_consumed, n_audit_new))
    gate = gate_cid or "无"
    C.log(f"增量宽表：本次追加 {len(new_cids)} 例 → 累计 {len(table_all)} 行 "
          f"× {len(headers)} 列；跳过（终态失败）{n_skipped_in} 例；"
          f"阶段④未定 {len(pending)} 例；闸门例={gate} → {csv_path}")
    if empty_cids:
        C.warn(f"{len(empty_cids)} 例整例零临床字段（空壳行）⇒ {out_dir}/_empty_cases.csv")
    return 1 if missing_norm else dedup_exit_code(dedup_info)



def cell_value(fields: dict, cid: str):
    f = fields.get(cid, {})
    v = f.get("value")
    return "" if v is None else v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="阶段⑥ 宽表生成")
    ap.add_argument("--batch", required=True)
    C.add_case_args(ap)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--incremental", action="store_true",
                    help="增量写：只把**新增**的例按 manifest 顺序追加到 wide.csv，"
                         "不重写整表（流水线跑法用；产物与一次性跑逐字节相同）")
    ap.add_argument("--keep-hospital-column", action="store_true",
                    help="对照跑法：不删 就诊医院 列（输出回到 24 列）。正式跑批不许用")
    ap.add_argument("--no-dedup", action="store_true",
                    help="对照跑法：完全不去重。正式跑批不许用")
    ap.add_argument("--stale-min", type=float, default=None,
                    help="增量模式：某例上游产物停更超过这么多分钟 ⇒ 判定终态失败、"
                         "放行后面的例（默认 30；config:table.incremental_stale_min 可覆盖）")
    ap.add_argument("--no-stale-skip", action="store_true",
                    help="关闭终态失败放行 = round42n 之前的旧行为（一个卡死的例"
                         "会永久堵死后面所有例）。只用于对照")
    ap.add_argument("--dedup-key", choices=list(DEDUP_KEY_FORMS), default=None,
                    help="判重键形态：quad=(标准化就诊医院,病案号,住院号,入院日期)【默认/交付】；"
                         "triple=(病案号,住院号,入院日期)【旧键，只用于 round34 B6 对照】")
    a = ap.parse_args(argv)
    cfg = C.load_pipeline()
    cols = C.load_columns()
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    out_dir = batch_dir / STAGE
    out_dir.mkdir(parents=True, exist_ok=True)

    from wide import s1_ocr as S1
    rows = S1.read_manifest(batch_dir / "00_index" / "manifest.tsv")
    want = C.wanted_cases(a.case, a.case_file)
    if want:
        rows = [r for r in rows if r["case_id"] in want]

    headers_all = [c["header"] for c in cols["columns"]]
    ids_all = [c["id"] for c in cols["columns"]]
    # 1.4 删列（normalize.drop_output_columns，缺键=开）→ 输出 23 列
    drop_ids = resolve_drop_ids(cfg, a.keep_hospital_column)
    unknown = [d for d in drop_ids if d not in ids_all]
    if unknown:
        C.err(f"drop_output_columns 里有 columns.yaml 不存在的列 id：{unknown} —— 忽略这些项")
    drop_ids = [d for d in drop_ids if d in ids_all]
    for key_cid in KEY_SOURCE_IDS:
        if key_cid in drop_ids:
            C.err(f"去重键依赖的列 {key_cid} 被 drop_output_columns 删掉了 —— "
                  f"本次不去重（否则键会算错），请改配置")
            drop_ids.remove(key_cid)
    headers = [h for h, cid in zip(headers_all, ids_all) if cid not in drop_ids]
    ids = [cid for cid in ids_all if cid not in drop_ids]
    if drop_ids:
        C.log(f"删列：{drop_ids} → 输出 {len(ids)} 列（columns.yaml 有 {len(ids_all)} 列）")
    csv_path = out_dir / "wide.csv"
    state_path = out_dir / "_incremental_state.json"

    if a.incremental:
        stale_min = a.stale_min
        if stale_min is None:
            stale_min = (cfg.get("table") or {}).get("incremental_stale_min",
                                                     DEFAULT_STALE_MIN)
        if a.no_stale_skip:
            stale_min = None
            C.warn("已关闭终态失败放行（--no-stale-skip）：卡死的例会堵死后面所有例")
        elif stale_min < 0:
            C.warn(f"--stale-min={stale_min} 非法 ⇒ 用默认 {DEFAULT_STALE_MIN}")
            stale_min = DEFAULT_STALE_MIN
        C.log(f"增量放行阈值：stale_min={stale_min}")
        return run_incremental(batch_dir, out_dir, rows, headers, ids, cfg, cols,
                              csv_path, state_path, drop_ids, a.no_dedup, a.dedup_key,
                              stale_min)

    table, table_cids, missing_norm, not_processed, flag_rows = [], [], [], [], []
    for r in rows:
        fj = batch_dir / "04_norm" / r["case_id"] / "fields.json"
        if not fj.exists():
            # 真断链 vs 不在本批范围：阶段③有产物才算断链
            if (batch_dir / "03_extract" / r["case_id"] / "frontpage.json").exists():
                missing_norm.append(r["case_id"])
            else:
                not_processed.append(r["case_id"])
            continue
        fields = C.read_json(fj).get("fields", {})
        table.append([cell_value(fields, cid) for cid in ids])
        table_cids.append(r["case_id"])
        qj = batch_dir / "05_qc" / r["case_id"] / "qc.json"
        if qj.exists():
            for fl in C.read_json(qj).get("flags", []):
                flag_rows.append([r["case_id"], fl["column_id"], fl["kind"], fl["detail"]])

    if not table:
        C.err(f"宽表为空（0 行）—— 不覆盖已有 {out_dir / 'wide.csv'}。"
              f"本批 {len(rows)} 例里：阶段③有产物但阶段④缺失 {len(missing_norm)} 例"
              f"（{missing_norm[:5]}），阶段①②③未跑 {len(not_processed)} 例。"
              f"先确认上游是否真的跑过（04_norm/<case>/fields.json）。")
        return 1

    empty_cids = empty_clinical_cids(table, table_cids, ids, cols)

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(table)
    C.set_output_mode(csv_path, cfg["pii"])

    # 1.5 去重（round34 Part B：交付键 = 四元组；键不完整禁止合并；跨院区撞车绝不合并）
    dedup_info = {}
    if not a.no_dedup:
        kept, dropped, dropped_cids, dedup_info = dedup_table(table, table_cids, ids, cfg,
                                                              a.dedup_key)
        if dropped:
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(headers)
                w.writerows(kept)
            C.set_output_mode(csv_path, cfg["pii"])
            table = kept
        write_dedup_artifacts(out_dir, dropped, dropped_cids, dedup_info, headers, cfg)
        flag_rows += dedup_flag_rows(dedup_info)
        empty_cids = [c for c in empty_cids if c not in set(dropped_cids)]

    flag_rows += incomplete_qc_flags(missing_norm, not_processed)
    flag_rows += [[c, "", "empty_case", "整例零临床字段（抽取列全空）"] for c in empty_cids]
    flag_path = out_dir / "qc_flags.csv"
    with open(flag_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "column_id", "kind", "detail"])
        w.writerows(flag_rows)
    C.set_output_mode(flag_path, cfg["pii"])

    xlsx_path = out_dir / "wide.xlsx"
    if not write_xlsx(table, headers, xlsx_path, cfg):
        xlsx_path = None

    # 审计汇总：把 _audit.jsonl 里三类哈希整理成一份可核对的清单
    audit_in = batch_dir / "_audit.jsonl"
    audit_out = out_dir / "audit.jsonl"
    n_audit = 0
    if audit_in.exists():
        with open(audit_in, "r", encoding="utf-8") as fin, \
             open(audit_out, "w", encoding="utf-8") as fout:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                fout.write(json.dumps(C.scrub_for_log(rec, cols, cfg["pii"]),
                                      ensure_ascii=False) + "\n")
                n_audit += 1
        C.set_output_mode(audit_out, cfg["pii"])
    else:
        C.warn("没有 _audit.jsonl，审计清单为空")

    write_incomplete_cases(out_dir, None, missing_norm, not_processed, None, cfg)
    write_empty_cases(out_dir, empty_cids, cfg)
    C.write_json(out_dir / "_manifest.json", {
        "columns": len(headers), "rows": len(table),
        "headers": headers, "columns_yaml_version": cols["version"],
        "cases_missing_stage4": missing_norm,
        "cases_not_processed": not_processed, "n_audit_records": n_audit,
        "cases_stale_failed": [], "cases_stage4_pending": [],
        "cases_empty_clinical": empty_cids,
        "wide_csv": str(csv_path), "wide_xlsx": str(xlsx_path or ""),
        "dropped_columns": [h for h, cid in zip(headers_all, ids_all) if cid in drop_ids],
        "dedup": dedup_info.get("stats", {})})
    C.log(f"宽表：{len(table)} 行 × {len(headers)} 列 → {csv_path}")
    if missing_norm:
        C.err(f"{len(missing_norm)} 例阶段③有产物但缺阶段④产物，未进表：{missing_norm[:5]}")
    if not_processed:
        C.warn(f"{len(not_processed)} 例本批未跑上游（①②③），不进表（不算失败）："
               f"{not_processed[:5]}")
    if empty_cids:
        C.warn(f"{len(empty_cids)} 例整例零临床字段（空壳行）⇒ {out_dir}/_empty_cases.csv")
    return 1 if missing_norm else dedup_exit_code(dedup_info)


if __name__ == "__main__":
    raise SystemExit(main())
