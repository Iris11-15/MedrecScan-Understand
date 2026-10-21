"""OCR → 中间文件 → 大模型分析 的三层流水线。

    图片/PDF ──[PaddleOCR-VL]──▶ 结构化块 ──[build]──▶ job 目录 ──[analyze]──▶ 分析结果
                                      │                    │
                                 保留坐标溯源         给大模型的干净文本

为什么中间要隔一层文件：
    直接拿 OCR 的原始 JSON 喂大模型是常见错误。那一份 31 块的 JSON 有 58 KB，
    其中 bbox 坐标占了大半 —— 对大模型毫无价值，纯粹烧 token。
    转成 Markdown 后通常只剩几 KB，结构和阅读顺序都还在。

为什么用「文件」而不是「内存里传」：
    1. 可复现：同一份中间文件可以换不同提示词、不同模型反复分析，不用重跑 OCR
    2. 可解耦：OCR 在 GPU 机器上跑，分析可以在另一台机器 / 另一个服务上做
    3. 可审计：每一份结论都能追回它读的是哪份文本

用法：
    # 1) OCR 结果 -> job 目录
    python stack.py build --from-json output/demo_real_res.json --name 示例
    python stack.py build --image page.png --name 合同A          # 顺便调 OCR

    # 2) 看中间文件长什么样
    python stack.py show jobs/示例

    # 3) 交给大模型分析
    python stack.py analyze jobs/示例 --task 摘要
    python stack.py analyze jobs/示例 --task 字段核对 --prompt my.txt

    # 4) 一条龙
    python stack.py run page.png --task 摘要

    # 5) 脱敏与还原
    python stack.py build --image 病历.pdf --name 病历001 --redact basic
    python stack.py restore jobs/病历001 jobs/病历001/analysis_摘要_xxx.md

脱敏（默认开启，`--redact off` 可关）：
    只脱**身份标识**，保留临床信息。年龄 / 性别 / 发病日期 / 诊断 / 用药 /
    检验值一律不动 —— 过度脱敏会让大模型没法分析。
    产出 document.md（已脱敏，可外发）+ redaction_map.json（含原文，只留本地）
    + 脱敏报告.md（逐条列出替换了什么，供人工复核）。
    力度：basic（默认，只脱高置信标识）| aggressive（加姓氏扫描，会误伤，必须复核）

大模型配置（环境变量，密钥不要写进代码、也不要发给任何人）：
    LLM_BASE_URL   OpenAI 兼容地址，如 https://api.deepseek.com/v1
    LLM_API_KEY    你自己的密钥
    LLM_MODEL      模型名，如 deepseek-chat

    不设 LLM_API_KEY 时自动进入 dry-run（只组装提示词，不真发请求），
    方便先把链路跑通、把提示词调好。
"""

from __future__ import annotations

import argparse
import base64
import html as htmllib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
JOBS = HERE / "jobs"
OCR_API = os.environ.get("OCR_API", "http://127.0.0.1:8100/layout-parsing")

# ---------------------------------------------------------------------------
# 类别语义：决定进 Markdown 时怎么表示
# ---------------------------------------------------------------------------
HEADING = {"doc_title": 1, "title": 1, "paragraph_title": 2, "content_title": 2,
           "abstract": 2}
# 默认丢弃：页眉页脚页码这些对分析没价值，白占 token
NOISE = {"header", "header_image", "footer", "footer_image", "number",
         "aside_text", "reference", "reference_content"}

LABEL_CN = {
    "text": "正文", "doc_title": "主标题", "paragraph_title": "段落标题",
    "table": "表格", "formula": "公式", "display_formula": "公式",
    "image": "图片", "chart": "图表", "seal": "印章", "figure_title": "图注",
    "vision_footnote": "图片说明", "header": "页眉", "footer": "页脚",
    "number": "页码", "footnote": "脚注", "ocr": "文字",
}


