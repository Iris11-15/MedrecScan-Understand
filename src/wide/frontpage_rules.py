#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段③ 首页**规则层**（round 18 第3步）—— B 类列不经过 LLM，直接由规则产出。

落地位置：src/wide/frontpage_rules.py（本副本在 workspace/round18/patched/，未落地）。
调用方：s3_extract.process_case() → run_frontpage(fp_pages, alias2canon, pat)，
结果写进 frontpage.json 的 strategies["rule"]，**键 = 列 id**（不是标签），
s4_normalize.pick_from_rule() 读它。

为什么键是列 id 而不是标签：规则命中天然带 source_label（标签词本身），
且「同一标签归哪一列」需要位置消歧（首页上「电话」出现 2 次，前一个是本人电话、
后一个是联系人电话）—— 这件事在规则层做比在 s4 做证据更足（能看到锚点）。

红线①⑥：规则值必须挂 source_label，且必须是**进过 prompt 的正文里的原样子串**，
否则会被 s4 来源守卫（G3）判坏 → 命中数下降。两条都在离线对账里逐格验过：
1793/1793 挂 label、J2 全过、J3 全过（workspace/round18/out/reconcile_many_final.out C 段）。
红线⑦：规则层不许为了少调 LLM 而少给字段 —— 拿不到就不写这一列，
由 s3 的 missing_columns() 决定是否补一次 LLM。
"""
from __future__ import annotations

import re
from collections import Counter

NULL_TOKENS = {"", "-", "—", "一", "－", "/", "无", "未提及", "null", "None", "☐"}
UNDER = re.compile(r"[-_—一－\s]{2,}")


# --------------------------------------------------------------- 校验器
# 每个校验器只做一件事：从「值段」里**取子串** + 记 flags。
# 不重写、不换算、不补全 —— 取到的子串天然满足守卫 J3（原文可原样找到）。
def _blank(seg: str) -> bool:
    s = seg.strip(" :：\t")
    return (not s) or (s in NULL_TOKENS) or bool(UNDER.fullmatch(s))


def v_code(seg: str):
    """病案号/住院号：值段里第一个「字母数字」token（≥4 位）。

    实测必须取 token、不能整段用 —— 桐乡页眉「病案号00286042 住院次数3」整段当值
    就是现网 adm_no 的串污染值（04_norm 里逐字可见，round10 报告）。
    """
    for m in re.finditer(r"[0-9A-Za-z][0-9A-Za-z\-/]{3,19}", seg):
        tok = m.group(0)
        if re.search(r"\d", tok):
            return tok, []
    return None


def v_age(seg: str):
    m = re.search(r"\d{1,3}\s*岁", seg)
    return (m.group(0).strip(), []) if m else None


def v_idcard(seg: str):
    m = re.search(r"\b\d{17}[0-9Xx]\b", seg)
    if not m:
        return None
    s = m.group(0)
    w = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    chk = "10X98765432"
    calc = chk[sum(int(s[i]) * w[i] for i in range(17)) % 11]
    flags = [] if calc == s[17].upper() else ["id_checksum_mismatch"]
    return s, flags


def v_phone(seg: str):
    m = re.search(r"\d[\d\s-]{5,19}\d", seg)
    if not m:
        return None
    s = m.group(0).strip()
    n = len(re.sub(r"\D", "", s))
    if not (7 <= n <= 20):
        return None
    return s, ([] if n in (11, 8) else ["phone_len_unusual"])


# 表格竖线必须容忍：实测 wide10c 长兴(13) 原文是「2024 年 11 月 22 日 | 19 时 5 分」，
# 不容 | 时规则只取到「2024 年 11 月 22 日」，比 LLM 少一段时分。
_DATE = (r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
         r"(?:[\s|]*\d{1,2}\s*时(?:[\s|]*\d{1,2}\s*分)?)?"
         r"|\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"
         r"(?:[\s|]*\d{1,2}[:时]\d{1,2}(?::\d{1,2})?)?")


def v_date(seg: str):
    m = re.search(_DATE, seg)
    if not m:
        return None
    raw = m.group(0).strip()
    flags = []
    # 跨单元格的日期：page_text 用 " | " 连接单元格，值段里的 | 换成空格再存。
    # 合法性：来源守卫 J3 的比较本身就是 re.sub(r"[\s|]+","")（s4_normalize.py:231），
    # 去掉 | 之后仍是 J3 语料的子串；不这么做的话 parse_cn_date(s4:32) 拿不到时分，
    # admit_time_std 会从 cn_datetime 退化成 cn_date（丢时分 = 字段完整性下降，红线⑦）。
    if "|" in raw:
        raw = re.sub(r"\s*\|\s*", " ", raw).strip()
        flags.append("cell_join_normalized")
    return raw, flags


_OPT = re.compile(r"(?:[①②③④⑤⑥⑦⑧⑨]|\d\s*[.、])\s*[\u4e00-\u9fa5]")


def v_person(seg: str):
    s = seg.strip(" :：\t")
    if _blank(s):
        return None
    m = re.match(r"[\u4e00-\u9fa5]{2,4}(?:·[\u4e00-\u9fa5]{1,4})*", s)
    if not m:
        return None
    name = m.group(0)
    rest = s[len(name):].strip(" :：\t")
    flags = []
    if rest and not _blank(rest):
        # 值段后面还粘着别的东西（跨栏/串栏）→ 保留姓名，但记账
        flags.append("trailing_text_in_segment")
    return name, flags


def v_place(seg: str):
    s = seg.strip(" :：\t")
    if _blank(s):
        return None
    m = re.match(r"[\u4e00-\u9fa5][\u4e00-\u9fa5\s]{1,40}", s)
    if not m:
        return None
    out = re.sub(r"\s+", " ", m.group(0)).strip()
    if _OPT.search(out):
        return None
    return out, []


def v_address(seg: str):
    s = seg.strip(" :：\t")
    if _blank(s):
        return None
    m = re.match(r"[\u4e00-\u9fa5][\u4e00-\u9fa50-9\s\-]{3,80}", s)
    if not m:
        return None
    out = re.sub(r"\s+", " ", m.group(0)).strip()
    if _OPT.search(out):
        return None
    return out, []


VALIDATORS = {"code": v_code, "age": v_age, "idcard": v_idcard, "phone": v_phone,
              "date": v_date, "person": v_person, "place": v_place,
              "address": v_address}

# --------------------------------------------------------------- 列规则表（B 类 12 列）
# labels = 优先顺序（前面的标签组赢）；fallback_labels = 前面的组全没命中才用
# anchor_after / anchor_before = 位置消歧（同一标签在首页上出现多次时）
RULES: dict[str, dict] = {
    "adm_no": {
        "labels": ["住院号", "住院病历号", "住院流水号"],
        "fallback_labels": ["病案号", "病历号", "病案编号"],
        "fallback_flag": "alias_claim(病案号→adm_no，columns.yaml:35 candidate_labels 无此写法，"
                         "沿用现网 s4 labels_by_col 的既定行为)",
        "validator": "code", "vote": True},
    "case_no": {"labels": ["病案号", "病历号", "病案编号", "病人ID"], "validator": "code",
                "vote": True},
    "patient_name": {"labels": ["姓名", "患者姓名", "病人姓名"], "validator": "person"},
    "age": {"labels": ["年龄", "年龄(岁)"], "validator": "age"},
    "native_place": {"labels": ["籍贯", "出生地"], "validator": "place"},
    "id_card": {"labels": ["身份证号", "身份证号码", "国籍/身份证号"], "validator": "idcard"},
    "patient_phone": {"labels": ["电话", "联系电话", "本人电话"],
                      "anchor_before": ["联系人姓名", "联系人"], "validator": "phone"},
    "contact_phone": {"labels": ["联系人电话", "电话"],
                      "anchor_after": ["联系人姓名", "联系人"], "validator": "phone"},
    "address": {"labels": ["现住址", "现地址", "住址", "户口地址"], "validator": "address"},
    "contact_name": {"labels": ["联系人姓名", "联系人", "紧急联系人"], "validator": "person"},
    "admit_time_raw": {"labels": ["入院时间", "入院日期", "住院时间", "入院日期及时间"],
                       "validator": "date"},
    "discharge_date": {"labels": ["出院时间", "出院日期及时间", "出院日期"], "validator": "date"},
}
RULE_COLUMNS = list(RULES)


# --------------------------------------------------------------- 扫描
def scan_page(text: str, pat: re.Pattern) -> list[dict]:
    """标签出现序列（文档顺序 + 绝对偏移）。值段 = 本标签末尾 → 下一标签开头。

    与 s3_extract.extract_pairs 同一切法（:53），差别是这里保留偏移与页号，
    供锚点消歧与多数表决用。
    """
    ms = list(pat.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append({"label": m.group("label"), "start": m.start(), "end": m.end(),
                    "seg": text[m.end():end]})
    return out


def apply_rule(spec: dict, occurrences: list[dict]) -> dict:
    """一个块（= 一页的 page_text）内的规则判定。

    返回 {"hit": 文档顺序第一个过校验的候选 or None,
          "cands": 该标签组内全部过校验候选, "group": 标签组序号, "diag": …}
    跨页的多数表决在 run_frontpage 里做，本函数只收集**一页内**的候选。
    """
    diag = {"n_label_occ": 0, "n_validator_reject": 0, "labels_seen": []}
    groups = [(spec.get("labels") or [],
               []),
              (spec.get("fallback_labels") or [],
               [spec["fallback_flag"]] if spec.get("fallback_flag") else [])]
    for gi, (labels, gflags) in enumerate(groups):
        cands: list[dict] = []
        for lab in labels:
            hits = [o for o in occurrences if o["label"] == lab]
            if not hits:
                continue
            diag["n_label_occ"] += len(hits)
            diag["labels_seen"].append(lab)
            anchors = [o for a in (spec.get("anchor_after") or [])
                       for o in occurrences if o["label"] == a]
            before = [o for a in (spec.get("anchor_before") or [])
                      for o in occurrences if o["label"] == a]
            if anchors:
                lim = min(o["start"] for o in anchors)
                hits = [o for o in hits if o["start"] > lim]
            if before:
                lim = min(o["start"] for o in before)
                hits = [o for o in hits if o["start"] < lim]
            for k, o in enumerate(hits):
                got = VALIDATORS[spec["validator"]](o["seg"])
                if got:
                    val, vflags = got
                    cands.append({"value": val, "source_label": lab,
                                  "validator": spec["validator"],
                                  "flags": list(gflags) + list(vflags), "occurrence": k,
                                  "n_candidates": len(hits), "page": o["page"],
                                  "block": o["block"]})
                else:
                    diag["n_validator_reject"] += 1
        if cands:
            return {"hit": cands[0], "cands": cands, "group": gi, "diag": diag}
    return {"hit": None, "cands": [], "group": len(groups), "diag": diag}


def vote_candidates(cands: list[dict]) -> dict:
    """同一标签多次出现且取到**不同值**（实测 wide10c 安吉：页眉块把 02489012 读成
    02405012，正文块读对）→ 多数表决，平票取最后一次出现。

    理由：住院病案首页的权威字段在正文行「健康卡号… 第N次住院 病案号：X」，
    页眉那条在前且实测会被串污染（「病案号00286042 住院次数3」）。
    cands 必须按文档顺序（页序 → 块序 → 块内偏移）传入。
    """
    cnt = Counter(h["value"] for h in cands)
    if len(cnt) == 1:
        return dict(cands[0])
    last = {h["value"]: i for i, h in enumerate(cands)}
    win = max(cnt.items(), key=lambda kv: (kv[1], last[kv[0]]))[0]
    out = dict(next(h for h in reversed(cands) if h["value"] == win))
    out["flags"] = out["flags"] + ["vote:" + ",".join(f"{v}x{n}" for v, n in cnt.items())]
    return out


def run_frontpage(pages: list[dict], alias2canon: dict, pat: re.Pattern) -> dict:
    """pages = s3 选定的首页页记录（文档顺序）。返回 {列 id: [命中]}。

    扫的是**进 prompt 的那份页文本**（s3_extract.page_text，含 text_* 副变体），
    不是 block.text。理由（实测）：s2 把 block 里的 HTML 表格标签换成 " | "，两者字符不同 ——
      ① 来源守卫 J3 的核对文本就是 page_text（s4_normalize.py:280-291），扫 block
         拿到的值段可能不在 J3 语料里；
      ② 跨单元格的真值只在 page_text 里连续（wide10c 长兴(13)：block 是
         「…22 日</td><td>19 时 5 分」，page_text 是「…22 日 | 19 时 5 分」）。
    """
    per_page: list[list[dict]] = []
    for r in pages:
        t = _page_text(r)
        if not t.strip():
            continue
        occ = scan_page(t, pat)
        for o in occ:
            o["page"] = r["page"]
            o["block"] = None
        per_page.append(occ)

    out: dict[str, list] = {}
    for cid, spec in RULES.items():
        first, pool, best_group = None, [], None
        for occ in per_page:                    # 页序 = frontpage_pages 升序
            r = apply_rule(spec, occ)
            if not r["cands"]:
                continue
            if first is None:
                first = r["cands"][0]
            if spec.get("vote"):
                if best_group is None or r["group"] < best_group:
                    pool, best_group = list(r["cands"]), r["group"]
                elif r["group"] == best_group:
                    pool += r["cands"]
            else:
                break                           # 非表决列：文档顺序第一个过校验的赢
        hit = vote_candidates(pool) if (spec.get("vote") and pool) else first
        if hit is not None:
            hit["where"] = "rule"
            out.setdefault(cid, []).append(hit)
    return out


def _page_text(p: dict) -> str:
    """延迟 import 避免与 s3 的循环依赖（s3 顶部 import 本模块）。"""
    from wide import s3_extract as S3
    return S3.page_text(p)


def missing_columns(rule_hits: dict, cols_cfg: dict) -> list[str]:
    """B 类里规则没拿到的列（且不是镜像列）→ 需要补一次 LLM 兜底。

    红线⑦：规则拿不到时**不许静默放弃**，必须回落到 LLM；镜像列（mirror_of）除外
    —— 它的值由 s4_normalize.py:380-388 从基准列复制，问 LLM 纯属浪费。
    """
    want = []
    for c in cols_cfg["columns"]:
        if c["id"] not in RULES:
            continue
        if c.get("mirror_of"):
            continue
        if not (rule_hits.get(c["id"]) or []):
            want.append(c["id"])
    return want
