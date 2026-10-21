#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宽表「怎么把三态渲染成格子」的唯一权威 —— 与 f3_to_wide.py 分离，便于单测。

★ 它要解决的那个真问题（2026-10-03 实测，104 行 × 503 列 = 52,312 格）：

    原写法 `str((items.get(c) or {}).get("值") or "")` 只写「值」，
    而阴性/待确认的「值」是 None → `or ""` → 落成空串。
    后果：**32,631 个阴性（有确定结论）与 7,255 个待确认全部被写成空白**，
    占全部空格 40,000 的 99.7%。

    所以这里的核心不是「补默认值」，而是**按 state 分别渲染**：
        阳性    → 值（占位词单独识别，不算有效填充）
        阴性    → 按列类型渲染成 否 / 未做 / 未查 / 未用 / 不适用 / 待人工
        待确认  → **照写「待确认」，绝不写空**（项目铁律：静默失败是最坏的错）

★ 为什么阴性也要写文本：空单元格在登记表里是**歧义**的 ——
    下游分不清「没有」和「没抽到」。阴性是有结论的，必须写出来。

★ 兼容性：老的下游可能硬编码「空 = 否」。所以渲染开关叫 `--fill-mode`，
    `compat` 保持旧行为（空串），`filled` 是新行为。默认 `filled`
    —— 因为空值的默认语义「歧义」比「多写了两个字」糟得多。