# ===========================================================================
# 1. 取 OCR 结果
# ===========================================================================
def ocr_via_api(image: Path, max_pixels: int | None = None) -> dict:
    """调本地服务做识别，返回 prunedResult 列表（每页一项）。"""
    is_pdf = image.read_bytes()[:5] == b"%PDF-"
    body = {
        "file": base64.b64encode(image.read_bytes()).decode(),
        "fileType": 0 if is_pdf else 1,   # 0=PDF 1=IMAGE，服务端不猜
        "returnMarkdownImages": False,
    }
    if max_pixels:
        body["maxPixels"] = max_pixels
    r = requests.post(OCR_API, json=body, timeout=1800).json()
    if r.get("errorCode") != 0:
        raise RuntimeError(f"OCR 失败: {r.get('errorMsg')}")
    items = r["result"].get("layoutParsingResults", [])
    return [it.get("prunedResult") or {} for it in items]


def load_from_json(path: Path) -> list[dict]:
    """读 paddleocr 产出的 *_res.json（注意它没包 'res' 外壳，save_to_json 落的是内层）。"""
    d = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(d, dict) and set(d.keys()) == {"res"}:
        d = d["res"]
    if "parsing_res_list" in d:
        return [d]
    if "layoutParsingResults" in d:            # 直接喂服务端响应也行
        return [it.get("prunedResult") or {}
                for it in d["layoutParsingResults"]]
    raise ValueError("认不出这个 JSON 的结构，需要带 parsing_res_list")


# ===========================================================================
# 2. 表格处理：简单表转 Markdown，复杂表保 HTML
# ===========================================================================
def html_table_to_markdown(h: str) -> str | None:
    """把简单 HTML 表格转成 Markdown 表格。

    有 rowspan/colspan 的（合并单元格）Markdown 表达不了，返回 None 让调用方保 HTML。
    理由：强行转会把结构转错，宁可多花点 token 也要保真。
    """
    if re.search(r"rowspan|colspan", h, re.I):
        return None
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S | re.I)
    if not rows:
        return None
    grid = []
    for tr in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        grid.append([re.sub(r"<[^>]+>", "", c).strip().replace("|", "\\|")
                     for c in cells])
    if not grid or not grid[0]:
        return None
    ncol = max(len(r) for r in grid)
    grid = [r + [""] * (ncol - len(r)) for r in grid]
    out = ["| " + " | ".join(grid[0]) + " |",
           "| " + " | ".join(["---"] * ncol) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in grid[1:]]
    return "\n".join(out)


def clean_content(label: str, content: str) -> str:
    c = (content or "").strip()
    if not c:
        return ""
    if label == "table":
        md = html_table_to_markdown(c)
        return md if md else c                 # 复杂表保 HTML
    # 图片说明常带 HTML 容器，剥掉
    c = re.sub(r"</?(div|p|span)[^>]*>", "", c, flags=re.I).strip()
    return c


# ===========================================================================
# PII 脱敏
# ===========================================================================
# 设计原则：**只脱身份标识，保留临床信息**。
#   病历分析需要 年龄 / 性别 / 发病日期 / 诊断 / 用药 / 检验值 —— 这些一律不脱，
#   过度脱敏会让大模型没法分析。日期默认保留（发病时间对分析至关重要）。
#
# 三档力度：
#   off        不脱敏
#   basic      只脱高置信度的结构化标识（身份证/手机/病历号/带标签的姓名）—— 默认
#   aggressive basic + 姓名字典扫描（召回高但**会误伤**，必须人工复核报告）
#
# 占位符形如 【姓名1】【身份证1】，按首次出现编号，同一实体全文一致。
# 映射表写在 job 目录的 redaction_map.json 里，**只留本地，绝不能发给大模型**。

COMMON_SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘"
    "于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江"
    "尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)

