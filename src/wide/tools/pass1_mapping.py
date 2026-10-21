#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/pass1_mapping.py —— 第一遍：模板列名 ↔ 首页实际标签写法（先给人看，不回填宽表）。

输入   run/wide/<batch>/00_index/page_index.json      首页页号（tools/locate_pages.py 产出）
       run/wide/<batch>/02_text/<case>/page_NNNN.json 阶段②的块/单元格矩阵
       config/columns.yaml + config/label_vocab.yaml
输出   config/field_mapping.yaml   status: draft（人工确认后才给阶段④用）
       docs/column_mapping.md      模板列名 → 首页实际标签 → 命中样本数 → 示例值（PII 打码）
       run/wide/<batch>/_audit.jsonl

为什么单独一遍（任务书第四节）：各院区首页写法不同（「住院号」也可能写「住院病历号」「病案号」）。
先统计**首页上实际出现的写法**，再按映射回填，映射表落盘保留来源与版本（红线③）。

不猜：命中数只统计**实际出现**的写法；没出现的写法不会出现在映射里。
首页上出现、但模板没有对应列的写法 → 单独列出（含 LLM 提议，标 proposed，等人裁决）。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from wide import common as C  # noqa: E402
from wide import s1_ocr as S  # noqa: E402

# 「像标签」的单元格：短、以中日韩字符为主、不含数字（含数字的多半是值不是标签）
LABELISH = re.compile(r"^[一-鿿A-Za-z0-9()（）/、．.·\- ]{1,20}$")
HAS_DIGIT = re.compile(r"[0-9０-９]")


# --------------------------------------------------------------- 别名 → 模板列
def build_alias_index(cols_cfg: dict, vocab: dict) -> tuple[dict, dict, list]:
    """label 写法 → {column_id}；canonical → {column_id}；冲突列表。"""
    label2cols: dict[str, set] = defaultdict(set)
    canon2cols: dict[str, set] = defaultdict(set)
    for col in cols_cfg["columns"]:
        for lab in col.get("candidate_labels") or []:
            lab = str(lab).strip()
            if lab:
                label2cols[lab].add(col["id"])
                canon2cols[lab].add(col["id"])
    # label_vocab 的 aliases 归到 canonical，再顺着 canonical 找到列
    for item in vocab.get("labels") or []:
        canon = str(item["canonical"]).strip()
        for a in [canon, *(str(x).strip() for x in (item.get("aliases") or []))]:
            if a and canon2cols.get(canon):
                label2cols[a] |= canon2cols[canon]
    collisions = [{"label": lab, "columns": sorted(cs)}
                  for lab, cs in sorted(label2cols.items()) if len(cs) > 1]
    return dict(label2cols), dict(canon2cols), collisions


def alias_pattern(label2cols: dict) -> re.Pattern:
    alts = sorted(label2cols, key=len, reverse=True)      # 长写法优先（联系人电话 > 电话）
    return re.compile("(?P<label>" + "|".join(re.escape(a) for a in alts) + ")")


# --------------------------------------------------------------- 采集
# 两种 OCR 变体都采（风险①：版面模式把首页表格合并成大块，纯文本模式保留勾选编号）。
# 先采 plain —— 它的「标签 值」粘连更干净，示例值更好看。
VARIANTS = (("plain", "blocks_plain"), ("layout", "blocks"))


def _entry() -> dict:
    return {"hits": 0, "cases": set(), "examples": [], "_seen": set(),
            "empty": 0, "by_variant": {}}


