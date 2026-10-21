#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段④ s4_normalize.py —— 字段名归一化 + 派生/标准化列。

输入   run/wide/<batch>/03_extract/<case_id>/{frontpage,first_progress}.json
       config/field_mapping.yaml      模板列 ↔ 首页实际标签（第一遍产出，人工确认后生效）
输出   run/wide/<batch>/04_norm/<case_id>/fields.json   全部模板列的取值 + provenance
                                        （列数 = config/columns.yaml，2026-10-09.4 起 24 列）
       run/wide/<batch>/04_norm/<case_id>/_manifest.json + .done / .incomplete

红线落点：
  * 缺失 = null。空串、纯符号、config 里的 null_tokens（"无"/"未提及"…）一律归 null。
  * 归一化必须留来源与版本：每个字段记 {source_label, strategy, mapping_version}。  * round 10 第1件：frontpage 列**来源说不出来就不给值**（guard_provenance，G3）。
    判据 J1 有 source_label / J2 该标签属于本列 / J3 值能在进过 prompt 的首页正文里原样找到。
    命中数会下降（adm_no 10/10 → 7/10），这是如实反映，不是回归。
    开关 config/pipeline.yaml: normalize.provenance_guard（缺键 = 开；见报告「需要裁决」）。
  * round42：标准化就诊医院 —— config 的 hospitals 有映射走映射（奉化 → 宁波市奉化区人民医院
    医共体总院 这类短键标准化名不变）；**没有映射就用源目录名原样**（raid5 语料的顶层目录名
    本身就是中文院名，如 安吉县人民医院 / 恩泽医院）。兜底值是照抄目录名，不是从拼音缩写猜中文院名（红线 E）。
    开关 config/pipeline.yaml: normalize.hospital_std_from_dir_name（缺键 = 开；
    写 false 回到旧行为「不在表里就 null」）。
  * 重名列（第 2/8 列「姓名」、第 19/20 列「标准化入院时间」）**不合并**，各写各的，
    但值来自同一次标准化 —— 报告里顶格说明。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wide import common as C  # noqa: E402

STAGE = "04_norm"
def parse_cn_date(text: str) -> tuple[str | None, str]:
    """中文日期 → ISO。解析不了返回 (None, 原因) —— 不猜。"""
    if not text:
        return None, "empty"
    s = str(text).strip()
    m = re.search(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月"
                  r"(?:\s*(?P<d>\d{1,2})\s*日)?"
                  r"(?:\s*(?P<h>\d{1,2})\s*时(?:\s*(?P<mi>\d{1,2})\s*分)?)?", s)
    if m:
        g = m.groupdict()
        y, mo = int(g["y"]), int(g["m"])
        d = int(g["d"]) if g["d"] else 1
        h = int(g["h"]) if g["h"] else None
        mi = int(g["mi"]) if g["mi"] else 0
        try:
            if h is None:
                return date(y, mo, d).isoformat(), "cn_date"
            return datetime(y, mo, d, h, mi).strftime("%Y-%m-%dT%H:%M"), "cn_datetime"
        except ValueError as e:
            return None, f"cn_date 越界 {e}"
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            if m.group(4):
                return datetime(y, mo, d, int(m.group(4)), int(m.group(5))).strftime(
                    "%Y-%m-%dT%H:%M"), "iso"
            return date(y, mo, d).isoformat(), "iso"
        except ValueError as e:
            return None, f"iso 越界 {e}"
    return None, "无法解析"


def to_iso_date(text: str) -> date | None:
    iso, _ = parse_cn_date(text)
    if not iso:
        return None
    return datetime.strptime(iso[:10], "%Y-%m-%d").date()


# ────────────────── round16 第7步：格式归一化（日期统一 + ID 尾随串） ──────────────────
# 用户指定：所有日期类列统一为「YYYY年MM月DD日」，月/日补零、丢弃时刻部分。
# 匹配不上的一律**保留原值**并打 QC flag（红线⑥：不许丢值、不许填占位词）。
DATE_DISPLAY_COLS = ("admit_time_raw", "admit_time_std", "admit_time_std_dup",
                     "discharge_date")
DATE_HEAD_RE = re.compile(r"^\s*(\d{4})\s?[-/年.]?\s?(\d{1,2})\s?[-/月.]?\s?(\d{1,2})\s*日?")
# round43 R3：raid5 语料里同一院区往往有两个扫描批次目录，第二个带 _YYYYMMDD 后缀
#   （实测 /raid5/history_13_15/history_15/{乐清市人民医院, 乐清市人民医院_20250520}）。
#   目录名兜底时照抄后缀 ⇒ 同一家医院算出两个「标准化就诊医院」⇒ 四元组判重键永不匹配
#   ⇒ 宽表里同一份病历留两行，且 s6 报「跨院区撞车」（假警报）并以 rc=1 退出。
#   只剥「_ + 8 位合法日期」这一种后缀；剥完再回 config:hospitals 查一次映射。
DIR_SCAN_DATE_RE = re.compile(r"^(?P<base>.+)_(?P<d>(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01]))$")
# ────────── 裁决 2026-10-18：标准化就诊医院 取「首页 OCR 读到的实际院名」 ──────────
# 实测（workspace/round45/probes/hosp_fp4.py，批 web-20261018-102926 抽 582 例）：
#   首页优先命中 485/582，再退到本例其它已 OCR 页补 30 ⇒ 515/582 = 88.5%；
#   15 个源目录 13 个选名唯一且正确 —— jiaxing_rmyy 读到的是「嘉兴市第二医院」，
#   按拼音猜会得「嘉兴人民医院」⇒ 正是红线 E 禁止的猜法。
#   pinghu_rmyy 12/40、yiwu_zxyy 4/40 命中（首页常只有转诊套话）⇒ 用同目录多数票补。
HOSP_NAME_RE = re.compile(r"[\u4e00-\u9fa5]{2,20}(?:医院|卫生院|保健院|医学中心)")
HOSP_NOISE_RE = re.compile(r"乡镇|社区|村级|转诊|可由|可到|上级|下级|本院|外院|该|各级"
                           r"|医疗机构|综合医院|附属医院|自行前往")