"""
from __future__ import annotations

import re
from collections import Counter

# ── 占位词：写进表里看着"非空"，其实是没有答案 ────────────────────────
# 它们必须被**单独统计**，否则填充率会被虚增（实测虚增 2,089 格 = 3.99%）。
PLACEHOLDERS = ("未提及", "无", "不详", "未知", "unknown", "N/A", "-", "—")

STATE_POS = "阳性"
STATE_NEG = "阴性"
STATE_PEND = "待确认"

# ── 「阴性」不是一个，是两个（route_f3.py:1224 的原话）────────────────
# ★ 这是 2026-10-03 下午被纠出来的**真 bug**：
#   我第一版把所有 阴性 一律渲染成 否/未做/未查/未用 —— 那等于把
#   「配置的猜测」当成「事实」写进了交付表。route_f3 里阴性有两条来路：
#
#   ① 逐条否定证据（why=「词条出现但被否定（前置或后置）」）—— 有据，可写「否」
#   ② 配置断言/段存在推断（why 含 配置断言 / 来源段存在 / 体健）
#      —— 这是「没找到就当没有」，confidence_of 只给 CONF_MED，
#         它的错法恰恰是假阴性。**写成「未做」就是把猜测当事实。**
#
#   实测：56 个零命中列（影像/化验/用药，全部 table-bound）的阴性 100% 是②
#   ——「来源段存在却没命中 → 否」。把 100 个卒中病人渲染成「头颅CT 未做」
#   在临床上不可能是真的；那只是关键词没对上表。
#   所以②必须渲染成「未提及」（= 文档没提到，可审计、可统计，但不断言）。
UNGROUNDED_MARKERS = ("配置断言", "来源段存在", "体健")
GROUNDED_MARKERS = ("被否定",)          # 「词条出现但被否定（前置或后置）」
NA_MARKERS = ("不适用",)                # 条件列/备注列非必填


def neg_grounded(why) -> bool:
    """这条阴性有没有否定证据支撑。

    ★ **认不出的 why 一律按「没据」处理** —— 宁可少写一个「否」，
      也不能把猜测渲染成事实（route_f3 的注释原话：把「没找到」当成「没有」）。
    """
    w = str(why or "")
    if not w:
        return False                     # 没有 why → 不敢说有据
    if any(m in w for m in UNGROUNDED_MARKERS):
        return False
    if any(m in w for m in NA_MARKERS):
        return False
    return any(m in w for m in GROUNDED_MARKERS)


def neg_is_na(why) -> bool:
    """这条阴性是不是「条件列/备注列非必填」那种不适用。"""
    return any(m in str(why or "") for m in NA_MARKERS)

# ── 列类型 → 阴性渲染文本 ────────────────────────────────────────────
# 「未做 / 未查 / 未用」不是修辞：它们是**登记表里可核对的答案**，
# 而「空」是不可核对的。这三者的区别决定了下游能不能拿去统计。
NEG_TEXT = {
    "bool": "否",          # 既往史 / 合并症 / 禁忌症
    "check": "未做",       # 影像、检查项目
    "lab": "未查",         # 检验数值
    "drug": "未用",        # 用药明细
    "conditional": "不适用",  # 仅 TIA / 仅醒后卒中等条件列
    "manual": "待人工",    # 序号 / 质控打分（按设计不由抽取链路填）
    "text": "未记录",      # 兜底：自由文本 / 标识号 / 人口学
}
# ★ 兜底必须是 text（"未记录"），不能是 bool（"否"）。
#   实测：498 列里有 300 列是 病案号/姓名/年龄/籍贯/地址 这类**标识与文本列**，
#   默认成 bool 会把「病案号」的阴性渲染成「否」——那是错的，而且错得很静默。
DEFAULT_KIND = "text"

# 规则**按顺序**匹配，先命中先生效。顺序错了会把「既往高同型半胱氨酸血症」
# 这类布尔列误判成检验列 —— 实测过，所以每条都带排除条件。
_RULES: list[tuple[str, str, str]] = [
    # (kind, 必须命中, 命中了也要排除)
    ("manual",      r"^(序号|质控打分)$", ""),
    ("conditional", r"(仅TIA|适用于醒来卒中|需要填写)", ""),
    # 用药：**必须**带 用药时间/剂量/频次/给药 等词，或「住院|出院时|入院前」+具体药名。
    #   光有「住院」不算（住院天数/住院总费用 不是药）；「目前用药」是自由文本列 → 落 text。
    ("drug",        r"(用药时间|使用剂量|使用频次|给药|药物名称|"
                    r"(?:住院|出院时|入院前)(?:阿司匹林|氯吡格雷|他汀|阿托伐他汀|"
                    r"阿加曲班|阿哌沙班|辛伐他汀|西洛他汀|西洛他唑|双嘧达莫|"
                    r"低分子肝素|华法林|替罗非班|丁苯酞|依达拉奉|甘露醇|尿激酶|阿替普酶))",
                    ""),
    # 检查项目：以「检查」结尾，或含影像模态词；**排除**既往史布尔列
    ("check",       r"(检查$|检查时间$|检查结果|造影|B超|超声|CTA|MRI|CT|DWI|TCD|"
                    r"DSA|灌注|多普勒|心电图|血管成像|核医学)",
                    r"^既往|史$"),
    # 检验数值：带单位括号 / 明确的检验项名；**排除**「…史」这种既往史布尔列
    #   （实测踩过：「抗血小板药物史」被「血小板」命中，误判成检验列）
    ("lab",         r"(（[^）]*(?:mg|mmol|umol|g/L|%|s|pg|UI|dL|FEU|ml)[^）]*）|"
                    r"检查结果|检查时间|血糖|糖化血红蛋白|肌酐|尿素|胆固醇|"
                    r"甘油三酯|脂蛋白|半胱氨酸|C反应蛋白|CRP|BNP|血红蛋白)",
                    r"^既往|史$"),
    # 既往史 / 合并症 / 家族史 / 禁忌症 / 是否… → 布尔
    ("bool",        r"(^既往|^是否有|^是否存在|^是否|^选择|史$|病史|家族史|"
                    r"禁忌症|并发症|合并症)", ""),
]


def col_kind(name: str) -> str:
    """列名 → 列类型（决定阴性渲染成什么）。纯函数，无副作用。"""
    n = str(name or "").strip()
    for kind, must, not_if in _RULES:
        if re.search(must, n):
            if not_if and re.search(not_if, n):
                continue
            return kind
    return DEFAULT_KIND


def is_placeholder(v) -> bool:
    """值是不是"看着有内容、其实没答案"的占位词。"""
    return str(v or "").strip() in PLACEHOLDERS


def render(state, value, kind, fill_mode="filled", why=None):
    """三态 → 写进单元格的文本。这是整个修复的落点。

    compat 模式保持旧行为（阴性/待确认 → 空串），只为兼容硬编码「空=否」的下游。

    ★ 阴性按 `why` 分两路（见 UNGROUNDED_MARKERS 的说明）：
        有否定证据 → 否/未做/未查/未用（按列类型）
        配置断言   → 「未提及」—— **不写「未做」**，那是把猜测当事实
        不适用     → 「不适用」
        why 认不出 → 「未提及」（宁可少断言）
    """
    st = str(state or "").strip()
    v = "" if value is None else str(value).strip()

    if fill_mode == "compat":
        # 旧行为：只写值。保留它，是为了让改动可回滚、可 A/B。
        return v

    if st == STATE_POS:
        # 阳性契约：必须有值。占位词原样保留（它本身就是答案："提到了但没给数"）。
        return v or "未提及"
    if st == STATE_NEG:
        if neg_is_na(why):
            return "不适用"
        if neg_grounded(why):
            return NEG_TEXT.get(kind, NEG_TEXT[DEFAULT_KIND])
        return "未提及"                # 配置断言/段存在推断：不冒充「没做」
    if st == STATE_PEND:
        return STATE_PEND          # ★ 绝不写空
    # state 缺失（结果文件里压根没有这一列）→ 也是待确认，不是空
    return STATE_PEND


# ── 统计口径：三个必须分开报，混着报会得出相反结论 ────────────────────
def fill_stats(cell_states, cell_values, cell_whys=None):
    """cell_states/cell_values（可选 cell_whys）是等长的迭代（逐格）。

    返回三个**口径不同**的数，项目文档里因为混用它们出现过 6 倍误差：
      有效填充率 = 阳性且值不是占位词 / 总格数          ← 「真的拿到了值」
      覆盖率     = (阳性 + 阴性) / 总格数               ← 「有结论」（含"没有"）
      有据覆盖率 = (阳性−占位 + 有据阴性) / 总格数       ← ★结论有证据撑着

    ★ 为什么要第三个：阴性分两路（UNGROUNDED_MARKERS）。
      32,631 个阴性若全是配置断言，那么「覆盖率 86%」就是把猜测当结论 ——
      实测 56 个零命中列正是这种情况。有据覆盖率才配得上「结论」两个字。
      配置断言的阴性在表里渲染成「未提及」，它不是空格，但也不是结论。
    """
    tot = pos = neg = pend = ph = neg_g = 0
    ws_iter = cell_whys if cell_whys is not None else [""] * tot
    for i, (st, v) in enumerate(zip(cell_states, cell_values)):
        tot += 1
        s = str(st or "").strip()
        if s == STATE_POS:
            pos += 1
            if is_placeholder(v):
                ph += 1
        elif s == STATE_NEG:
            neg += 1
            w = cell_whys[i] if cell_whys is not None else None
            if neg_grounded(w):
                neg_g += 1
        else:
            pend += 1
    return {
        "总格数": tot, "阳性": pos, "阴性": neg, "待确认": pend,
        "占位": ph,
        "阴性_有据": neg_g,
        "阴性_配置断言": neg - neg_g,
        "有效填充率": (pos - ph) / tot if tot else 0.0,
        "覆盖率": (pos + neg) / tot if tot else 0.0,
        "有据覆盖率": (pos - ph + neg_g) / tot if tot else 0.0,
    }


# ── 写盘门禁 ─────────────────────────────────────────────────────────
# 现在的链路一条门禁都没有：106 份源文件只出 104 行、整行 99.4% 空，
# 全都静默落盘了。**假绿灯比报错糟得多**，所以这里一律 exit 而不是 warn。
GATES = {
    "min_fill_rate": 0.15,      # 有效填充率低于此 → 拒绝（疑似整批没抽到）
    "min_coverage": 0.80,       # 覆盖率低于此 → 拒绝（疑似段解析整体错位）
    "max_zero_hit_cols": 80,    # 阳性=0 的列超过此数 → 拒绝（疑似字段映射错位）
    "max_bad_rows": 5,          # 空值率 >95% 的行超过此数 → 拒绝（疑似 OCR 挂了）
    "bad_row_rate": 0.95,
}


def check_gates(st, n_rows, zero_hit_cols, bad_rows, missing_cases,
                empty_cells_in_filled_mode, gates=None):
    """返回 (是否通过, [失败原因...])。空列表 = 通过。"""
    g = dict(GATES)
    g.update(gates or {})
    bad = []
    if st["总格数"] == 0:
        return False, ["一个格子都没有"]
    if st["有效填充率"] < g["min_fill_rate"]:
        bad.append(f"有效填充率 {st['有效填充率']:.2%} < {g['min_fill_rate']:.0%}"
                   f"（疑似整批没抽到值）")
    if st["覆盖率"] < g["min_coverage"]:
        bad.append(f"覆盖率 {st['覆盖率']:.2%} < {g['min_coverage']:.0%}"
                   f"（疑似章节解析整体错位）")
    if len(zero_hit_cols) > g["max_zero_hit_cols"]:
        bad.append(f"零命中列 {len(zero_hit_cols)} > {g['max_zero_hit_cols']}"
                   f"（疑似字段映射错位）")
    if len(bad_rows) > g["max_bad_rows"]:
        bad.append(f"空值率>{g['bad_row_rate']:.0%} 的行 {len(bad_rows)} > "
                   f"{g['max_bad_rows']}（疑似整批 OCR 挂了）")
    if missing_cases:
        bad.append(f"**源文件 {len(missing_cases)} 份没有对应输出行** "
                   f"（每个 PDF = 一例，不能静默丢）：{missing_cases[:5]}")
    if empty_cells_in_filled_mode:
        bad.append(f"filled 模式下仍有 {empty_cells_in_filled_mode} 个空单元格"
                   f"（应为 0）")
    return (not bad), bad


# ── 异常清单：每次跑批必出，不给"静默失败"留地方 ──────────────────────
def build_abnormal(result_cases, manifest_names, rows, flat, stat, bad_rows,
                   zero_hit_cols, st):
    """返回 [(表名, 表头, 行...)]，直接给 f3_to_wide 写成 sheet。"""
    out = []

    # ① 文件对账：**每个 PDF = 一例**，少一行就是丢了一份病历
    missing = [n for n in manifest_names if n not in result_cases]
    extra = [c for c in result_cases if manifest_names and c not in manifest_names]
    out.append((
        "① 文件对账",
        ["项", "数量", "说明"],
        ["源文件数（manifest）", len(manifest_names),
         "每个 PDF = 一例"],
        ["输出行数", len(rows), "应等于源文件数"],
        ["差额", len(manifest_names) - len(rows),
         "**非 0 就是丢了病历**" if len(manifest_names) != len(rows) else "✓ 对得上"],
        ["缺失的病例名", len(missing),
         "；".join(missing[:10]) + ("…" if len(missing) > 10 else "") or "—"],
        ["多出来的病例名", len(extra),
         "；".join(extra[:10]) + ("…" if len(extra) > 10 else "") or "—"],
    ))

    # ② 抽崩的行 → 整份 PDF 抽取失败的样本
    #    ★ 用**待确认率**判定：filled 模式下空格已被消灭，空值率恒为 0，
    #      真正能反映「这一例没抽到」的是待确认率（项目里 99.4% 空的那两行
    #      就是这么暴露出来的）。
    t2 = [["行号", "病例名", "待确认率", "待确认格数", "空值率", "空值格数",
           "占位格数", "判定"]]
    for r in bad_rows:
        no, case, pend_r, pend_n, blank_r, blank_n, ph_n = r
        t2.append([no, case, f"{pend_r:.1%}", pend_n, f"{blank_r:.1%}", blank_n,
                   ph_n, "整份抽取失败 —— 查源 PDF 是不是空白页/方向错"])
    if len(t2) == 1:
        t2.append(["—", "—", "—", "—", "—", "—", "—", "无（全部行都达标）"])
    out.append(("② 抽崩的行", t2[0], *t2[1:]))

    # ③ 零命中列 → 要区分「确实没做」还是「字段映射错了」
    #    ★ 判据看**阴性里配置断言的占比**（不是翻 PDF）：
    #      整片阴性且几乎全是配置断言 = 关键词没对上的指纹，
    #      真正"没做"在 107 例里应该是零星的。
    t3 = [["列名", "列类型", "阳性", "阴性", "其中配置断言", "待确认",
           "本批渲染为", "判定"]]
    for c in zero_hit_cols:
        s = stat[c]
        k = col_kind(c)
        neg = s.get(STATE_NEG, 0)
        cfg = s.get("阴性_配置断言", 0)
        if neg == 0:
            shown, verdict = NEG_TEXT.get(k, NEG_TEXT[DEFAULT_KIND]), \
                "真失败：查 source_sections / OCR 产出"
        elif cfg and cfg / max(neg, 1) >= 0.8:
            shown = "未提及（配置断言，**不断言「没做」**）"
            verdict = ("整片阴性是**配置断言**的指纹 —— 词很可能在正文里，"
                       "只是没对上列。跑 f3_zero_hit_diag.py 分"
                       "「映射错 / 上游没产出」，两者修法相反")
        else:
            shown = NEG_TEXT.get(k, NEG_TEXT[DEFAULT_KIND])
            verdict = "有据阴性为主 → 可当「没做/未查」；再核对 1~2 份原文"
        t3.append([c, k, s.get(STATE_POS, 0), neg, cfg, s.get(STATE_PEND, 0),
                   shown, verdict])
    if len(t3) == 1:
        t3.append(["—", "—", "—", "—", "—", "—", "—", "无"])
    out.append(("③ 零命中列", t3[0], *t3[1:]))

    # ④ 口径
    cfg = st.get("阴性_配置断言", 0)
    out.append((
        "④ 口径",
        ["指标", "值", "定义"],
        ["总格数", st["总格数"], f"{len(rows)} 行 × {len(flat)} 列（不含前置列）"],
        ["阳性", st["阳性"], "抽到了值"],
        ["其中占位词", st["占位"], "「未提及/无」等，看着非空其实没答案"],
        ["阴性", st["阴性"], "其中**有据**（有否定证据）"
         f"{st.get('阴性_有据', 0)}、**配置断言** {cfg}"],
        ["待确认", st["待确认"], "没抽到，已照写「待确认」"],
        ["**有效填充率**", f"{st['有效填充率']:.2%}", "(阳性 − 占位) / 总格数"],
        ["**覆盖率**", f"{st['覆盖率']:.2%}", "(阳性 + 阴性) / 总格数"],
        ["**有据覆盖率**", f"{st.get('有据覆盖率', 0):.2%}",
         "(阳性 − 占位 + 有据阴性) / 总格数 —— "
         "配置断言的阴性不算结论，**这个数最诚实**"],
        ["⚠ 三者不可混用", "", "覆盖率≠准确率；没有人工金标准算不出准确率"],
    ))
    return out


def kind_histogram(names):
    """列名列表 → 各类型的计数。给自检和单测用。"""
    return Counter(col_kind(n) for n in names)