def harvest_text(text: str, pat: re.Pattern, out: dict, case: str, variant: str) -> None:
    """一段文本 → 已知写法命中（带值）+ 未知但像标签的片段（不带值）。"""
    ms = list(pat.finditer(text))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        val = text[m.end():end].strip(" :：\t").split("\n")[0].strip()
        e = out["known"].setdefault(m.group("label"), _entry())
        e["hits"] += 1
        e["by_variant"][variant] = e["by_variant"].get(variant, 0) + 1
        e["cases"].add(case)
        if val:
            if val not in e["_seen"] and len(e["examples"]) < 3:
                e["_seen"].add(val)
                e["examples"].append(val[:40])
        else:
            e["empty"] += 1
    # 未知写法：整段本身像标签，且没有任何已知命中
    t = text.strip()
    if not ms and t and len(t) <= 20 and LABELISH.match(t) and not HAS_DIGIT.search(t):
        e = out["unknown"].setdefault(t, _entry())
        e["hits"] += 1
        e["by_variant"][variant] = e["by_variant"].get(variant, 0) + 1
        e["cases"].add(case)


def harvest_case(case: str, pages: list[dict], pat: re.Pattern, out: dict) -> int:
    for pg in pages:
        for variant, key in VARIANTS:
            for b in pg.get(key, []) or []:
                cells_any = False
                for row in (b.get("cells") or []):
                    cells_any = True
                    for cell in row:
                        cell = (cell or "").strip()
                        if cell:
                            harvest_text(cell, pat, out, case, variant)
                if not cells_any:
                    for line in (b.get("text") or "").split("\n"):
                        for seg in re.split(r"\s{2,}|\|", line.strip()):
                            if seg.strip():
                                harvest_text(seg, pat, out, case, variant)
    return len(pages)


# --------------------------------------------------------------- LLM 提议（只提议，不落地）
def propose_unmapped(cfg_llm: dict, unmapped: list[str], cols_cfg: dict,
                     audit: C.Audit) -> dict:
    headers = [f"{i + 1}.{c['header']}" for i, c in enumerate(cols_cfg["columns"])]
    sys_p = ("你在做病历字段映射。只输出 JSON 对象，不要解释。"
             "对每个给定标签，从给定候选列里选**最多一个**最贴切的列；"
             "没有贴切的就输出 null。不确定也输出 null，不要猜。")
    user = ("候选列（编号.列名）：\n" + "\n".join(headers) +
            "\n\n首页上出现、尚未映射的标签写法（每行一个）：\n" +
            "\n".join(unmapped) +
            '\n\n输出形如 {"标签写法": "编号.列名" 或 null}。')
    sys_sha, user_sha = C.sha256_text(sys_p), C.sha256_text(user)
    content, e, raw_sha, finish = _llm(cfg_llm, sys_p, user)
    audit.emit(kind="prompt_pass1_propose", stage="00_pass1",
               system_sha256=sys_sha, prompt_sha256=user_sha,
               response_sha256=raw_sha, content_sha256=C.sha256_text(content),
               model=cfg_llm["model"], temperature=cfg_llm["temperature"],
               finish_reason=finish, error=e)
    if not content:
        return {}
    from wide.s3_extract import parse_json_loose
    parsed, pe = parse_json_loose(content)
    if pe:
        audit.emit(kind="pass1_propose_parse_error", stage="00_pass1", error=pe)
        return {}
    return {str(k): v for k, v in (parsed or {}).items() if isinstance(v, str)}