HOSP_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")


def hospital_ocr_name(batch_dir: Path, case: str):
    """本例【已 OCR 的页】里的院名（不额外 OCR）：首页命中优先，没有再退到其它已 OCR 页。"""
    from collections import Counter
    from wide import s3_extract as S3
    fpj = batch_dir / "03_extract" / case / "frontpage.json"
    fp_pages = set(C.read_json(fpj).get("frontpage_pages") or []) if fpj.exists() else set()
    texts = {}
    for pj in sorted((batch_dir / "02_text" / case).glob("page_*.json")):
        rec = C.read_json(pj)
        texts[rec.get("page")] = S3.page_text(rec)
    for scope in (sorted(fp_pages), list(texts)):
        if not scope:
            continue
        cands = [c for c in HOSP_NAME_RE.findall("\n".join(texts.get(p, "") for p in scope))
                 if not HOSP_NOISE_RE.search(c)]
        cnt = Counter(cands)
        for a in list(cnt):            # A 是 B 的真后缀 ⇒ A 是被截断的尾巴，丢 A
            for b in list(cnt):
                if a != b and b.endswith(a):
                    cnt.pop(a, None)
        if cnt:
            return cnt.most_common(1)[0][0]
    return None


def hospital_dir_vote(batch_dir: Path, dir_key: str, own):
    """把本例候选记进 00_index/hospital_names.json，返回该目录的多数票院名（没有则 None）。
    24 并发 ⇒ 读-改-写整段在 flock 里；文件只有「源目录数」条，成本可忽略。"""
    import fcntl
    out_dir = batch_dir / "00_index"
    out_dir.mkdir(parents=True, exist_ok=True)
    idx, lock = out_dir / "hospital_names.json", out_dir / "hospital_names.json.lock"
    # 锁文件也必须 open 时就 0600（R9/R10 同一根因：先 open 再 chmod 有 664 窗口）
    with C._open_out(lock, "600") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        data = C.read_json(idx) if idx.exists() else {}
        ent = data.setdefault(dir_key, {"votes": {}, "n_cases": 0, "n_ocr_hit": 0})
        ent["n_cases"] += 1
        if own:
            ent["n_ocr_hit"] += 1
            ent["votes"][own] = ent["votes"].get(own, 0) + 1
        top = sorted(ent["votes"].items(), key=lambda kv: -kv[1])
        C.write_json(idx, data)
        return top[0][0] if top else None
# ────────────────── round33 第1.2/1.3件：入院时间的时刻不再丢弃 ──────────────────
# 现状（round16 第7步）：四列日期一律归一化成「YYYY年MM月DD日」，时刻只留在
#   fields.json 的 pre_norm_value（config/columns.yaml:205,217 明写「丢弃时刻」）。
# round33 改：
#   admit_time_raw       → 首页原文照抄（只丢秒），不再统一成「YYYY年MM月DD日」
#   admit_time_std/_dup  → ISO「YYYY-MM-DD[ HH:MM]」，不再转中文显示格式
#   discharge_date       → 维持「YYYY年MM月DD日」不动（任务书 1.2：出院日期保持原文照抄）
# 对照/回滚开关：config normalize.date_time.keep（**缺键=开**）或 s4 --no-keep-admit-time。
DATE_KEEP_TIME_COLS = ("admit_time_raw", "admit_time_std", "admit_time_std_dup")
TIME_MINUTE_RE = re.compile(r"\d{1,2}\s*时\s*\d{1,2}\s*分|\d{1,2}:\d{2}")
TIME_HOUR_RE = re.compile(r"\d{1,2}\s*时")
SECONDS_TAIL_RE = re.compile(r"(\d{1,2}[:：]\d{2})[:：]\d{2}")
SECONDS_CN_RE = re.compile(r"(\d{1,2}\s*时\s*\d{1,2}\s*分)\s*\d{1,2}\s*秒")


def trim_seconds(text):
    """印了秒的抄到分钟（秒丢弃）。没印秒的原样返回。"""
    s = str(text)
    s2 = SECONDS_TAIL_RE.sub(r"\1", s)
    s2 = SECONDS_CN_RE.sub(r"\1", s2)
    return s2.strip()


def time_precision(text) -> str:
    """页面上印到的时间精度：minute / hour / date_only / none。不猜。"""
    if text is None:
        return "none"
    s = str(text)
    if TIME_MINUTE_RE.search(s):
        return "minute"
    if TIME_HOUR_RE.search(s):
        return "hour"
    if re.search(r"\d{4}\s*年|\d{4}[-/]\d{1,2}", s):
        return "date_only"
    return "none"


def iso_display(iso: str, precision: str) -> str:
    """parse_cn_date 的 ISO（YYYY-MM-DDTHH:MM）→ 交付形态「YYYY-MM-DD[ HH:MM]」。
    页面只印到「时」→ 标准化列只到日期（红线⑥：不许把分钟补成 00）；
    小时原样留在 admit_time_raw 与 fields.json，并由 s5 记一条 QC。"""
    if not iso:
        return iso
    s = str(iso).replace("T", " ").strip()
    if precision != "minute":
        s = s[:10]
    return s


