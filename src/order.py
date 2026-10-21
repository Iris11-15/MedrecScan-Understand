# -*- coding: utf-8 -*-
"""权威阅读顺序（F2.1）—— 全链路唯一权威值。

问题回顾：阅读顺序原先在四处各排各的 —— ``layered.py`` 不排且输出没有 ``block_order``；
``webui.py`` 按 (y,x) 排但 ``parsing_res_list`` 未排序（双栏被交错打散）；
``stack.py build`` 按 ``block_order`` 排，而字段缺失时排序是空操作。
本模块产出唯一权威顺序，下游一律**只读不排**。

算法（只用 bbox + 标签，零模型）：

  ① 明显跨栏块
     宽度 > ``span_max_frac`` × 页宽，或图/表类标签 > ``span_override`` × 页宽，
     或宽度 > ``span_median_mult`` × 块宽中位数 —— 且**跨过页中线**（防"并排两栏
     各占 45% 页宽"被误判）。这些块不参与聚栏。

  ② 聚栏（只用非跨栏块，按几何序）
     两个 x 区间算"同栏"需同时满足 ``_compatible`` 三条：左边界差 <= ``col_tol_frac``
     × 页宽、**两边都对齐**（双向包含）、宽度比例 >= ``width_ratio``。
     * 双向包含是必需的：只要求单向包含会留下歧义 —— 居中署名既包含正文又包含图注，
       "先处理谁"决定归属，同一份数据两次排序结果会不同（实测踩过）；
     * 宽度比例是必需的：跨栏大标题"完全包含"左右两栏，不加它会两栏粘一栏。

  ③ 横跨跨栏块
     x 范围与 >= 2 个**文字栏**相交，且宽度 >= ``min_span_width_frac`` × 页宽。

  ④ 排序
     栏列表**只由文字块决定**（跨栏块不参与扩栏 —— 否则图/图注会把两栏粘起来、
     甚至造出互相重叠的栏区间，实测踩过）。普通块按中心 x 落栏（落在栏间隙取最近栏）；
     跨栏块按 **x1** 落栏；栏内按 ``(y1, x1, 标签, block_id)`` 排序 —— **一律不含输入
     index**（输入数组顺序本身就是错的，键里带上它会让错误顺序重新渗透进结果）。

夹具实测（``jobs/demo``，1 页 31 块，页宽 1524）：

    cols  = [8,361] [388,743] [770,1124] [1151,1507]   ← 4 个文字栏，单调不重叠
    spans = [0, 2, 3]                                   ← 大标题 / 整幅图 / 图注
    order = 0,4..13, 1,14..22, 2,3, 23..30              ← 与人工阅读顺序一致

且输入被 (y,x) 交错、逆序、或完全随机打乱 50 次，结果**完全一致**。

对外主接口：``order_blocks()`` 返回排好的 Block 列表（取 ``b.block_id`` / ``b.raw``），
``apply_order()`` 就地写回 ``block_order``。**不要**用 ``order_page()`` 的 index 去索引
调用方自己的输入列表 —— 内部先做了几何规范化排序，index 只在模块内部有意义。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Sequence

# ---- 可调参数（诊断报告会回显，便于按页调参）----------------------------
COL_TOL_FRAC = 0.18          # 同栏：左边界差 <= 18% 页宽（容忍栏内左边界不齐）
CONTAIN_TOL_PX = 2.0         # 完全包含判定的容差
WIDTH_RATIO = 0.70           # 同栏：较窄宽/较宽宽 >= 70%（防跨栏大块桥接两栏）
CLUSTER_GAP_PX = 6.0         # 簇合并：两簇 x 间隙 <= 6px（防跨栏图片桥接）
SPAN_MAX_FRAC = 0.40         # 宽度 > 40% 页宽 -> 跨栏块
SPAN_OVERRIDE = 0.55         # 图/表类标签宽度 > 55% 页宽 -> 跨栏块
SPAN_MEDIAN_MULT = 2.5       # 宽度 > 2.5 × 块宽中位数 -> 跨栏块
MIN_SPAN_WIDTH_FRAC = 0.25   # 宽度 < 25% 页宽的块不可能跨栏
SPAN_CENTER_FRAC = 0.5       # 绝对/相对宽度规则要求跨过页中线才算跨栏
SPAN_LABELS = {"image", "table", "chart", "seal",
               "header_image", "footer_image", "figure"}


@dataclass
class Block:
    """排序用视图，不改动原始字典。"""
    raw: dict
    index: int
    block_id: Any = None
    label: str = ""
    bbox: tuple = field(default_factory=tuple)

    @classmethod
    def of(cls, d: dict, index: int, label_key: str = "block_label") -> "Block":
        bb = d.get("block_bbox") or d.get("bbox") or []
        bbox = tuple(float(v) for v in bb) if len(bb) >= 4 else (0.0, 0.0, 0.0, 0.0)
        return cls(raw=d, index=index,
                   block_id=d.get("block_id", index),
                   label=(d.get(label_key) or "").strip(),
                   bbox=bbox)

    @property
    def x1(self) -> float: return self.bbox[0]
    @property
    def y1(self) -> float: return self.bbox[1]
    @property
    def x2(self) -> float: return self.bbox[2]
    @property
    def y2(self) -> float: return self.bbox[3]
    @property
    def width(self) -> float: return max(0.0, self.x2 - self.x1)
    @property
    def height(self) -> float: return max(0.0, self.y2 - self.y1)
    @property
    def cx(self) -> float: return (self.x1 + self.x2) / 2
    @property
    def valid(self) -> bool: return self.width > 0 and self.height > 0


# ------------------------------------------------------------------ 同栏判定
def _compatible(a: tuple[float, float], b: tuple[float, float], tol: float,
                width_ratio: float = WIDTH_RATIO,
                contain_tol: float = CONTAIN_TOL_PX) -> bool:
    """两个 x 区间是否算"同一栏"：左边界接近 + 一方完全包含 + 宽度量级相当。"""
    if abs(a[0] - b[0]) > tol:
        return False
    wa, wb = a[1] - a[0], b[1] - b[0]
    if wa <= 0 or wb <= 0:
        return False
    # **双向包含**：要求两条边都对齐（容差 contain_tol）。
    # 只要求"单向包含"会留下歧义 —— 居中署名 x[582,930] 既包含正文 x[582,743]
    # （向右多出去一截），又包含图注 x[809,930]（向左多出去一截），于是"先处理谁"
    # 决定归属，导致同一份数据两次排序结果不同（实测：compile.py 的输出与
    # order.py 单测不一致）。双向包含把这种歧义消掉。
    if abs(a[0] - b[0]) > contain_tol or abs(a[1] - b[1]) > contain_tol:
        return False
    return min(wa, wb) / max(wa, wb) >= width_ratio


def is_obvious_span(b: Block, page_width: float, narrow_median_w: float = 0.0,
                    span_max_frac: float = SPAN_MAX_FRAC,
                    span_override: float = SPAN_OVERRIDE,
                    span_median_mult: float = SPAN_MEDIAN_MULT,
                    span_labels: set = SPAN_LABELS,
                    center_frac: float = SPAN_CENTER_FRAC) -> bool:
    """光看宽度就能定的跨栏块：绝对超宽 / 图表类超宽 / 相对中位数超宽。

    宽阈值规则额外要求**跨越页面中线**：真正跨栏的块必然横跨页中线，
    而"占页宽 40%+ 的并排栏"并不会（实测：两栏各 690px = 45% 页宽，
    若不要求跨中线会被误判为跨栏块，两栏被当成两个跨栏块 → 只切出 1 栏）。
    """
    if not b.valid:
        return False
    crosses = True
    if page_width:
        mid = page_width / 2
        crosses = b.x1 < mid - 1e-6 and b.x2 > mid + 1e-6
    if crosses and page_width and b.width > span_max_frac * page_width:
        return True
    if b.label in span_labels and page_width and b.width > span_override * page_width:
        return True
    return bool(crosses and narrow_median_w and b.width > span_median_mult * narrow_median_w)


def is_cross_span(b: Block, cols: Sequence[dict], page_width: float,
                  min_span_width_frac: float = MIN_SPAN_WIDTH_FRAC) -> bool:
    """x 范围与 >= 2 个栏相交（且宽度够大）-> 跨栏块。"""
    if not b.valid or not cols:
        return False
    if page_width and b.width < min_span_width_frac * page_width:
        return False
    hit = sum(1 for c in cols if b.x1 < c["x2"] - 1e-6 and b.x2 > c["x1"] + 1e-6)
    return hit >= 2


# ------------------------------------------------------------------ 聚栏
def _seed_clusters(blocks: Sequence[Block], page_width: float,
                   tol_frac: float = COL_TOL_FRAC) -> list[dict]:
    """种子簇：几何序扫描，块与某簇**全体成员**都兼容才并入（防贪婪桥接）。"""
    bs = [b for b in blocks if b.valid]
    if not bs:
        return []
    tol = max(1.0, tol_frac * float(page_width or 0))
    bs = sorted(bs, key=lambda b: (b.x1, b.y1, b.x2, b.y2, b.label, str(b.block_id)))

    seeds: list[dict] = []
    for b in bs:
        for c in seeds:
            if all(_compatible((b.x1, b.x2), m, tol) for m in c["ranges"]):
                c["ranges"].append((b.x1, b.x2))
                c["x1"] = min(c["x1"], b.x1)
                c["x2"] = max(c["x2"], b.x2)
                c["n"] += 1
                c["ids"].append(b.block_id)
                break
        else:
            seeds.append({"x1": b.x1, "x2": b.x2, "n": 1, "ids": [b.block_id],
                          "ranges": [(b.x1, b.x2)]})
    seeds.sort(key=lambda c: (c["x1"], c["x2"]))
    return seeds


def build_columns(blocks: Sequence[Block], page_width: float,
                  tol_frac: float = COL_TOL_FRAC,
                  cluster_gap_px: float = CLUSTER_GAP_PX) -> tuple[list[dict], list[dict]]:
    """返回 (锚柱, 待归并的单块簇)。

    分两类：
      * **锚柱**：合并后有 >= 2 个块的簇 —— 是可靠的栏；
      * **单块簇**：只有 1 个块的簇（居中署名、跨栏图注、稀疏栏）。
    只有单块簇时，把它们**全部当作锚柱**返回（否则两栏各 1 块会被并成一栏 ——
    实测 "跨栏标题 + 两栏" 合成用例就是这样退化成单栏的）。
    """
    seeds = _seed_clusters(blocks, page_width, tol_frac)
    if not seeds:
        return [], []
    tol = max(1.0, tol_frac * float(page_width or 0))
    n = len(seeds)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        for j in range(i + 1, n):
            a, b = seeds[i], seeds[j]
            if not _compatible((a["x1"], a["x2"]), (b["x1"], b["x2"]), tol):
                continue
            gap = max(a["x1"], b["x1"]) - min(a["x2"], b["x2"])
            if gap <= cluster_gap_px:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    merged = []
    for idxs in groups.values():
        merged.append({"x1": min(seeds[i]["x1"] for i in idxs),
                       "x2": max(seeds[i]["x2"] for i in idxs),
                       "n": sum(seeds[i]["n"] for i in idxs),
                       "ids": [x for i in idxs for x in seeds[i]["ids"]]})
    merged.sort(key=lambda c: (c["x1"], c["x2"]))
    anchors = [c for c in merged if c["n"] >= 2]
    singles = [c for c in merged if c["n"] < 2]
    if not anchors:                       # 全是单块簇 -> 每个单块簇本身就是一栏
        return singles, []
    return anchors, singles


def _col_by_center(b: Block, cols: Sequence[dict]) -> int:
    """普通块落栏：中心 x 落在哪一栏；落在栏间隙时取最近栏。

    夹具实测：居中署名 x[582,930]（中心 756）正好落在栏 B[388,743] 与
    栏 C[770,1124] 之间的 27px 间隙里 —— 必须有兜底，否则该块无处可归。
    """
    for i, c in enumerate(cols):
        if c["x1"] - 1e-6 <= b.cx <= c["x2"] + 1e-6:
            return i
    return _nearest(b.cx, cols)


def _nearest(cx: float, cols: Sequence[dict]) -> int:
    return min(range(len(cols)),
               key=lambda i: abs(cx - (cols[i]["x1"] + cols[i]["x2"]) / 2))


# ------------------------------------------------------------------ 排序
def order_blocks(blocks: Sequence[Block], page_width: float | None = None,
                 tol_frac: float = COL_TOL_FRAC,
                 span_max_frac: float = SPAN_MAX_FRAC) -> tuple[list[Block], dict]:
    """主接口：返回 (按阅读顺序排好的 Block 列表, 诊断信息)。

    调用方用 ``b.block_id`` / ``b.raw`` 取结果，不要用 index 索引自己的输入。
    """
    blocks = list(blocks)
    if not blocks:
        return [], {"cols": [], "spans": [], "warnings": ["空页"]}
    if page_width is None:
        page_width = max((b.x2 for b in blocks), default=0.0)
    page_width = float(page_width or max((b.x2 for b in blocks), default=1.0) or 1.0)

    # 规范化：纯几何 + 文本键，**不含 index**（否则输入顺序会渗透进结果）
    blocks = sorted(blocks, key=lambda b: (b.x1, b.y1, b.x2, b.y2, b.label, str(b.block_id)))

    widths = sorted(b.width for b in blocks if b.valid)
    median_w = statistics.median(widths) if widths else 0.0

    # ① 明显跨栏块（不参与聚栏）
    obvious = [b for b in blocks if is_obvious_span(b, page_width, median_w, span_max_frac)]
    plain = [b for b in blocks if not is_obvious_span(b, page_width, median_w, span_max_frac)]

    # ② 聚栏
    solids, weak = build_columns(plain, page_width, tol_frac)
    if not solids:
        solids = [{"x1": min((b.x1 for b in plain), default=0.0),
                   "x2": max((b.x2 for b in plain), default=page_width),
                   "n": len(plain), "ids": [b.block_id for b in plain]}]
        weak = []

    # ③ 跨栏判定
    spans = list(obvious)
    normal = []
    for b in plain:
        (spans if is_cross_span(b, solids, page_width) else normal).append(b)

    # ④ 落栏
    # cols 只由**文字块**决定（solids），跨栏块不参与扩栏。原因：把图/图注的
    # x 范围并进最近的栏，会把两个文字栏粘成一栏、甚至造出互相重叠的栏区间
    # （实测：图 x[777,1502] 与图注 x[809,1486] 分别被并进第三栏与第四栏，
    # 报告里出现 [770,1124] 与 [777,1507] 重叠）。跨栏块改由 x1 落入某个文字栏。
    cols = [dict(c) for c in solids]

    span_idx = {b.index for b in spans}
    col_of: dict[int, int] = {}
    for b in blocks:
        if b.index in span_idx:
            for i, c in enumerate(cols):
                if c["x1"] - 1e-6 <= b.x1 <= c["x2"] + 1e-6:
                    col_of[b.index] = i
                    break
            else:
                col_of[b.index] = _nearest(b.x1, cols)
        else:
            col_of[b.index] = _col_by_center(b, cols)

    def sort_key(b: Block) -> tuple:
        ci = col_of[b.index]
        y = b.y1
        if b.index in span_idx:
            follow = [x for x in normal
                      if col_of[x.index] == ci and x.y1 >= b.y2 - 1e-6]
            y = (min(x.y1 for x in follow) - 1.0) if follow else (b.y1 - 1.0)
        return (ci, y, b.x1, b.label, str(b.block_id))

    ordered = sorted(blocks, key=sort_key)

    warnings: list[str] = []
    if len(cols) > 1:
        warnings.append(f"检测到 {len(cols)} 栏")
    if spans:
        warnings.append(f"跨栏块 {len(spans)} 个: {[b.block_id for b in spans][:8]}")
    naive = [b.index for b in sorted(blocks, key=lambda b: (b.y1, b.x1, b.label))]
    if [b.index for b in ordered] != naive:
        warnings.append("阅读顺序与朴素 (y,x) 排序不一致，已按栏序重排")
    no_bbox = [b.block_id for b in blocks if not b.valid]
    if no_bbox:
        warnings.append(f"{len(no_bbox)} 个块缺 bbox: {no_bbox[:8]}")

    return ordered, {"cols": cols, "spans": [b.block_id for b in spans],
                     "warnings": warnings}


def order_page(blocks: Sequence[Block], page_width: float | None = None,
               tol_frac: float = COL_TOL_FRAC,
               span_max_frac: float = SPAN_MAX_FRAC) -> dict:
    """兼容接口：返回 index 列表（仅模块内部/调试用，见 order_blocks 的提醒）。"""
    ordered, diag = order_blocks(blocks, page_width, tol_frac, span_max_frac)
    return {"order": [b.index for b in ordered], **diag}


def apply_order(page: dict, blocks_key: str = "parsing_res_list",
                tol_frac: float = COL_TOL_FRAC,
                span_max_frac: float = SPAN_MAX_FRAC) -> dict:
    """就地重排一页并写 block_order（1 起连续），返回诊断信息。"""
    raw_blocks = page.get(blocks_key) or []
    bs = [Block.of(d, i) for i, d in enumerate(raw_blocks)]
    ordered, diag = order_blocks(bs, page.get("width"), tol_frac, span_max_frac)

    page[blocks_key] = [b.raw for b in ordered]
    for pos, b in enumerate(ordered, 1):
        b.raw["block_order"] = pos

    meta = {
        "col_count": len(diag["cols"]),
        "cols": [[round(c["x1"], 1), round(c["x2"], 1)] for c in diag["cols"]],
        "col_blocks": [c["n"] for c in diag["cols"]],
        "span_blocks": diag["spans"],
        "warnings": diag["warnings"],
    }
    page["_order_meta"] = meta
    return meta


def iter_pages(data: Any) -> list[dict]:
    """兼容两种输入：单页 dict 或页数组 [{...}]。"""
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    if isinstance(data, dict):
        if "parsing_res_list" in data:
            return [data]
        for k in ("pages", "prism_result", "layouts"):
            if isinstance(data.get(k), list):
                return [p for p in data[k] if isinstance(p, dict)]
    raise ValueError("认不出结构：需要单页 dict 或页数组（每页含 parsing_res_list）")