def _llm(cfg_llm: dict, system: str, user: str) -> tuple[str, str, str]:
    import json as _j
    payload = {"model": cfg_llm["model"], "temperature": float(cfg_llm["temperature"]),
               "top_p": float(cfg_llm.get("top_p", 1.0)),
               "max_tokens": int(cfg_llm.get("max_tokens", 4096)),
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    url = cfg_llm["base_url"].rstrip("/") + cfg_llm["endpoint"]
    status, js, e = C.http_json(url, payload, int(cfg_llm["timeout_s"]),
                                int(cfg_llm.get("retries", 0)))
    raw = C.canon_json(js) if js is not None else ""
    if js is None:
        C.warn(f"LLM 失败 status={status} {e}")
        return "", f"status={status} {e}", C.sha256_text(raw), ""
    try:
        content = js["choices"][0]["message"].get("content") or ""
        finish = js["choices"][0].get("finish_reason") or ""
    except (KeyError, IndexError, TypeError) as exc:
        return "", f"响应结构异常 {type(exc).__name__}", C.sha256_text(raw), ""
    if finish == "length":
        return "", f"finish_reason=length（max_tokens={payload['max_tokens']}）", \
            C.sha256_text(raw), finish
    return content, "", C.sha256_text(raw), finish


# --------------------------------------------------------------- 输出
def mask_example(col: dict, value: str, cfg_pii: dict) -> str:
    if col.get("pii"):
        return C.mask_value(value, col["id"], cfg_pii)
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="第一遍：模板列 ↔ 首页实际标签")
    ap.add_argument("--batch", required=True)
    ap.add_argument("--case", action="append")
    ap.add_argument("--case-file", default="", help="从文件读 case_id，一行一个")
    ap.add_argument("--index", default="", help="定位文件（默认 run/wide/<batch>/00_index/page_index.json）")
    ap.add_argument("--out-config", default="/home/ubuntu/medrec/config/field_mapping.yaml")
    ap.add_argument("--out-doc", default="/home/ubuntu/medrec/docs/column_mapping.md")
    ap.add_argument("--no-llm", action="store_true", help="不让 LLM 提议未映射写法")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    cfg = C.load_pipeline()
    cols_cfg = C.load_columns()
    vocab = C.load_yaml(C.CONFIG_DIR / "label_vocab.yaml")
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    audit = C.Audit(batch_dir / "_audit.jsonl", "00_pass1")

    # 注意：Path("") == Path(".") 且为真值 —— 必须先判空串再构造 Path
    idx_cfg = str(cfg["locator"].get("index_file") or "").strip()
    ip = Path(a.index) if a.index else (Path(idx_cfg) if idx_cfg
                                        else batch_dir / "00_index" / "page_index.json")
    if not ip.exists():
        C.err(f"缺定位文件 {ip}（先跑 tools/locate_pages.py）")
        return 2
    index = C.read_json(ip).get("cases", {})

    rows = S.read_manifest(batch_dir / "00_index" / "manifest.tsv")
    want = list(a.case or [])
    if a.case_file:
        want += [x.strip() for x in Path(a.case_file).read_text(encoding="utf-8").splitlines()
                 if x.strip()]
    if want:
        rows = [r for r in rows if r["case_id"] in set(want)]

    label2cols, canon2cols, collisions = build_alias_index(cols_cfg, vocab)
    pat = alias_pattern(label2cols)
    out = {"known": {}, "unknown": {}}

    n_cases, n_no_fp = 0, []
    for r in rows:
        case = r["case_id"]
        entry = index.get(case) or {}
        fp = list(entry.get("frontpage_pages") or [])
        if not fp:
            n_no_fp.append(case)
            continue
        pages = []
        for p in fp:
            pj = batch_dir / "02_text" / case / f"page_{p + 1:04d}.json"
            if pj.exists():
                rec = C.read_json(pj)
                if rec.get("ocr_status") == "ok" and rec.get("page_kind") == "ok":
                    pages.append(rec)
        if not pages:
            n_no_fp.append(case)
            C.warn(f"{case}: 首页 {fp} 在 02_text 里没有 ok 的页产物")
            continue
        harvest_case(case, pages, pat, out)
        n_cases += 1
        C.log(f"  {case} 首页页 {[p['page'] for p in pages]}", quiet=a.quiet)

    if n_cases == 0:
        C.err("没有任何例子的首页可用，映射表不产出")
        return 1

    proposals = {}
    if not a.no_llm:
        unmapped = sorted(out["unknown"], key=lambda k: -out["unknown"][k]["hits"])[:120]
        if unmapped:
            proposals = propose_unmapped(cfg["llm"], unmapped, cols_cfg, audit)

    # ---- 组装映射（按模板列） ----
    cfg_pii = cfg["pii"]
    col_by_id = {c["id"]: c for c in cols_cfg["columns"]}
    per_col: dict[str, list] = defaultdict(list)
    for lab, e in out["known"].items():
        for cid in sorted(label2cols.get(lab, [])):
            # ★ PII 列的示例值在**装配时**就打码：field_mapping.yaml 是给人看/给人改的配置，
            #   不能带身份证号、电话、地址原文（文档同样吃这份已打码的值）。
            col = col_by_id[cid]
            per_col[cid].append({"label": lab, "hits": e["hits"], "n_cases": len(e["cases"]),
                                 "empty_value_hits": e["empty"],
                                 "examples": [mask_example(col, x, cfg_pii) for x in e["examples"]],
                                 "by_variant": dict(e["by_variant"])})
    for cid in per_col:
        per_col[cid].sort(key=lambda x: (-x["n_cases"], -x["hits"]))

    mapping = {"version": cols_cfg["version"], "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "batch": a.batch, "n_cases_scanned": n_cases,
               "n_cases_no_frontpage": len(n_no_fp),
               "status": "draft",           # ★ 人工确认前不得被阶段④当权威映射用
               "columns": [], "unmapped_labels": [], "label_collisions": collisions,
               "pii_examples_masked": True}
    for col in cols_cfg["columns"]:
        if col["source"] != "frontpage":
            continue
        labs = per_col.get(col["id"], [])
        mapping["columns"].append({
            "id": col["id"], "header": col["header"],
            "seed_labels": col.get("candidate_labels") or [],
            "labels_found": labs,
            "status": "draft" if labs else "no_label_found",
        })
    for lab, e in sorted(out["unknown"].items(), key=lambda kv: -kv[1]["hits"]):
        mapping["unmapped_labels"].append({
            "label": lab, "hits": e["hits"], "n_cases": len(e["cases"]),
            "llm_proposed_column": proposals.get(lab),
        })
    C.write_yaml(Path(a.out_config), mapping)

    # ---- 文档 ----
    pos_by_id = {c["id"]: i for i, c in enumerate(cols_cfg["columns"], 1)}
    L = [f"# 模板列名 ↔ 病历首页实际标签（第一遍产出）", "",
         f"- 生成时间: {mapping['generated']}　列定义版本: `{cols_cfg['version']}`　"
         f"词表版本: `{vocab['version']}`",
         f"- 批次: `{a.batch}`　扫描例数: **{n_cases}**　首页不可用例数: {len(n_no_fp)}",
         f"- 状态: **draft（待人工确认）**。确认前阶段④不把它当权威映射。",
         f"- 命中样本数 = 该写法在多少例的首页上出现过；示例值一律打码（PII 列不进文档）。",
         f"- **示例值只是「标签后面紧邻的原文片段」**，用来看写法、不是最终取值；"
         f"跨栏粘连（如 `年龄` 后面跟着 `国籍`）在这里是正常的，"
         f"最终取值由阶段③的 cell/block/coord/llm 策略决定。",
         f"- 采集自**两种 OCR 变体**（版面检测 / 纯文本，见 config/pipeline.yaml:ocr.variants）；"
         f"命中次数按「版面/纯文本」拆开写，避免同一次出现被算两遍时看不出来。",
         ""]
    L += ["## 一、逐列映射", "",
          "| 列序 | 模板列名 | 首页实际标签写法 | 命中样本数 | 命中次数(版面/纯文本) | 示例值 |",
          "|---|---|---|---|---|---|"]
    for i, col in enumerate(cols_cfg["columns"], 1):
        if col.get("mirror_of"):
            base = pos_by_id.get(col["mirror_of"], "?")
            L.append(f"| {i} | {col['header']} | "
                     f"_（镜像列：取值同第 {base} 列，模板原样保留、不合并）_ | - | - | - |")
            continue
        if col["source"] != "frontpage":
            L.append(f"| {i} | {col['header']} | _（非首页来源：{col['source']}）_ | - | - | - |")
            continue
        labs = per_col.get(col["id"], [])
        if not labs:
            L.append(f"| {i} | {col['header']} | **未找到任何写法** | 0 | 0 | - |")
            continue
        for j, lab in enumerate(labs):
            ex = list(lab["examples"][:2])   # 已在装配时打码，这里不再打第二遍
            bv = lab["by_variant"]
            L.append(f"| {i if j == 0 else ''} | {col['header'] if j == 0 else ''} | "
                     f"`{lab['label']}` | {lab['n_cases']} | "
                     f"{lab['hits']}（版面 {bv.get('layout', 0)}/纯文本 {bv.get('plain', 0)}） | "
                     f"{' / '.join(ex) if ex else '（空栏）'} |")
    L += ["", "## 二、一个写法被多列同时认领（歧义，需人裁决）", ""]
    if collisions:
        L += ["| 写法 | 认领的列 id |", "|---|---|"]
        L += [f"| `{c['label']}` | {', '.join(c['columns'])} |" for c in collisions]
    else:
        L.append("（无）")
    L += ["", "## 三、首页上出现、但模板没有对应列的写法", "",
          "`LLM 提议` 只是建议（temp=0，未落地），空=模型也没找到贴切列。", ""]
    if mapping["unmapped_labels"]:
        L += ["| 写法 | 命中样本数 | 命中次数 | LLM 提议 |", "|---|---|---|---|"]
        L += [f"| `{u['label']}` | {u['n_cases']} | {u['hits']} | "
              f"{u['llm_proposed_column'] or '-'} |" for u in mapping["unmapped_labels"]]
    else:
        L.append("（无）")
    L += ["", "## 四、模板里非首页来源的列（第一遍不抽）", ""]
    L += [f"- 第 {i} 列 `{c['header']}` → source=`{c['source']}`"
          for i, c in enumerate(cols_cfg["columns"], 1) if c["source"] != "frontpage"]
    if n_no_fp:
        L += ["", "## 五、首页不可用的例", ""] + [f"- `{c}`" for c in n_no_fp]

    # 模板自身的疑点（重名 / 无来源 / 同量）+ 本次统计出的歧义 → 集中列给人裁决
    susp = cols_cfg.get("suspicions") or {}
    L += ["", "## 待人工裁决（确认后把 config/field_mapping.yaml 的 status 改成 confirmed）", ""]
    for d in susp.get("duplicate_headers") or []:
        L.append(f"- 重名列 `{'` / `'.join(d['header'] + '（第 ' + str(p) + ' 列）' for p in d['positions'])}`"
                 f"：ids={', '.join(d['ids'])}。现按 `mirror_of` 同源同值输出，**不合并**。要不要合并？")
    for d in susp.get("same_quantity_pairs") or []:
        L.append(f"- 同一个量两列：{', '.join(d['ids'])}（第 {', '.join(str(p) for p in d['positions'])} 列）"
                 f"。现同源同值输出，要不要只留一列？")
    for d in susp.get("no_known_source") or []:
        L.append(f"- 第 {d['position']} 列 `{d['header']}`（id={d['id']}）首页上没有对应字段，"
                 f"现填 null。来源怎么定（来源目录注入 / 配置注入 / 保持 null）？")
    for d in susp.get("computed_from_two_paths") or []:
        L.append(f"- 第 {d['position']} 列 `{d['header']}`：两条路都算，"
                 f"冲突时以哪条为准（A 首页直写 / B 出院−入院）？")
    for c in collisions:
        L.append(f"- 写法 `{c['label']}` 同时认领 {', '.join(c['columns'])} —— 按位置区分还是只给其中一列？")
    L.append("- 勾选框字段（性别 / 入院途径 / 离院方式 / 病案质量）：版面模式渲染成 `☐`（选中项丢失），"
             "纯文本模式保留 `①`/数字。**是否授权按编号回填**？不授权则这些列大面积 null。")
    C.write_text(Path(a.out_doc), "\n".join(L) + "\n")

    C.log(f"扫描 {n_cases} 例；已知写法 {len(out['known'])} 种；"
          f"未映射写法 {len(mapping['unmapped_labels'])} 种；歧义 {len(collisions)} 条")
    C.log(f"写入 {a.out_config}\n写入 {a.out_doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