# ────────────────── round33 第1.1件：年龄去单位（只剥后缀，不做语义解析） ──────────────────
AGE_BARE_RE = re.compile(r"^\d+$")
AGE_SUFFIX_RE = re.compile(r"^(\d+)(\s*)岁$")
AGE_Y_RE = re.compile(r"^(\d{1,3})\s*[Yy](?:\s*(\d{1,2})\s*[Mm月])?$")
AGE_YM_RE = re.compile(r"^(\d{1,3})\s*(?:岁|周岁)\s*(\d{1,2})\s*[个]?[月Mm]$")
AGE_DEC_RE = re.compile(r"^(\d{1,3})(?:\.\d*)?$")


def norm_age(value):
    """年龄统一成纯数字（round42o）。返回 (值, 处理方式)：
      already_numeric      「59」          → 原样
      stripped             「59岁」        → 「59」
      stripped_whitespace  「85 岁」       → 「85」（数字与「岁」之间有空格，s5 记一条）
      stripped_y           「44Y」         → 「44」（护理单常见写法 N+Y）
      stripped_y_months    「44Y6M」/「68岁3个月」 → 「44」/「68」（月数丢弃，s5 记一条）
      decimal_truncated    「60.2」/「30.」 → 「60」/「30」（小数部分丢弃，s5 记一条）
      unparseable          「（」/「__岁」/「不足1周岁的）」 → (None, "unparseable")
                           按红线⑥判缺失，**不再把模板残片原样写进年龄列**
    """
    if value is None:
        return None, "no_value"
    s = str(value).strip()
    if not s:
        return None, "no_value"
    if AGE_BARE_RE.match(s):
        return s, "already_numeric"
    m = AGE_SUFFIX_RE.match(s)
    if m:
        return m.group(1), ("stripped" if not m.group(2) else "stripped_whitespace")
    m = AGE_Y_RE.match(s)
    if m:
        return m.group(1), ("stripped_y_months" if m.group(2) else "stripped_y")
    m = AGE_YM_RE.match(s)
    if m:
        return m.group(1), "stripped_y_months"
    m = AGE_DEC_RE.match(s)
    if m:
        return m.group(1), "decimal_truncated"
    return None, "unparseable"


# ID 列（住院号/病案号/标准化病案号）：值本体必须是单个 ID token，
# 实测桐乡二院两例走 block 路径取到「00286042 住院次数3」这类跨栏尾巴。
ID_TOKEN_COLS = ("adm_no", "case_no", "case_no_std")
ID_TOKEN_RE = re.compile(r"^([0-9A-Za-z][0-9A-Za-z/_\-]*)(\s+)(\S.*)$")


def norm_date_display(text) -> tuple[str | None, bool, bool]:
    """日期 → 「YYYY年MM月DD日」。返回 (显示值, 匹配与否, 是否丢了时刻部分)。
    匹配不上 → (None, False, False)，调用方必须保留原值。"""
    if text is None:
        return None, False, False
    s = str(text).strip()
    if not s:
        return None, False, False
    m = DATE_HEAD_RE.match(s)
    if not m:
        return None, False, False
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        date(y, mo, d)                       # 越界（2025-13-40）不算匹配，保留原值
    except ValueError:
        return None, False, False
    return f"{y:04d}年{mo:02d}月{d:02d}日", True, bool(s[m.end():].strip())


def trim_id_tail(value):
    """ID 列尾随串污染 → 只留首个 ID token。砍不动就原样返回（不猜）。"""
    m = ID_TOKEN_RE.match(str(value))
    if not m:
        return None, None
    head, tail = m.group(1), m.group(3).strip()
    return (head, tail) if head else (None, None)


def norm_case_no(raw: str, hosp: str, cfg_norm: dict) -> str | None:
    if not raw:
        return None
    rules = cfg_norm["case_no_std"]
    s = str(raw)
    if rules.get("strip_nonalnum"):
        s = re.sub(r"[^0-9A-Za-z]", "", s)
    if rules.get("upper"):
        s = s.upper()
    w = (rules.get("width_by_hospital") or {}).get(hosp)
    if w and s.isdigit():
        s = s.zfill(int(w))
    return s or None