# 结构化标识：正则本身就高置信，直接脱
_STRUCTURED: list[tuple[str, re.Pattern]] = [
    ("身份证", re.compile(r"(?<!\d)([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
                          r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)")),
    ("手机",   re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")),
    ("邮箱",   re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")),
    ("固话",   re.compile(r"(?<!\d)(0\d{2,3}[-\s]?\d{7,8})(?!\d)")),
    # 银行卡放最后：16-19 位纯数字，前面身份证已换成占位符就不会误吃
    ("银行卡", re.compile(r"(?<!\d)(\d{16,19})(?!\d)")),
]

# 带标签的字段：(类别, 正则, 是否保留标签)
#   保留标签 = 形如「姓名：张三」，只换值，保住上下文语义
#   不保留   = 形如「王先生」，整段都是标识，整个换掉
_LABELED: list[tuple[str, re.Pattern, bool]] = [
    ("病历号", re.compile(
        r"(住院号|病案号|门诊号|就诊号|医保号|卡号|病历号|档案号|床号)"
        r"\s*[：:]\s*([A-Za-z0-9\-]{3,24})"), True),
    ("姓名", re.compile(
        r"(姓名|患者姓名|病人姓名|患者|病人|联系人|监护人|家属|陪人|"
        r"签名|记录者|报告人|申请人|审核人|医师|医生|护士)"
        r"\s*[：:]\s*([\u4e00-\u9fa5]{2,4})(?=[^\u4e00-\u9fa5]|$)"), True),
    # 「王先生」「李女士」这类：姓氏 + 称谓，整段替换。
    # 注意**不能**加「前面必须是非汉字」的断言 —— 病历里「患者王先生」太常见了，
    # 前一个字符是「者」也会漏掉（自检时就是这么发现的）。
    ("姓名", re.compile(
        r"[\u4e00-\u9fa5](?:先生|女士|同志|大夫)"), False),
]

# 详细地址：要求出现「省/市/区/县」+「路/街/号/栋/室」组合，避免误伤
_ADDRESS = re.compile(
    r"([\u4e00-\u9fa5]{2,8}(?:省|市|自治区)[\u4e00-\u9fa5]{0,10}"
    r"(?:市|区|县|州|盟)[\u4e00-\u9fa5]{0,20}"
    r"(?:路|街|道|巷|村|镇|乡)[\u4e00-\u9fa5\d]{0,15}"
    r"(?:\d+号|[\d]+栋|[\d]+室|[\d]+单元)[\u4e00-\u9fa5\d]{0,15})"
)

# aggressive 模式的止损表：这些词以姓氏开头但显然不是人名
_NOT_NAMES = {
    "主治", "主任", "住院", "门诊", "病房", "病史", "患者", "病人", "白细", "王不",
    "黄金", "陆地", "陈述", "任何", "条件", "程序", "方案", "方法", "方向", "程度",
}


class Redactor:
    """确定性规则脱敏。记录映射，可逆。"""

    def __init__(self, mode: str = "basic"):
        self.mode = mode
        self.map: dict[str, str] = {}        # 占位符 -> 原文
        self.reverse: dict[str, str] = {}    # 原文 -> 占位符
        self.report: list[dict] = []
        self._counters: dict[str, int] = {}

    # -------------------------------------------------------------- 内部
    def _placeholder(self, kind: str, original: str) -> str:
        if original in self.reverse:
            return self.reverse[original]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        ph = f"【{kind}{self._counters[kind]}】"
        self.map[ph] = original
        self.reverse[original] = ph
        return ph

    def _record(self, kind: str, original: str, context: str) -> None:
        self.report.append({"kind": kind, "original": original,
                            "placeholder": self.reverse[original],
                            "context": context[:70]})

    def _sub_structured(self, text: str) -> str:
        for kind, pat in _STRUCTURED:
            def repl(m):
                orig = m.group(1)
                ph = self._placeholder(kind, orig)
                self._record(kind, orig, text[max(0, m.start() - 20):m.end() + 20])
                return m.group(0).replace(orig, ph)
            text = pat.sub(repl, text)
        return text

    def _sub_labeled(self, text: str) -> str:
        for kind, pat, keep_label in _LABELED:
            def repl(m):
                if keep_label:
                    label, value = m.group(1), m.group(2)
                    ph = self._placeholder(kind, value)
                    self._record(kind, value, text[max(0, m.start() - 20):m.end() + 20])
                    # 标签原样保留，连分隔符也照抄，只换值
                    sep = m.group(0)[len(label):len(label) + 1]
                    sep = sep if sep in "：:" else "："
                    return f"{label}{sep}{ph}"
                # 整段都是标识（如「王先生」），整个替换
                whole = m.group(0)
                ph = self._placeholder(kind, whole)
                self._record(kind, whole, text[max(0, m.start() - 20):m.end() + 20])
                return ph
            text = pat.sub(repl, text)
        return text

    def _sub_address(self, text: str) -> str:
        def repl(m):
            orig = m.group(1)
            ph = self._placeholder("住址", orig)
            self._record("住址", orig, text[max(0, m.start() - 20):m.end() + 20])
            return ph
        return _ADDRESS.sub(repl, text)

    def _sub_aggressive(self, text: str) -> str:
        """姓氏字典扫描。召回高，但**一定会误伤**，必须看报告复核。"""
        def repl(m):
            word = m.group(0)
            if word in _NOT_NAMES or word[0] not in COMMON_SURNAMES:
                return word
            ph = self._placeholder("姓名", word)
            self._record("姓名", word, text[max(0, m.start() - 25):m.end() + 25])
            return ph
        # 前后都是非汉字边界，长度 2-3，首字是常见姓
        return re.sub(r"(?<![\u4e00-\u9fa5])[\u4e00-\u9fa5]{2,3}(?![\u4e00-\u9fa5])",
                      repl, text)

    # -------------------------------------------------------------- 对外
    def __call__(self, text: str) -> str:
        if self.mode == "off" or not text:
            return text
        text = self._sub_structured(text)
        text = self._sub_labeled(text)
        text = self._sub_address(text)
        if self.mode == "aggressive":
            text = self._sub_aggressive(text)
        return text

    def restore(self, text: str) -> str:
        for ph, orig in self.map.items():
            text = text.replace(ph, orig)
        return text

    @property
    def hits(self) -> int:
        return len(self.report)


def redaction_report_md(red: Redactor) -> str:
    by_kind: dict[str, list[dict]] = {}
    for r in red.report:
        by_kind.setdefault(r["kind"], []).append(r)

    out = ["# 脱敏报告", "",
           f"- 力度：`{red.mode}`",
           f"- 替换总数：**{red.hits}** 处",
           f"- 实体种类：{len(by_kind)}",
           "",
           "> 映射表在同目录 `redaction_map.json`，**只留本地，不要发给大模型**。",
           "> 分析结果里的占位符可用 `stack.py restore` 还原。", ""]
    for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        out.append(f"## {kind}（{len(items)}）")
        out.append("")
        out.append("| 占位符 | 原文 | 上下文 |")
        out.append("| --- | --- | --- |")
        for it in items:
            ctx = it["context"].replace("|", "\\|").replace("\n", " ")
            out.append(f"| `{it['placeholder']}` | {it['original']} | {ctx} |")
        out.append("")
    if red.mode == "aggressive":
        out += ["---", "",
                "**aggressive 模式一定会误伤**：上表里凡是「原文」看着不像人名的，",
                "都是被姓氏字典扫中的普通词。请逐条核对，把不该脱的加进",
                "`stack.py` 的 `_NOT_NAMES` 里，或者改用 `basic` 模式。", ""]
    return "\n".join(out)


# ===========================================================================
# 3. 块 -> 给大模型看的 Markdown
# ===========================================================================
def blocks_to_markdown(pages: list[dict], drop_noise: bool = True,
                       redactor: "Redactor | None" = None) -> tuple[str, dict]:
    """返回 (markdown, 统计)。保留块序号注释以便溯源。"""
    lines, stats = [], {"kept": 0, "dropped": 0, "by_label": {}}
    if len(pages) > 1:
        lines.append(f"<!-- 共 {len(pages)} 页 -->\n")

    for pi, page in enumerate(pages):
        blocks = page.get("parsing_res_list") or []
        # 按阅读顺序排；order 为 None 的（页眉页脚图表等）排在最后
        blocks = sorted(enumerate(blocks),
                        key=lambda t: (t[1].get("block_order") is None,
                                       t[1].get("block_order") or 0))
        if len(pages) > 1:
            lines.append(f"\n## — 第 {pi + 1} 页 —\n")

        for idx, b in blocks:
            label = b.get("block_label") or ""
            content = clean_content(label, b.get("block_content") or "")
            if redactor is not None:
                content = redactor(content)      # 在进 Markdown 之前脱敏
            stats["by_label"][label] = stats["by_label"].get(label, 0) + 1

            if drop_noise and label in NOISE:
                stats["dropped"] += 1
                continue
            if not content and label not in ("image", "chart"):
                stats["dropped"] += 1
                continue

            stats["kept"] += 1
            # 溯源锚点：块序号，大模型要引用时用得上，对 token 几乎无成本
            anchor = f"<!--b{pi}-{idx}-->"

            if label in HEADING:
                lines.append(f"\n{'#' * HEADING[label]} {content}\n{anchor}")
            elif label in ("image", "chart"):
                lines.append(f"\n![{'图片' if label == 'image' else '图表'}]\n{anchor}")
            elif label in ("formula", "display_formula"):
                f = content if content.startswith("$$") else f"$$\n{content}\n$$"
                lines.append(f"\n{f}\n{anchor}")
            elif label == "seal":
                lines.append(f"\n（印章：{content}）\n{anchor}")
            elif label in ("figure_title", "vision_footnote"):
                lines.append(f"\n> {content}\n{anchor}")
            elif label == "footnote":
                lines.append(f"\n[脚注] {content}\n{anchor}")
            else:
                lines.append(f"\n{content}\n{anchor}")

    md = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return md, stats


def build_job(pages: list[dict], source: str, name: str | None,
              drop_noise: bool = True, redact: str = "basic") -> Path:
    job_name = name or datetime.now().strftime("%Y%m%d-%H%M%S")
    job = JOBS / job_name
    job.mkdir(parents=True, exist_ok=True)

    redactor = Redactor(redact) if redact != "off" else None
    md, stats = blocks_to_markdown(pages, drop_noise, redactor)
    (job / "document.md").write_text(md, encoding="utf-8")

    # 映射表与报告：**只留本地**。document.md 是发出去的那份，这两个不是。
    if redactor is not None and redactor.hits:
        (job / "redaction_map.json").write_text(
            json.dumps({"mode": redact, "map": redactor.map},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        (job / "脱敏报告.md").write_text(
            redaction_report_md(redactor), encoding="utf-8")

    # 原始块完整保留：写坐标、写 bbox，用于溯源和二次开发，但不进大模型
    (job / "blocks.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")

    # 表格单独导出，方便人工核对 / 进 Excel
    tdir = job / "tables"
    n_tab = 0
    for pi, page in enumerate(pages):
        for i, b in enumerate(page.get("parsing_res_list") or []):
            if b.get("block_label") == "table" and (b.get("block_content") or "").strip():
                tdir.mkdir(exist_ok=True)
                (tdir / f"p{pi + 1}_t{i}.html").write_text(
                    b["block_content"], encoding="utf-8")
                n_tab += 1

    raw_bytes = sum(len(json.dumps(p, ensure_ascii=False)) for p in pages)
    meta = {
        "source": source,
        "created": datetime.now().isoformat(timespec="seconds"),
        "pages": len(pages),
        "blocks_total": sum(len(p.get("parsing_res_list") or []) for p in pages),
        "blocks_kept": stats["kept"],
        "blocks_dropped": stats["dropped"],
        "tables_exported": n_tab,
        "label_counts": stats["by_label"],
        "raw_json_chars": raw_bytes,
        "markdown_chars": len(md),
        "compression": round(1 - len(md) / max(1, raw_bytes), 3),
        "redaction": {
            "mode": redact,
            "hits": redactor.hits if redactor else 0,
            "map_file": "redaction_map.json" if (redactor and redactor.hits) else None,
            "report_file": "脱敏报告.md" if (redactor and redactor.hits) else None,
            "warning": ("document.md 已脱敏，可安全发给大模型；"
                        "redaction_map.json 含原文，只留本地"
                        if (redactor and redactor.hits) else None),
        },
    }
    (job / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


# ===========================================================================
# 4. 大模型分析
# ===========================================================================
PROMPTS = {
    "摘要": (
        "你是文档分析助手。下面是一份文档经 OCR 还原后的 Markdown。\n"
        "请输出：\n"
        "1. 一句话摘要\n"
        "2. 关键要点（3-6 条）\n"
        "3. 涉及的实体（人名/机构/日期/金额/编号等），没有就写「无」\n\n"
        "要求：只依据原文，不要推测。原文没有的信息写「文中未提及」。\n"
        "引用原文时用「」括起来。"
    ),
    "字段核对": (
        "你是数据核对助手。下面是一份文档经 OCR 还原后的 Markdown。\n"
        "请把文档中的关键字段抽成 JSON，并标出可疑项（可能的 OCR 错字、"
        "格式异常、前后矛盾）。\n\n"
        "输出格式：\n"
        '```json\n{"fields": {"字段名": "值"}, '
        '"suspicious": [{"field": "字段名", "reason": "为什么可疑"}]}\n```\n'
        "无法确认的字段值写 null，不要编造。"
    ),
    "病历质控": (
        "你是病历质控助手。下面是一份病历经 OCR 还原后的文本。\n"
        "请检查并输出：\n"
        "1. 必填项缺失情况（主诉/现病史/既往史/诊断/处理等）\n"
        "2. 前后矛盾之处（如性别与描述不符、日期先后颠倒、剂量与用法冲突）\n"
        "3. 疑似 OCR 识别错误（结合医学常识判断不合理的字词）\n"
        "4. 需要人工复核的具体位置\n\n"
        "严格区分「原文确实没写」和「OCR 没识别出来」——前者说缺失，"
        "后者说存疑。不要脑补原文没有的内容。"
    ),
}


def call_llm(system: str, user: str, base_url: str, api_key: str,
             model: str, temperature: float = 0.2) -> str:
    r = requests.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"model": model, "temperature": temperature,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=600,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ===========================================================================
# 5. 溯源：分析结论回指原文位置
# ===========================================================================
def locate_quote(quote: str, pages: list[dict], threshold: float = 0.6) -> list[dict]:
    """把模型引用的一段原文，在 blocks.json 里定位回块 + 坐标。

    这是结构化 OCR 相对纯文本 OCR 的核心优势：结论可追溯、可回看原图。
    """
    import difflib

    q = re.sub(r"\s+", "", quote)
    if len(q) < 4:
        return []
    hits = []
    for pi, page in enumerate(pages):
        for i, b in enumerate(page.get("parsing_res_list") or []):
            c = re.sub(r"\s+", "", b.get("block_content") or "")
            if not c:
                continue
            if q in c:
                score = 1.0
            else:
                score = difflib.SequenceMatcher(None, q, c).ratio()
            if score >= threshold:
                hits.append({"page": pi, "block_id": i,
                             "label": b.get("block_label"),
                             "bbox": b.get("block_bbox"),
                             "score": round(score, 3),
                             "text": (b.get("block_content") or "")[:80]})
    return sorted(hits, key=lambda h: -h["score"])[:5]


# ===========================================================================
# CLI
# ===========================================================================
def cmd_build(a) -> int:
    if a.from_json:
        pages = load_from_json(Path(a.from_json))
        source = Path(a.from_json).name
    else:
        img = Path(a.image)
        if not img.exists():
            print(f"找不到文件: {img}", file=sys.stderr)
            return 1
        print(f"调用 OCR 服务识别 {img.name} …（整页可能几分钟）")
        t = time.time()
        pages = ocr_via_api(img, a.max_pixels)
        print(f"识别完成，用时 {time.time() - t:.1f}s")
        source = img.name

    job = build_job(pages, source, a.name, drop_noise=not a.keep_noise,
                    redact=a.redact)
    meta = json.loads((job / "meta.json").read_text(encoding="utf-8"))
    print(f"\njob 目录 -> {job}")
    print(f"  页数 {meta['pages']} · 块 {meta['blocks_total']} "
          f"(保留 {meta['blocks_kept']} / 丢弃 {meta['blocks_dropped']})")
    print(f"  原始 JSON {meta['raw_json_chars']:,} 字符 "
          f"-> document.md {meta['markdown_chars']:,} 字符 "
          f"(压缩 {meta['compression'] * 100:.0f}%)")
    if meta["tables_exported"]:
        print(f"  导出表格 {meta['tables_exported']} 个 -> tables/")
    red = meta.get("redaction") or {}
    if red.get("hits"):
        print(f"  脱敏 {red['hits']} 处（{red['mode']}）-> {red['report_file']}")
        print(f"      映射表 {red['map_file']}（含原文，只留本地）")
    elif red.get("mode") and red["mode"] != "off":
        print(f"  脱敏：未命中（{red['mode']}）")
    return 0


def cmd_show(a) -> int:
    job = Path(a.job)
    md = (job / "document.md").read_text(encoding="utf-8")
    if a.raw:
        print(md)
    else:
        # 默认去掉溯源注释，看"大模型实际读到的样子"
        print(re.sub(r"<!--b\d+-\d+-->\n?", "", md))
    return 0


def cmd_analyze(a) -> int:
    job = Path(a.job)
    md_path = job / "document.md"
    if not md_path.exists():
        print(f"{job} 里没有 document.md", file=sys.stderr)
        return 1
    doc = md_path.read_text(encoding="utf-8")

    system = (Path(a.prompt).read_text(encoding="utf-8")
              if a.prompt else PROMPTS.get(a.task, PROMPTS["摘要"]))
    user = f"文档内容如下：\n\n{doc}"

    prompt_dump = job / "prompt.txt"
    prompt_dump.write_text(f"===== SYSTEM =====\n{system}\n\n"
                           f"===== USER =====\n{user}", encoding="utf-8")

    base_url = a.base_url or os.environ.get("LLM_BASE_URL", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = a.model or os.environ.get("LLM_MODEL", "")

    if not (base_url and api_key and model):
        print("未配置大模型 —— 进入 dry-run（只组装提示词，不发请求）")
        print(f"  提示词已写入 {prompt_dump}")
        print(f"  文档 {len(doc):,} 字符 ≈ {int(len(doc) / 1.5):,} tokens（中文粗估）")
        print()
        print("要真正调用，先设三个环境变量（密钥只在你本机，不要发给任何人）：")
        print('  $env:LLM_BASE_URL="https://api.deepseek.com/v1"')
        print('  $env:LLM_API_KEY ="你的密钥"')
        print('  $env:LLM_MODEL   ="deepseek-chat"')
        return 0

    print(f"调用 {model} @ {base_url} …（文档 {len(doc):,} 字符）")
    t = time.time()
    try:
        out = call_llm(system, user, base_url, api_key, model, a.temperature)
    except requests.HTTPError as e:
        print(f"调用失败: HTTP {e.response.status_code} "
              f"{e.response.text[:300]}", file=sys.stderr)
        return 1
    except requests.RequestException as e:
        print(f"调用失败: {e}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = job / f"analysis_{a.task}_{stamp}.md"
    out_path.write_text(f"# 分析结果（{a.task}）\n\n"
                        f"- 来源文档：{job.name}/document.md\n"
                        f"- 模型：{model}\n"
                        f"- 用时：{time.time() - t:.1f}s\n\n---\n\n{out}",
                        encoding="utf-8")
    print(f"\n分析完成，用时 {time.time() - t:.1f}s -> {out_path}")
    print("\n" + "─" * 60)
    print(out[:1500])
    return 0


def cmd_run(a) -> int:
    a.from_json = None
    if cmd_build(a) != 0:
        return 1
    job = JOBS / (a.name or sorted(JOBS.iterdir(), key=lambda p: p.stat().st_mtime)[-1].name)
    a.job = str(job)
    return cmd_analyze(a)


def _load_map(job: Path) -> dict[str, str]:
    mf = job / "redaction_map.json"
    if not mf.exists():
        return {}
    return json.loads(mf.read_text(encoding="utf-8")).get("map", {})


def _redacted_view(pages: list[dict], mapping: dict[str, str]) -> list[dict]:
    """把 blocks.json 按映射表还原成「脱敏后的样子」。

    大模型引用的原文里是占位符（【姓名1】），直接拿它去搜原始 blocks.json 搜不到，
    所以要先造一份同样脱敏的副本再匹配。
    """
    pairs = sorted(((orig, ph) for ph, orig in mapping.items()),
                   key=lambda t: -len(t[0]))

    def sub(s: str) -> str:
        for orig, ph in pairs:
            s = s.replace(orig, ph)
        return s

    out = []
    for p in pages:
        p2 = dict(p)
        p2["parsing_res_list"] = [
            {**b, "block_content": sub(b.get("block_content") or "")}
            for b in (p.get("parsing_res_list") or [])]
        out.append(p2)
    return out


def cmd_locate(a) -> int:
    """把一段原文定位回块和坐标。"""
    job = Path(a.job)
    pages = json.loads((job / "blocks.json").read_text(encoding="utf-8"))
    mapping = _load_map(job)

    hits = locate_quote(a.quote, pages)
    if not hits and mapping:
        # 引文里可能带脱敏占位符，换脱敏视图再试一次
        hits = locate_quote(a.quote, _redacted_view(pages, mapping))
        if hits:
            print("（引文含脱敏占位符，已在脱敏视图里定位）")
    if not hits:
        print("没有匹配到块")
        return 1
    for h in hits:
        print(f"  第 {h['page'] + 1} 页 · 块 {h['block_id']} · {h['label']} "
              f"· 相似度 {h['score']}\n    bbox={h['bbox']}\n    {h['text']}")
    return 0


def cmd_restore(a) -> int:
    """把大模型输出里的占位符还原成原文。"""
    job = Path(a.job)
    mapping = _load_map(job)
    if not mapping:
        print(f"{job} 里没有 redaction_map.json，无需还原", file=sys.stderr)
        return 1

    if a.file:
        src = Path(a.file)
        text = src.read_text(encoding="utf-8")
        out_path = src.with_name(src.stem + "_还原" + src.suffix)
    else:
        text = sys.stdin.read()
        out_path = job / f"restored_{datetime.now():%Y%m%d-%H%M%S}.md"

    n = 0
    for ph, orig in mapping.items():
        c = text.count(ph)
        if c:
            text = text.replace(ph, orig)
            n += c
    out_path.write_text(text, encoding="utf-8")
    print(f"还原 {n} 处占位符 -> {out_path}")

    if a.print:
        print("\n" + "─" * 60)
        print(text[:2000])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OCR → 中间文件 → 大模型分析")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_build_args(p):
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument("--from-json", help="已有的 *_res.json")
        src.add_argument("--image", help="图片/PDF，顺便调 OCR 服务")
        p.add_argument("--name", help="job 名，默认时间戳")
        p.add_argument("--max-pixels", type=int)
        p.add_argument("--keep-noise", action="store_true",
                       help="保留页眉页脚页码（默认丢弃）")
        p.add_argument("--redact", default="basic",
                       choices=["off", "basic", "aggressive"],
                       help="脱敏力度：basic 只脱高置信标识（默认）；"
                            "aggressive 加姓氏扫描，召回高但会误伤，务必看报告")

    def add_llm_args(p):
        p.add_argument("--task", default="摘要", choices=list(PROMPTS),
                       help="内置任务模板")
        p.add_argument("--prompt", help="自定义提示词文件（覆盖 --task）")
        p.add_argument("--base-url")
        p.add_argument("--model")
        p.add_argument("--temperature", type=float, default=0.2)

    p = sub.add_parser("build", help="OCR 结果 -> job 目录")
    add_build_args(p); p.set_defaults(func=cmd_build)

    p = sub.add_parser("show", help="打印中间文件")
    p.add_argument("job"); p.add_argument("--raw", action="store_true",
                                          help="带溯源注释一起打印")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("analyze", help="job 目录 -> 大模型分析")
    p.add_argument("job"); add_llm_args(p); p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("run", help="build + analyze 一步到位")
    add_build_args(p); add_llm_args(p); p.set_defaults(func=cmd_run)

    p = sub.add_parser("locate", help="把一段原文定位回块和坐标")
    p.add_argument("job"); p.add_argument("quote"); p.set_defaults(func=cmd_locate)

    p = sub.add_parser("restore", help="把大模型输出里的占位符还原成原文")
    p.add_argument("job")
    p.add_argument("file", nargs="?",
                   help="要还原的文件；省略则从标准输入读")
    p.add_argument("--print", action="store_true", help="顺便打印还原后的内容")
    p.set_defaults(func=cmd_restore)

    a = ap.parse_args()
    JOBS.mkdir(exist_ok=True)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