def clean_value(v, null_tokens: list[str]) -> str | None:
    """值归一：空/占位词 → None（红线①）。其它只去首尾空白。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in null_tokens:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def pick_from_strategies(strategies: dict, llm: dict, labels: list[str],
                         priority: list[str], null_tokens: list[str]) -> dict | None:
    """按优先级在多种策略里找第一个有值的命中。"""
    for strat in priority:
        src = llm.get("values", {}) if strat == "llm" else strategies.get(strat, {})
        for lab in labels:
            if strat == "llm":
                # llm 的键是列 id，不是标签 —— 由调用方单独处理
                continue
            hits = src.get(lab) or []
            for h in hits:
                val = clean_value(h.get("value"), null_tokens)
                if val is not None:
                    return {"value": val, "source_label": lab, "strategy": strat,
                            "page": h.get("page")}
    return None


def guard_checkbox(hit: dict | None, cb_cfg: dict) -> dict | None:
    """勾选框列：读到的是**整排选项**（「☐ 1. 男 2. 女」）而不是选中项 → 判缺失。

    红线①：缺失就是缺失，不许拿选项串冒充有值。原文留 raw_option_text 供人工裁决。
    按编号回填（①→第 1 项）是**推断**，config 里 fill: number 才允许，且当前未实现
    —— 未授权前只会显式报错，不会静默回填。
    """
    if not hit or hit.get("value") is None:
        return hit
    v = str(hit["value"])
    n_opt = len(re.findall(cb_cfg["option_pattern"], v))
    has_box = any(ch in v for ch in str(cb_cfg.get("box_chars") or ""))
    if n_opt < int(cb_cfg["min_options"]) and not has_box:
        return hit
    if str(cb_cfg.get("fill")) == "number":
        raise SystemExit("checkbox fill=number 未实现：按编号回填属推断，"
                         "需人工授权后再实现，不得静默回填")
    hit = dict(hit)
    hit["raw_option_text"] = v
    hit["value"] = None
    hit["null_reason"] = "勾选框只读到整排选项，选中项无法确定（待人工裁决，见 docs/column_mapping.md）"
    return hit


def pick_from_llm(llm: dict, col_id: str, null_tokens: list[str]) -> dict | None:
    if llm.get("status") != "ok":
        return None
    v = (llm.get("values") or {}).get(col_id)
    if v is None:
        return None
    if isinstance(v, dict):
        val = clean_value(v.get("value"), null_tokens)
        lab = v.get("label")
    else:
        val, lab = clean_value(v, null_tokens), None
    if val is None:
        return None
    return {"value": val, "source_label": lab, "strategy": "llm", "page": None}


def pick_from_rule(strategies: dict, col_id: str, null_tokens: list[str]) -> dict | None:
    """round18 第3步：规则层命中（frontpage.json 的 strategies["rule"]，**键 = 列 id**）。

    只在 strategies 里真的有 "rule" 时生效 —— 产线 s3 的产物没有这个键，
    所以本函数对旧批次是纯 no-op（不需要改 config 就能安全对照）。
    fields.json 的字段结构不变：只有 value / source_label / strategy / page
    （外加 rule_flags / rule_validator 两个审计用的附加键，与既有
    raw_option_text / raw_rejected 同类，下游按 .get() 读）。
    """
    rule = strategies.get("rule")
    if not rule:
        return None
    for h in rule.get(col_id) or []:
        val = clean_value(h.get("value"), null_tokens)
        if val is None:
            continue
        out = {"value": val, "source_label": h.get("source_label"),
               "strategy": "rule", "page": h.get("page")}
        if h.get("flags"):
            out["rule_flags"] = list(h["flags"])
        if h.get("validator"):
            out["rule_validator"] = h["validator"]
        return out
    return None


# --------------------------------------------------------------- 来源守卫（round 10 第1件 · G3）
def norm_label(s) -> str | None:
    """标签归一：去所有空白 + 去首尾标点。

    实测必须归一，否则 OCR 的空格/冒号写法会误杀真值：
      「年 龄」→年龄、「民 族」→民族、「病案号：」→病案号。
    「住院病案首页」归一后仍是它自己 —— 它是**文档标题**不是字段标签，必须拦住。
    """
    if s is None:
        return None
    s = re.sub(r"\s+", "", str(s))
    s = s.strip("：:、,，.。;；-—_ ")
    return s or None


def build_label_sets(cols_cfg: dict, mapping: dict) -> dict:
    """每列可接受的**字段标签**集合（归一化后）。三处来源，缺一不可：
      1) config/columns.yaml: candidate_labels + header（列定义唯一来源）
      2) config/field_mapping.yaml: 该列 labels_found（第一遍映射 + 人工确认的实际写法）
      3) config/label_vocab.yaml: progress_sections（first_progress 列的小标题词表）

    实测：contact_phone 的 candidate_labels 只有「联系人电话」，但首页上实际写「电话」
    （393/398 格）—— 只靠 (1) 会把这一列几乎全灭，所以 (2) 必须在。
    """
    from wide import s3_extract as S3      # s3 不 import s4 → 无循环
    vocab = S3.load_vocab()[1]
    sections = vocab.get("progress_sections") or {}
    found = {}
    for m in mapping.get("columns", []):
        found[m["id"]] = [x.get("label") for x in (m.get("labels_found") or [])]
    out = {}
    for col in cols_cfg["columns"]:
        cid = col["id"]
        raw = list(col.get("candidate_labels") or []) + [col["header"]]
        raw += found.get(cid) or []
        if col["source"] == "first_progress":
            raw += sections.get(col["header"]) or []
        out[cid] = {x for x in (norm_label(v) for v in raw) if x}
    return out


def guard_provenance(hit, cid, label_sets, cfg_norm, pii, src_norm_text):
    """来源守卫 G3：frontpage 列的值必须**说得出从哪来**，否则判缺失（红线①⑥）。

    三条判据（任一不过 → value=None + null_reason + raw_rejected 元数据）：
      J1 source_label 必须存在；
      J2 source_label 归一后必须属于该列的字段标签集（build_label_sets）；
      J3 值必须能在真正进过 prompt 的首页正文里原样找到（空白与表格竖线归一化后取子串）。

    实测拦住的两种真实缺陷（历史上都只发生在 adm_no 上，见 reports/round10-*）：
      · strategy=llm、source_label=null —— 模型从页眉一个**无标签裸数字**取值
        （旧 OCR 里那个数字是独立块 block_label=vision_footnote，bbox 在页眉右上角）；
      · strategy=llm、source_label=「住院病案首页」—— 模型把**文档标题**当字段标签，
        取了标题后面那个流水号。
    s5 打 flag 的旧做法对这两条无效：s5 全文不读 source_label → 对 QC 完全静默。

    红线⑨：raw_rejected 里不放原值；PII 列连哈希都不放（哈希可反查），只放长度。
    """
    if not hit or hit.get("value") is None:
        return hit
    g = cfg_norm.get("provenance_guard") or {}
    if not bool(g.get("enabled", True)):
        return hit
    lab = norm_label(hit.get("source_label"))
    val = str(hit["value"])
    reason = None
    if lab is None:
        if bool(g.get("require_label", True)):
            reason = "no_source_label"
    elif lab not in label_sets.get(cid, set()):
        if bool(g.get("require_label", True)):
            reason = "source_label_not_a_field_label_of_this_column"
    if reason is None and bool(g.get("require_verbatim", True)):
        # 表格行里单元格用 " | " 分隔，值可能跨格（实测「2024 年 11 月 22 日 | 19 时 5 分」）
        # → 归一化必须连 | 一起去掉，否则误杀真值。
        if src_norm_text and re.sub(r"[\s|]+", "", val) not in src_norm_text:
            reason = "value_not_verbatim_in_source_page"
    if reason is None:
        return hit
    hit = dict(hit)
    rej = {"reason": reason, "strategy": hit.get("strategy"),
           "source_label": hit.get("source_label"), "value_len": len(val)}
    if cid not in pii:
        rej["value_sha256_8"] = C.sha256_text(val)[:8]
    hit["raw_rejected"] = rej
    hit["value"] = None
    hit["source_label"] = None
    hit["null_reason"] = ("来源守卫判坏：" + reason
                          + "（来路不明的值不给值；原值长度/哈希见 raw_rejected）")
    return hit


def process_case(row: dict, cfg: dict, cols_cfg: dict, mapping: dict, batch_dir: Path,
                 audit: C.Audit, seq_no: int, force: bool, quiet: bool) -> dict:
    case = row["case_id"]
    src = batch_dir / "03_extract" / case
    dst = batch_dir / "04_norm" / case
    dst.mkdir(parents=True, exist_ok=True)
    (dst / ".done").unlink(missing_ok=True)
    (dst / ".incomplete").unlink(missing_ok=True)
    if not (src / "frontpage.json").exists():
        C.write_marker(dst / ".incomplete", "stage-3 产物缺失")
        return {"case_id": case, "errors": ["missing stage-3 output"]}

    fp = C.read_json(src / "frontpage.json")
    pn = C.read_json(src / "first_progress.json")
    strategies, llm = fp.get("strategies", {}), fp.get("llm", {})
    null_tokens = list(cfg["normalize"]["null_tokens"])
    priority = list(cfg["normalize"]["merge_priority"])
    map_version = mapping.get("version", "unmapped")
    # 映射表 schema = tools/pass1_mapping.py 的产出（columns[*].id / labels_found[*].label）
    labels_by_col = {m["id"]: [str(x.get("label")) for x in (m.get("labels_found") or [])
                               if x.get("label")]
                     for m in mapping.get("columns", [])}
    label_sets = build_label_sets(cols_cfg, mapping)
    pii = C.pii_ids(cols_cfg)

    hosp_key = row["source_dir"].split("/")[0]
    hosp_cfg = cfg["hospitals"].get(hosp_key) or {}
    # 表里没这个键（或 std_name 空）⇒ 用源目录名原样兜底。raid5 语料顶层就是中文院名，
    # 不是拼音缩写；兜底值是照抄目录名，不是猜中文院名（红线 E）。
    # 表里有映射的照旧走映射，老批次（奉化/安吉/桐乡二院/玉环第一/长兴人民）取值不变。
    hosp_std = str(hosp_cfg.get("std_name") or "").strip()
    hosp_rule = "config:hospitals"
    if not hosp_std:
        own = hospital_ocr_name(batch_dir, case)
        top = hospital_dir_vote(batch_dir, hosp_key, own)
        if own:
            hosp_std, hosp_rule = own, "ocr:首页/已OCR页"
        elif top:
            hosp_std, hosp_rule = top, "ocr:同目录多数票"
    # 只有目录名本身是中文时才允许照抄（raid5 语料顶层是拼音 slug ⇒ 照抄=脏数据）
    if (not hosp_std and bool(cfg["normalize"].get("hospital_std_from_dir_name", True))
            and HOSP_CJK_RE.search(hosp_key)):
        hosp_std = hosp_key.strip()
        hosp_rule = "source_dir:目录名原样"
        if bool(cfg["normalize"].get("hospital_std_strip_dir_date_suffix", True)):
            m = DIR_SCAN_DATE_RE.match(hosp_std)
            if m:
                base = m.group("base").strip()
                std2 = str((cfg["hospitals"].get(base) or {}).get("std_name") or "").strip()
                if std2:
                    hosp_std, hosp_rule = std2, "config:hospitals(去扫描日期后缀)"
                else:
                    hosp_std, hosp_rule = base, "source_dir:目录名去扫描日期后缀"

    # J3 的核对文本 = 真正进过 prompt 的那几页首页正文（含副变体），
    # 与 s3 拼 prompt 时用的 page_text() 逐字一致 —— 否则「核对的文本」≠「模型看到的文本」。
    _src_cache = {}

    def src_norm_text():
        if "t" in _src_cache:
            return _src_cache["t"]
        from wide import s3_extract as S3
        pages = set(fp.get("frontpage_pages") or [])
        parts = []
        for pj in sorted((batch_dir / "02_text" / case).glob("page_*.json")):
            r = C.read_json(pj)
            if r.get("page") in pages:
                parts.append(S3.page_text(r))
        _src_cache["t"] = (re.sub(r"[\s|]+", "", "\n\n".join(parts)) if parts else None)
        return _src_cache["t"]

    fields: dict[str, dict] = {}
    for col in cols_cfg["columns"]:
        cid, src_kind = col["id"], col["source"]
        rec = {"value": None, "source_label": None, "strategy": None,
               "mapping_version": map_version, "column_source": src_kind}
        if src_kind == "derived":
            if cid == "source_dir":
                rec.update(value=row["source_dir"], strategy="derived")
            elif cid == "input_file":
                rec.update(value=row["input_file"], strategy="derived")
            elif cid == "seq_no":
                rec.update(value=seq_no, strategy="derived")
        elif src_kind == "none":
            rec["null_reason"] = col.get("note", "无既定来源")
        elif src_kind == "first_progress":
            v = (pn.get("values") or {}).get(col["header"])
            if isinstance(v, dict):
                rec.update(value=clean_value(v.get("value"), null_tokens),
                           source_label=v.get("heading"), strategy="llm",
                           llm_status=pn.get("status"))
            else:
                rec.update(value=clean_value(v, null_tokens), strategy="llm",
                           llm_status=pn.get("status"))
            vb = (pn.get("verbatim") or {}).get(col["header"])
            if vb is not None:
                rec["verbatim"] = vb          # 原文照抄核对结果（布尔+长度，不含原文）
        elif src_kind in ("frontpage", "normalized", "computed"):
            def _guarded(h, _col=col, _cid=cid, _kind=src_kind):
                if _col.get("widget") == "checkbox":
                    h = guard_checkbox(h, cfg["normalize"].get("checkbox") or {})
                # round16 7f: ID 列的跨栏尾巴必须在**来源守卫之前**砍掉 —— 否则守卫会因为
                # 「整串（含尾巴）在首页正文里找不到」而把本来有值的格判成缺失。
                # round19 合并: 放进 _guarded，规则命中与 LLM/strategies 命中走同一条守卫链。
                if h and _cid in ID_TOKEN_COLS and h.get("value") is not None:
                    head, tail = trim_id_tail(h["value"])
                    if head and head != str(h["value"]).strip():
                        h["pre_trim_value"] = h["value"]
                        h["value"] = head
                        h["id_tail_trimmed"] = tail
                        C.warn(f"{case}: {_cid} 去掉尾随串 {tail!r} → {head!r}")
                if _kind == "frontpage":
                    h = guard_provenance(h, _cid, label_sets, cfg["normalize"], pii,
                                         src_norm_text())
                return h

            # ── round18 第3步：冲突消解 = **规则优先、LLM 兜底**（改调用点，不改 merge_priority）。
            # 为什么改 priority 是空操作：s4_normalize.py:320 判的是 `"llm" in priority`，
            #   与 "llm" 在列表里的**位置无关** → 把 rule 排到前面不会改变任何行为。
            # 为什么规则赢：规则命中天然带 source_label（= 命中的标签词本身），离线实测
            #   1793/1793 过 J2/J3；LLM 的 adm_no 命中里 source_label 常是 null 或
            #   「住院病案首页」（文档标题）→ 被来源守卫判坏，现网 adm_no 只有 7/10 就是这么来的。
            # 规则被守卫判坏时**不锁死**：回落到 LLM 路径 → 命中数只增不减（红线⑦）。
            hit = None
            rule_rejected = None
            rule_hit = pick_from_rule(strategies, cid, null_tokens)
            if rule_hit is not None:
                rule_hit = _guarded(rule_hit)
                if rule_hit is not None and rule_hit.get("value") is not None:
                    hit = rule_hit
                else:
                    rule_rejected = (rule_hit or {}).get("raw_rejected")
            if hit is None:
                hit = pick_from_llm(llm, cid, null_tokens) if "llm" in priority else None
                if hit is None:
                    hit = pick_from_strategies(strategies, llm,
                                               labels_by_col.get(cid, []), priority, null_tokens)
                hit = _guarded(hit)
            if hit:
                rec.update(hit)
            if rule_rejected is not None:
                rec["rule_rejected"] = rule_rejected
            if hit and hit.get("raw_option_text"):
                rec["raw_option_text"] = hit["raw_option_text"]
        fields[cid] = rec

    # ---- round33 第1.1件：年龄去单位（只剥「岁」后缀，不做语义解析） ----
    fa = fields.get("age")
    if fa is not None and fa.get("value") is not None:
        raw_age = fa["value"]
        v, how = norm_age(raw_age)
        fa["age_norm"] = how
        if how == "unparseable":
            fa["pre_norm_value"] = raw_age
            fa["value"] = None
            fa["null_reason"] = f"年龄原文无法解析为数字：{raw_age!r}"
        elif how != "already_numeric":
            fa["pre_norm_value"] = raw_age
            fa["value"] = v

    # ---- round33 第1.2/1.3件：先记下页面印到的时间精度（必须在标准化列之前） ----
    keep_time = bool((cfg["normalize"].get("date_time") or {}).get("keep", True))
    fam = fields.get("admit_time_raw")
    if fam is not None and fam.get("value") is not None:
        fam["admit_time_precision"] = time_precision(fam["value"])

    # ---- 标准化列 ----
    raw_case = fields.get("case_no", {}).get("value")
    std_case = norm_case_no(raw_case, hosp_key, cfg["normalize"])
    fields["case_no_std"] = {"value": std_case, "strategy": "normalized",
                             "from": "case_no", "rule": "strip_nonalnum+upper",
                             "mapping_version": map_version}
    raw_admit = fields.get("admit_time_raw", {}).get("value")
    iso, how = parse_cn_date(raw_admit) if raw_admit else (None, "no_value")
    fields["admit_time_std"] = {"value": iso, "strategy": "normalized", "from": "admit_time_raw",
                                "rule": how, "raw": raw_admit, "mapping_version": map_version}
    if keep_time and iso:
        prec = (fields.get("admit_time_raw") or {}).get("admit_time_precision", "none")
        fields["admit_time_std"]["admit_time_precision"] = prec
        fields["admit_time_std"]["pre_norm_value"] = iso
        fields["admit_time_std"]["value"] = iso_display(iso, prec)
        fields["admit_time_std"]["date_norm"] = ("iso_datetime" if prec == "minute"
                                                 else "iso_date")
    fields["hospital_std"] = {"value": hosp_std or None, "strategy": "normalized",
                              "from": "source_dir", "rule": hosp_rule,
                              "mapping_version": map_version}

    # ---- 日期类列统一显示格式（必须在 los_days 等派生列**之前**）----
    # 顺序无影响的依据：los_days 的 path B 走 to_iso_date()→parse_cn_date()，
    # 它对「YYYY年MM月DD日」和 ISO 都能解析，且只取 [:10] 的日期部分 → 归一化前后同值。
    date_norm_log: list[dict] = []
    for cid in DATE_DISPLAY_COLS:
        f = fields.get(cid)
        if not f or f.get("value") is None:
            continue
        # round33：入院时间三列不再压成「YYYY年MM月DD日」——时刻是页面上印着的，
        # 丢掉它等于把已有字段做没（红线⑦）。discharge_date 维持原行为不动。
        if keep_time and cid in DATE_KEEP_TIME_COLS:
            if cid == "admit_time_raw":
                trimmed = trim_seconds(f["value"])
                if trimmed != str(f["value"]).strip():
                    f["pre_norm_value"] = f["value"]
                    f["value"] = trimmed
                    f["date_norm"] = "verbatim_trim_seconds"
                else:
                    f["date_norm"] = "verbatim"
                date_norm_log.append({"column_id": cid, "action": "verbatim",
                                      "precision": f.get("admit_time_precision")})
            else:
                date_norm_log.append({"column_id": cid, "action": "iso",
                                      "precision": f.get("admit_time_precision")})
            continue
        disp, matched, dropped = norm_date_display(f["value"])
        if not matched:
            f["date_format_unmatched"] = True
            date_norm_log.append({"column_id": cid, "action": "unmatched"})
            C.warn(f"{case}: {cid} 日期格式无法归一化，保留原值 {f['value']!r} 并打 flag")
            continue
        f["pre_norm_value"] = f["value"]
        f["value"] = disp
        f["date_norm"] = "ymd_display"
        if dropped:
            f["date_time_dropped"] = True
        date_norm_log.append({"column_id": cid, "action": "normalized",
                              "time_dropped": dropped})

    # ---- 住院天数：两条路都试（疑点④）----
    los_cfg = cfg["normalize"]["los"]
    a_val = None
    for lab in [los_cfg["path_a_label"], "住院天数", "实际住院天数"]:
        hit = pick_from_strategies(strategies, llm, [lab], priority, null_tokens)
        if hit:
            a_val = hit
            break
    path_a = None
    if a_val:
        m = re.search(r"\d+", str(a_val["value"]))
        path_a = int(m.group(0)) if m else None
    d_in = to_iso_date(fields["admit_time_std"].get("value") or raw_admit or "")
    d_out = to_iso_date(fields.get("discharge_date", {}).get("value") or "")
    path_b = (d_out - d_in).days if (d_in and d_out) else None
    tol = int(los_cfg["tolerance_days"])
    if path_a is not None and path_b is not None:
        agree = abs(path_a - path_b) <= tol
        los_val, how2 = path_a, "path_a(首页直写)"
    elif path_a is not None:
        los_val, how2, agree = path_a, "path_a(首页直写)", None
    elif path_b is not None:
        los_val, how2, agree = path_b, "path_b(出院−入院)", None
    else:
        los_val, how2, agree = None, "none", None
    fields["los_days"] = {"value": los_val, "strategy": "computed",
                          "path_a_frontpage": path_a, "path_b_discharge_minus_admit": path_b,
                          "path_used": how2, "paths_agree": agree,
                          "mapping_version": map_version}

    # ---- 重名/镜像列：各写各的，但同源同值（**不合并**，模板原样保留）----
    # 哪些列是镜像由 config/columns.yaml:mirror_of 决定，不在代码里点名。
    for col in cols_cfg["columns"]:
        base, dup = col.get("mirror_of"), col["id"]
        if not base or dup not in fields or base not in fields:
            continue
        fields[dup] = dict(fields[base])
        fields[dup]["duplicate_of"] = base
        fields[dup]["note"] = f"模板重名列，与 {base} 同源同值（未合并）"

    out = {"case_id": case, "mapping_version": map_version,
           "frontpage_pages": fp.get("frontpage_pages"), "fields": fields,
           "date_norm_log": date_norm_log}
    C.write_json(dst / "fields.json", out)
    n_null = sum(1 for f in fields.values() if f.get("value") is None)
    n_guarded = sum(1 for f in fields.values() if f.get("raw_rejected"))
    C.write_json(dst / "_manifest.json", {"case_id": case, "n_columns": len(fields),
                                          "n_null": n_null, "n_guarded_null": n_guarded,
                                          "n_date_normalized": sum(
                                              1 for x in date_norm_log
                                              if x["action"] == "normalized"),
                                          "n_date_unmatched": sum(
                                              1 for x in date_norm_log
                                              if x["action"] == "unmatched"),
                                          "n_id_tail_trimmed": sum(
                                              1 for f in fields.values()
                                              if f.get("id_tail_trimmed")),
                                          "errors": []})
    C.write_marker(dst / ".done")
    audit.emit(case_id=case, kind="normalize", mapping_version=map_version,
               n_columns=len(fields), n_null=n_null, n_guarded_null=n_guarded,
               fields_sha256=C.sha256_text(C.canon_json(fields)))
    C.log(f"    归一化 {len(fields)} 列，null {n_null}"
            + (f"（其中来源守卫判坏 {n_guarded}）" if n_guarded else ""), quiet=quiet)
    return {"case_id": case, "n_null": n_null}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="阶段④ 字段名归一化")
    ap.add_argument("--batch", required=True)
    ap.add_argument("--case", action="append")
    ap.add_argument("--mapping", default="", help="默认 config/field_mapping.yaml")
    ap.add_argument("--case-file", default="", help="从文件读 case_id，一行一个")
    ap.add_argument("--allow-draft", action="store_true",
                    help="允许用 status=draft 的映射表试跑（正式回填必须用人工确认过的表）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-provenance-guard", action="store_true",
                    help="临时关掉来源守卫（只为对照/回滚，会大声告警）。"
                         "默认开；正式跑批不要用这个开关把坏值放回去。")
    ap.add_argument("--no-keep-admit-time", action="store_true",
                    help="round33 对照/回滚开关：把入院时间的时刻重新丢弃、"
                         "三列恢复成「YYYY年MM月DD日」（round16 第7步的旧行为）。"
                         "只为对照/回滚，正式跑批不许关（会少字段，红线⑦）。")
    a = ap.parse_args(argv)
    cfg = C.load_pipeline()
    cols = C.load_columns()
    if a.no_provenance_guard:
        cfg["normalize"].setdefault("provenance_guard", {})["enabled"] = False
        C.warn("来源守卫已用 --no-provenance-guard 关闭：来路不明的值会被原样写进宽表。"
               "这只允许用于开/关对照或人工回滚，正式跑批必须保持开启。")
    if a.no_keep_admit_time:
        cfg["normalize"].setdefault("date_time", {})["keep"] = False
        C.warn("入院时间时刻已用 --no-keep-admit-time 丢弃：入院时间三列退回"
               "「YYYY年MM月DD日」。这只允许用于开/关对照或人工回滚，"
               "正式跑批必须保持开启（关掉等于把页面上印着的时刻做没）。")
    mpath = Path(a.mapping or cfg["paths"]["mapping"])
    if not mpath.exists():
        C.err(f"映射表不存在：{mpath}（先跑 tools/pass1_mapping.py 并由人确认）")
        return 2
    mapping = C.load_yaml(mpath)
    map_status = str(mapping.get("status") or "")
    C.log(f"映射表版本：{mapping.get('version')}  status={map_status}")
    if map_status != "confirmed" and not a.allow_draft:
        C.err(f"映射表 status={map_status or '未知'}，不是 confirmed —— "
              f"第一遍映射必须人工确认后才回填宽表（任务书第四节）。映射表：{mpath}"
              f"（version={mapping.get('version')}）。\n"
              f"    试跑请加：--allow-draft\n"
              f"    整条链试跑请用：python -m wide.tools.run_steps --batch <batch> "
              f"--case <case_id> --allow-draft\n"
              f"    正式回填：由人把 {mpath} 的 status 改成 confirmed（产线不改）。")
        return 3
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    audit = C.Audit(batch_dir / "_audit.jsonl", STAGE)
    from wide import s1_ocr as S1
    rows_all = S1.read_manifest(batch_dir / "00_index" / "manifest.tsv")
    # round19 修复「序号全是 1」：seq_no 必须是**整份 manifest 里的行号**
    # （columns.yaml:206「产线生成的行序号（1 起，按 manifest 顺序）」），
    # 不是「本次 --case 过滤后子集的下标」。产线 run_steps 每例单独调一次 s4 →
    # 过滤后 rows 只剩 1 行 → enumerate(start=1) 永远从 1 开始 → 全表序号全是 1。
    seq_by_case = {r["case_id"]: i for i, r in enumerate(rows_all, start=1)}
    rows = rows_all
    want = list(a.case or [])
    if a.case_file:
        want += [x.strip() for x in Path(a.case_file).read_text(encoding="utf-8").splitlines()
                 if x.strip()]
    if want:
        rows = [r for r in rows if r["case_id"] in set(want)]
    n_done, n_incomplete, n_skipped = 0, [], 0
    for i, r in enumerate(rows, start=1):
        dst = batch_dir / STAGE / r["case_id"]
        if (dst / ".done").exists() and not a.force:
            C.log(f"  跳过（已 .done）{r['case_id']}", quiet=a.quiet)
            n_skipped += 1
            continue
        res = process_case(r, cfg, cols, mapping, batch_dir, audit,
                           seq_by_case.get(r["case_id"], i), a.force, a.quiet)
        if res.get("errors"):
            n_incomplete.append(res["case_id"])
        else:
            n_done += 1
    if not rows:
        C.err("manifest 里没有匹配的例 —— 什么都没归一化")
        return 2
    # 旧行为：全部例都 .incomplete（阶段③没产物）也返回 0，下游 s6 只得到空表。
    if n_done == 0 and n_skipped == 0:
        C.err(f"{len(rows)} 例全部没有阶段③产物（03_extract/<case>/frontpage.json），"
              f"没有生成任何 fields.json —— 先跑阶段①②③")
        return 1
    if n_incomplete:
        C.warn(f"{len(n_incomplete)}/{len(rows)} 例缺阶段③产物，标 .incomplete："
               f"{n_incomplete[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
