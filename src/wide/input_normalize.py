#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入归一 input_normalize.py —— 散页图片目录 → 病例 PDF（无损嵌入）+ 多患者守卫。

为什么存在：产线只认 <源根>/<院区>/<病例>/<文件>.pdf（s1_ocr.discover 只 rglob("*.pdf")），
网页上传也硬要求 zip 里有 .pdf（webui/ingest.py:unpack_zip）。散页图片语料因此进不来。

四条硬约束（round42 补充条款 C/D/F）：
  * 无损：JPEG 原字节直接作为 /DCTDecode 流嵌入，不重编码、不重采样、不改 DPI；
    从 PDF 里抽回的图像字节必须与原文件**逐字节相同**（verify 脚本证这条）。
  * 色彩空间：JPEG 1 分量 → /DeviceGray，3 分量 → /DeviceRGB；
    4 分量（CMYK）/2 分量（YCCK）→ 拒绝合并并报告，不猜。
  * EXIF 方向：不动像素，用页面 CTM 矩阵表达 8 种方向（5/6/7/8 时页面宽高互换）。
    产线渲染走 pypdfium2.render（s1_ocr.render_page:95）→ CTM 会被尊重。
  * 排序：一律自然序。字典序会把 10.jpg 排到 2.jpg 前面 ⇒ 页序全乱 ⇒ 首页/首程定位全废。

守卫（比合并本身更重要）：一个散页目录里可能混着两个患者的页 —— 合成一个 PDF
等于凭空造一份不存在的病历。合并前抽 首页/中间/末页 各 1 页做轻量 OCR，
比对页间「住院号」与「姓名」；读到 ≥2 个不同值 ⇒ 禁止合并 + 写 _merge_alerts.json
+ 列入人工确认清单。读不到值 ≠ 通过，记 guard_unreadable 同样进人工清单。

非 JPEG（png/tif/tiff/bmp）走 FlateDecode：像素逐点无损，但**重新压缩**（不是原字节），
manifest 里 embed=flate-lossless 标出来，不与 JPEG 的字节级无损混为一谈。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import struct
import sys
import time
import zlib
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wide import common as C  # noqa: E402

IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
JPEG_EXTS = {".jpg", ".jpeg"}
IGNORE_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}
PDF_EXTS = {".pdf"}

# 文件名里嵌的 id 前缀（实测语料两种命名：<住院号>_pageN.jpg 和 <文书名>_pageN.jpg）。
# 同一目录里出现 ≥2 个不同的数字前缀 ⇒ 这个目录**几乎肯定**混了两个患者的页。
# 这条是零成本守卫（只看文件名，不 OCR）：实测 2449 目录 / 4.1 s / 0 个多前缀目录。
NAME_ID_RE = re.compile(r"^(\d{6,})")


def name_prefix_signal(names, dirname: str) -> dict:
    """一个目录的图片文件名 → 数字前缀一致性信号。

    status: multi（≥2 个不同前缀 ⇒ 硬报警） / single（1 个前缀） / none（没有数字前缀）。
    ★ none 不等于通过 —— 它只是「这条查不了」，必须照实记下来，不许冒充有值（红线⑥）。
    """
    pref = set()
    for n in names:
        m = NAME_ID_RE.match(n)
        if m:
            pref.add(m.group(1))
    if len(pref) >= 2:
        return {"status": "multi", "prefixes": sorted(pref), "dirname": dirname}
    if len(pref) == 1:
        p = next(iter(pref))
        return {"status": "single", "prefixes": [p], "dirname": dirname,
                "matches_dirname": p == dirname}
    return {"status": "none", "prefixes": [], "dirname": dirname}


# --------------------------------------------------------------- 排序（自然序）
def natural_key(s: str) -> list:
    """数字段按整数值比，其余按小写串比。10.jpg 排在 2.jpg 之后。"""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", str(s))]


def sort_natural(paths) -> list:
    """按**文件名**自然序（不是整条路径 —— 同一目录内文件名才是页序）。"""
    return sorted(paths, key=lambda p: natural_key(Path(p).name))


# --------------------------------------------------------------- 发现与分类
def is_ignored(name) -> bool:
    b = Path(str(name)).name
    return b.lower() in IGNORE_NAMES or b.startswith(".")


def classify_dir(d: Path) -> dict:
    """一个散页目录 → 图片 / PDF / 其它 / 忽略项（忽略项逐个计数，不静默吞）。"""
    out = {"dir": Path(d), "images": [], "pdfs": [], "other": [], "zero_byte": [],
           "ignored": Counter()}
    for p in sorted(Path(d).iterdir()):
        if not p.is_file():
            continue
        if is_ignored(p.name):
            out["ignored"][Path(p.name).name.lower() if p.name.lower() in IGNORE_NAMES
                          else "hidden:" + p.name] += 1
            continue
        ext = p.suffix.lower()
        if p.stat().st_size == 0:
            out["zero_byte"].append(p.name)
            continue
        if ext in IMG_EXTS:
            out["images"].append(p)
        elif ext in PDF_EXTS:
            out["pdfs"].append(p)
        else:
            out["other"].append(p)
    out["images"] = sort_natural(out["images"])
    return out


def scan_image_dirs(root: Path, max_dirs: int = 0,
                    max_seconds: float = 0.0) -> tuple[list, bool]:
    """root 下所有**含图片**的目录（递归，跳过隐藏目录）→ (列表, 是否被上限截断)。

    max_dirs / max_seconds 传 0 = 不限（跑批用，必须扫全）。网页预检是**同步 HTTP 请求**，
    必须能中途收手 ⇒ 传上限，被截断时把「计数只是下限」如实报出去，不许当扫完了。
    """
    root = Path(root)
    out = []
    deadline = (time.monotonic() + max_seconds) if max_seconds else None
    truncated = False
    for dirpath, dirnames, _files in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not is_ignored(d))
        d = Path(dirpath)
        if any(p.suffix.lower() in IMG_EXTS for p in d.iterdir() if p.is_file()):
            out.append(d)
            if max_dirs and len(out) >= max_dirs:
                truncated = True
                break
        if deadline is not None and time.monotonic() > deadline:
            truncated = True
            break
    return sorted(out), truncated


def find_image_dirs(root: Path) -> list:
    """root 下所有**含图片**的目录（不限上限 ⇒ 跑批口径，漏一个目录 = 少一份病历）。"""
    return scan_image_dirs(root)[0]


# --------------------------------------------------------------- 图片结构探针
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_info(data: bytes) -> dict:
    """扫 JPEG 段（到 SOS 为止）：尺寸 / 分量数 / 是否渐进 / JFIF DPI / 有无 APP1。"""
    out = {"w": None, "h": None, "components": None, "sof": None, "progressive": False,
           "density": None, "density_units": None, "has_app1": False, "error": None,
           "tail_eoi": data[-2:] == b"\xff\xd9"}
    if data[:2] != b"\xff\xd8":
        out["error"] = "no SOI marker"
        return out
    i = 2
    while i + 1 < len(data):
        if data[i] != 0xFF:
            out["error"] = "marker desync at byte %d" % i
            return out
        while i + 1 < len(data) and data[i + 1] == 0xFF:
            i += 1
        m = data[i + 1]
        if m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xD9:
            break
        if m == 0xDA:
            break
        if i + 4 > len(data):
            out["error"] = "truncated segment header at byte %d" % i
            return out
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        if m in _SOF and out["sof"] is None:
            out["sof"] = "0x%02X" % m
            out["components"] = data[i + 9]
            out["h"] = struct.unpack(">H", data[i + 5:i + 7])[0]
            out["w"] = struct.unpack(">H", data[i + 7:i + 9])[0]
            out["progressive"] = m in (0xC2, 0xC6, 0xCA, 0xCE)
        if m == 0xE0 and data[i + 4:i + 8] == b"JFIF":
            out["density_units"] = data[i + 11]
            out["density"] = (struct.unpack(">H", data[i + 12:i + 14])[0],
                              struct.unpack(">H", data[i + 14:i + 16])[0])
        if m == 0xE1:
            out["has_app1"] = True
        i += 2 + seglen
    return out


def exif_orientation(path: Path):
    """EXIF 方向标记（1..8）；没有 EXIF / 读不到 → None（按 1 处理，并记账）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.getexif().get(0x0112)
    except Exception:
        return None


def image_item(path: Path) -> dict:
    """一张图 → PDF 图像 XObject 的原料。JPEG 走原字节，其它走无损 Flate。"""
    p = Path(path)
    raw = p.read_bytes()
    ext = p.suffix.lower()
    info = {"path": p, "bytes": len(raw), "sha256": C.sha256_bytes(raw),
            "orientation": exif_orientation(p) if ext in JPEG_EXTS or ext == ".png" else None,
            "embed": "", "colorspace": "", "note": ""}
    if ext in JPEG_EXTS:
        j = jpeg_info(raw)
        info.update(j)
        comp = j["components"]
        if j["error"]:
            info["note"] = "jpeg_scan_error:" + str(j["error"])
        if not j["tail_eoi"]:
            info["note"] = (info["note"] + "; " if info["note"] else "") + "jpeg_truncated"
        if comp == 1:
            info["colorspace"] = "/DeviceGray"
        elif comp == 3:
            info["colorspace"] = "/DeviceRGB"
        else:
            info["note"] = (info["note"] + "; " if info["note"] else "") + \
                "unsupported_components_%s" % comp
            return info
        info["embed"] = "dct-raw"
        info["stream"] = raw
        info["filter"] = "/DCTDecode"
        return info

    from PIL import Image
    with Image.open(p) as im:
        mode = im.mode
        w, h = im.size
        ori = im.getexif().get(0x0112)
        if mode in ("L", "1", "I;16", "I"):
            cs, samples = "/DeviceGray", im.convert("L").tobytes()
        elif mode == "RGB":
            cs, samples = "/DeviceRGB", im.tobytes()
        elif mode == "P":
            cs, samples = "/DeviceRGB", im.convert("RGB").tobytes()
        else:
            info["note"] = "unsupported_mode_%s" % mode
            info.update({"w": w, "h": h, "components": None, "density": None,
                         "density_units": None, "progressive": False, "error": None,
                         "tail_eoi": True, "has_app1": False})
            info["orientation"] = ori
            return info
    info.update({"w": w, "h": h, "components": 1 if cs == "/DeviceGray" else 3,
                 "sof": None, "progressive": False, "density": None,
                 "density_units": None, "has_app1": False, "error": None, "tail_eoi": True})
    info["colorspace"] = cs
    info["embed"] = "flate-lossless"
    info["stream"] = zlib.compress(samples, 9)
    info["filter"] = "/FlateDecode"
    info["orientation"] = ori
    info["note"] = (info["note"] + "; " if info["note"] else "") + "pixels_lossless_recompressed"
    return info


def page_points(item: dict) -> tuple:
    """页面几何（pt）。DPI 取 JFIF 声明（1=inch，2=cm），没声明按 72 dpi（1 px = 1 pt）。"""
    dpi_x, dpi_y = 72.0, 72.0
    d, units = item.get("density"), item.get("density_units")
    if d and d[0] > 0 and d[1] > 0:
        if units == 2:
            dpi_x = d[0] * 2.54
            dpi_y = d[1] * 2.54
        elif units in (1, None):
            dpi_x, dpi_y = float(d[0]), float(d[1])
    w_pt = item["w"] * 72.0 / dpi_x
    h_pt = item["h"] * 72.0 / dpi_y
    return w_pt, h_pt, dpi_x, dpi_y


# --------------------------------------------------------------- EXIF 方向 → CTM
def orientation_matrix(ori: int, pw: float, ph: float) -> tuple:
    """把「存进来的图」映射到「页面上该看到的样子」的 PDF 矩阵（不动像素）。

    单位正方形 (u,v)：u=0 左 / u=1 右，v=0 底 / v=1 顶（PDF 图像空间原点在左下）。
    返回 (a,b,c,d,e,f)：x = a*u + c*v + e，y = b*u + d*v + f。
    """
    o = int(ori or 1)
    if o == 1:
        return (pw, 0.0, 0.0, ph, 0.0, 0.0)
    if o == 2:
        return (-pw, 0.0, 0.0, ph, pw, 0.0)
    if o == 3:
        return (-pw, 0.0, 0.0, -ph, pw, ph)
    if o == 4:
        return (pw, 0.0, 0.0, -ph, 0.0, ph)
    if o == 5:
        return (0.0, -ph, -pw, 0.0, pw, ph)
    if o == 6:
        return (0.0, -ph, pw, 0.0, 0.0, ph)
    if o == 7:
        return (0.0, ph, pw, 0.0, 0.0, 0.0)
    if o == 8:
        return (0.0, ph, -pw, 0.0, pw, 0.0)
    raise ValueError("非法 EXIF 方向：%r" % ori)


def display_points(item: dict) -> tuple:
    """页面宽高（pt），已按 EXIF 方向交换（5/6/7/8 转 90/270 度）。"""
    w_pt, h_pt, dx, dy = page_points(item)
    o = int(item.get("orientation") or 1)
    if o in (5, 6, 7, 8):
        return h_pt, w_pt, dx, dy
    return w_pt, h_pt, dx, dy


# --------------------------------------------------------------- PDF 写出（确定性）
def build_pdf(items: list, out_path: Path) -> dict:
    """最小 PDF 1.5：每页一个图像 XObject。字节级确定性（无时间戳）⇒ 幂等可比 sha256。"""
    # 对象编号：1=Catalog 2=Pages，之后每例 3 个 —— page / content / image。
    # ★ /Pages /Kids 必须指向 /Page 对象本身（曾误指 content 流 ⇒ pdfium 找不到页，
    #   回落成默认 612x792，页序与尺寸全废）。
    page_ids, content_ids, img_ids = [], [], []
    nid = 3
    for it in items:
        page_ids.append(nid)
        content_ids.append(nid + 1)
        img_ids.append(nid + 2)
        nid += 3
    n_objs = nid - 1

    body = bytearray()
    body += b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n"
    offsets = {}

    def emit(num: int, payload: bytes) -> None:
        offsets[num] = len(body)
        body.extend(b"%d 0 obj\n" % num + payload + b"\nendobj\n")

    kids = b" ".join(b"%d 0 R" % k for k in page_ids)
    emit(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    emit(2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(items))

    for idx, it in enumerate(items):
        pw, ph, _dx, _dy = display_points(it)
        a, b, c, d, e, f = orientation_matrix(it.get("orientation") or 1, pw, ph)
        img_id = img_ids[idx]
        stream = it["stream"]
        img_dict = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                    b"/ColorSpace %s /BitsPerComponent 8 /Filter %s /Length %d >>"
                    % (it["w"], it["h"], it["colorspace"].encode(), it["filter"].encode(),
                       len(stream)))
        emit(img_id, img_dict + b"\nstream\n" + stream + b"\nendstream")

        content = (b"q\n%.4f %.4f %.4f %.4f %.4f %.4f cm\n/Im0 Do\nQ\n"
                   % (a, b, c, d, e, f))
        cid = content_ids[idx]
        emit(cid, b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")

        page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.4f %.4f] "
                b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                % (pw, ph, img_id, cid))
        emit(page_ids[idx], page)

    start = len(body)
    body += b"xref\n0 %d\n" % (n_objs + 1)
    body += b"0000000000 65535 f \n"
    for num in range(1, n_objs + 1):
        body += b"%010d 00000 n \n" % offsets[num]
    body += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
             % (n_objs + 1, start))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = bytes(body)
    return {"pdf_path": out_path, "pdf_bytes": len(data),
            "pdf_sha256": C.sha256_bytes(data), "data": data, "n_pages": len(items)}


def write_pdf_atomically(built: dict, out_path: Path) -> None:
    tmp = Path(str(out_path) + ".tmp")
    tmp.write_bytes(built["data"])
    os.replace(str(tmp), str(out_path))
    os.chmod(out_path, 0o600)


# --------------------------------------------------------------- 守卫：多患者检测
GUARD_FIELDS = ("adm_no", "patient_name")
GUARD_SAMPLE_PAGES = 5
# 单页嫌疑默认「照常合并 + 进人工复核清单」。要恢复成硬拦（宁可整例不产出）：
#   MEDREC_GUARD_STRICT=1
GUARD_STRICT_SUSPECTS = os.environ.get("MEDREC_GUARD_STRICT", "") == "1"


def guard_sample_indices(n: int) -> list:
    """首页 / 末页 + 中间均匀取样（去重）。n=1 → [0]；n<=k → 全取。

    ★ 上一版只抽 3 页 ⇒ 单页 OCR 抖动就能把整例拦掉（实测 2449 目录拦下 127 例，
      抽查 6 例全是误报）。5 页 + 按页支持数判定，才能把「一页读歪」和「真有两个患者」分开。
    """
    if n <= 0:
        return []
    k = GUARD_SAMPLE_PAGES
    if n <= k:
        return list(range(n))
    return sorted(set(round(i * (n - 1) / (k - 1)) for i in range(k)))


def guard_render_png(item: dict, out_png: Path, render_cfg: dict) -> dict:
    """按 s1 的缩放三重约束把**原图**渲染成 PNG（含 EXIF 转正）。

    ★ 缩放因子是从**页面点数**算出来的（s1_ocr.render_page:102），所以必须乘在点数上，
      不能乘在像素数上 —— 乘像素会得到 17 倍于 target_px 的图（300 dpi 时 4 倍边长），
      守卫看到的分辨率就和产线不一样了。对照用；守卫本身走 render_pdf_page_png。
    """
    from PIL import Image, ImageOps
    pw, ph, _dx, _dy = display_points(item)
    target_px = float(render_cfg["target_px"])
    scale = min((target_px / (pw * ph)) ** 0.5, float(render_cfg["max_upscale"]),
                float(render_cfg["max_side_pt"]) / max(pw, ph))
    scale = max(scale, float(render_cfg["min_scale"]))
    with Image.open(item["path"]) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        tw = max(1, int(round(pw * scale)))
        th = max(1, int(round(ph * scale)))
        im = im.resize((tw, th))
        out_png.parent.mkdir(parents=True, exist_ok=True)
        im.save(out_png, format="PNG")
        C.chmod(out_png)                # 渲染页含患者影像/PII → 600（同 s1_ocr.py:1171）
    return {"png": out_png, "scale": round(scale, 4), "w": tw, "h": th}


def render_pdf_page_png(pdf_bytes: bytes, page_idx: int, out_png: Path,
                        render_cfg: dict, expect_pt: tuple | None = None) -> dict:
    """从**合并后的 PDF 字节**渲染一页 = 产线 s1_ocr.render_page 同一条路径。

    守卫必须看产线会看到的东西，而不是 PIL 另算一遍的原图：
      * 分辨率/缩放与 s1 完全一致；
      * 顺带把 PDF 结构也验了 —— /Kids 指错对象时 pdfium 会静默回落成默认
        612x792 页（本轮实测踩过），expect_pt 对不上就当场报错，不让它混过去。
    """
    import pypdfium2 as pdfium
    from wide import s1_ocr as S1
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        got_pt = doc[page_idx].get_size()
        im, scale = S1.render_page(doc, page_idx, render_cfg)
    finally:
        doc.close()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_png, format="PNG")
    C.chmod(out_png)                    # 渲染页含患者影像/PII → 600（同 s1_ocr.py:1171）
    note = ""
    if expect_pt and (abs(got_pt[0] - expect_pt[0]) > 0.01
                      or abs(got_pt[1] - expect_pt[1]) > 0.01):
        note = ("page_size_mismatch pdf_pt=%.2fx%.2f expect=%.2fx%.2f"
                % (got_pt[0], got_pt[1], expect_pt[0], expect_pt[1]))
    return {"png": out_png, "scale": round(scale, 4), "w": im.size[0], "h": im.size[1],
            "pdf_pt": [round(got_pt[0], 2), round(got_pt[1], 2)], "note": note}


def guard_label_pattern() -> tuple:
    """标签词表 = **产线同一份** config/label_vocab.yaml（经 s3_extract.load_vocab）。

    为什么不用守卫自己的两个字段：scan_page 把「本标签末尾 → 下一标签开头」当值段，
    词表少了 性别/年龄/… 就会把它们全吞进 姓名 的值里 ⇒ 守卫读到的值与产线不一致。
    返回 (pat, missing)：missing = RULES 用到但词表没有的标签（会让守卫假 unreadable，
    必须显式报出来，不许静默）。
    """
    from wide import frontpage_rules as FR
    from wide import s3_extract as S3
    alias2canon, _v = S3.load_vocab()
    missing = []
    for col in GUARD_FIELDS:
        spec = FR.RULES[col]
        labs = (list(spec.get("labels") or []) + list(spec.get("fallback_labels") or [])
                + list(spec.get("anchor_after") or []) + list(spec.get("anchor_before") or []))
        missing += [l for l in labs if l not in alias2canon]
    return S3.label_pattern(alias2canon), sorted(set(missing))


def guard_read_fields(png_path: Path, cfg: dict) -> dict:
    """一页 PNG → OCR 文本 → 用产线同一套 frontpage_rules 取 住院号 / 姓名。"""
    from wide import s1_ocr as S1
    from wide import frontpage_rules as FR
    ocr_cfg = cfg["ocr"]
    _pat, missing = guard_label_pattern()
    if missing:
        return {"error": "vocab_missing:" + ",".join(missing), "values": {},
                "cands": {}, "text_len": 0, "truncated": False}
    pr, _raw, err = S1.ocr_page_vllm(Path(png_path), ocr_cfg)
    if pr is None:
        return {"error": err or "ocr_no_response", "values": {}, "text_len": 0,
                "cands": {}, "truncated": False}
    text = "\n".join(str(b.get("block_content") or "")
                     for b in pr.get("parsing_res_list") or [])
    pat, _missing = guard_label_pattern()
    occ = FR.scan_page(text, pat)
    for o in occ:
        o.setdefault("page", 0)
        o.setdefault("block", 0)
    values = {}
    cands = {}
    for col in GUARD_FIELDS:
        got = FR.apply_rule(FR.RULES[col], occ)
        values[col] = got["hit"]["value"] if got["hit"] else None
        cands[col] = [{"value": c["value"], "label": c["source_label"]}
                      for c in got["cands"]]
    return {"error": "", "values": values, "cands": cands, "text_len": len(text),
            "truncated": bool(pr.get("_truncated")), "url": pr.get("_url", "")}


def mask_name(v) -> str:
    """PII：姓名不进 stdout / 日志（columns.yaml pii: true）。留长度 + sha8 供对账。"""
    if not v:
        return ""
    s = str(v)
    return (s[0] + "*" * (len(s) - 1)) if len(s) > 1 else "*"


def sha8(v) -> str:
    return hashlib.sha256(str(v).encode("utf-8")).hexdigest()[:8] if v else ""


def mask_case_id(s) -> str:
    """stdout/日志用：目录名/文件名可能内嵌姓名（红线⑨）。

    ASCII 段（院区拼音 + 病案号）原样保留 —— 那是定位必需的；
    连续中文段按 mask_name 打码（留首字）。
    """
    if not s:
        return ""
    return re.sub(r"[\u4e00-\u9fff]+", lambda m: mask_name(m.group(0)), str(s))


# -------------------------------------------------- 守卫：值比对（容忍 OCR 抖动）
# 实测（/raid5/history_13_15，2449 个散页目录，2026-10-16/17）：
# 上一版把「抽到的页里出现 ≥2 个不同字符串」直接判成两个患者 ⇒ 拦下 127 个目录。
# 抽查 6 个（每个都重跑 OCR 并打出取值的来源标签）⇒ 6 个全是误报，误报来源三条：
#   ① v_code 把日期/年份/表单名当成住院号：恩泽 6002058215 第 80 页 标签=住院号 取值=NRS2002；
#      桐乡 00403563 第 54 页 取值=2024；乐清 166160247 取值=2025-01-14。
#   ② 同一例不同表单的编号体系不同：安吉 img20240123_09182519 第 1 页 住院号=02493031、
#      第 46 页 **病历号**=90411853、三页姓名同一个 ⇒ 跨标签族比字符串必然打架。
#   ③ OCR 抖动：乐清 200555565 vs 20055565（掉一位）、安吉 Z02488632 vs 02488632（0 读成 Z）、
#      姓名同长度差一个字（50 例）。
# ⇒ 这一版不是放宽守卫，是换判据：**同标签族 + 归一化 + 看有几页支持**。
#   真混两个患者（同族两个编号各自被 ≥2 页支持）照样硬拦；只有单页嫌疑进人工清单不硬拦。


def id_label_family() -> dict:
    """标签 → 编号族。住院号族与病案号/病历号族是两个体系，跨族不比对。

    词表来源 = 产线同一份 frontpage_rules.RULES（adm_no.labels vs case_no.labels），
    不在守卫里另立一套标签，否则又会出现「守卫看到的」和「产线抽到的」不一致。
    """
    from wide import frontpage_rules as FR
    fam = {}
    for lab in (FR.RULES["adm_no"].get("labels") or []):
        fam[lab] = "adm"
    for lab in (FR.RULES["case_no"].get("labels") or []):
        fam.setdefault(lab, "case")
    return fam


DATEISH_RE = re.compile(r"^(?:19|20)\d{2}(?:\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2})?$")


def digit_seq(v) -> str:
    return re.sub(r"\D", "", str(v or "")).lstrip("0")


def id_is_noise(v) -> bool:
    """日期 / 年份 / 位数太短 ⇒ 不是编号，不参与患者比对。

    最短 5 位数字的依据：本批六个院区真实编号最短是桐乡 6 位（00360656 → 360656），
    而误取值全是 4 位（2024 / 2002）或带分隔符的日期。
    """
    s = re.sub(r"\s", "", str(v or ""))
    if not s or DATEISH_RE.match(s):
        return True
    return len(digit_seq(v)) < 5


def edit_le1(a: str, b: str) -> bool:
    """编辑距离 ≤1（只允许一次替换/插入/删除）。不引第三方库。"""
    if a == b:
        return True
    if len(a) > len(b):
        a, b = b, a
    if len(b) - len(a) > 1:
        return False
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if i == len(a):
        return True
    return a[i:] == b[i + 1:]


def id_relation(a, b) -> str:
    x, y = digit_seq(a), digit_seq(b)
    if not x or not y:
        return "unknown"
    if x == y:
        return "same"
    if (x in y) or (y in x) or edit_le1(x, y):
        return "ocr_variant"
    return "different"


def name_relation(a, b) -> str:
    a, b = re.sub(r"\s", "", str(a or "")), re.sub(r"\s", "", str(b or ""))
    if a == b:
        return "same"
    if len(a) == len(b) and sum(1 for x, y in zip(a, b) if x != y) <= 1:
        return "ocr_variant"
    if abs(len(a) - len(b)) == 1 and (a in b or b in a):
        return "ocr_variant"
    return "different"


def cluster_values(pairs: list, rel_fn) -> list:
    """[(value, page_index)] → 簇。同簇 = 与簇内任一值不是 different（传递合并）。"""
    clusters: list = []
    for val, pg in pairs:
        hit = None
        for cl in clusters:
            if any(rel_fn(val, v) != "different" for v, _p in cl["items"]):
                hit = cl
                break
        if hit is None:
            clusters.append({"items": [(val, pg)]})
        else:
            hit["items"].append((val, pg))
    for cl in clusters:
        cnt = Counter(v for v, _p in cl["items"])
        cl["value"] = cnt.most_common(1)[0][0]
        cl["variants"] = sorted(cnt)
        cl["pages"] = sorted({p for _v, p in cl["items"]})
        cl["n_pages"] = len(cl["pages"])
    return clusters


def guard_verdict(buckets: dict, rel_fns: dict, strict=None, masks=None) -> dict:
    """按族聚类后的判定。

    hard    = 同族两个簇**各自被 ≥2 页支持** ⇒ 这个目录里确实有两个患者的页 ⇒ 禁止合并。
              或 **两个字段同时**出现单页嫌疑（编号与姓名各只有一个簇落在 1 页上）
              ⇒ 两条独立证据指向同一件事，同样算硬证据。
    suspect = 只有一个字段出现单页嫌疑 ⇒ 单页污染嫌疑：不硬拦（整例丢掉等于凭空少
              一份病历，和凭空多一份同样是错），但必须进报警清单等人确认。
              MEDREC_GUARD_STRICT=1 把这一档也升回硬拦。
    variants= 同簇内的 OCR 抖动（记账，不算冲突）。

    ★ masks = {bucket: 打码函数}，**只在输出处**打码。聚类/比对一律用原值：
      姓名先 mask_name 再比 ⇒ 方** 与 邵** 只差 1 个可见字符 ⇒ name_relation 判
      ocr_variant ⇒ 姓名这条证据直接失效（round42p 合成回归被打绿就是这个 bug）。
    """
    hard, suspect, variants = [], [], []
    mk = masks or {}
    for bname, pairs in buckets.items():
        cls = cluster_values(pairs, rel_fns[bname])
        mask = mk.get(bname) or (lambda v: v)
        cls.sort(key=lambda c: (-c["n_pages"], c["value"]))
        if len(cls) >= 2:
            view = [{"value_masked": mask(c["value"]), "n_pages": c["n_pages"],
                     "pages": c["pages"], "n_variants": len(c["variants"])} for c in cls]
            if cls[0]["n_pages"] >= 2 and cls[1]["n_pages"] >= 2:
                hard.append({"bucket": bname, "clusters": view})
            else:
                suspect.append({"bucket": bname, "clusters": view})
        for c in cls:
            if len(c["variants"]) >= 2:
                variants.append({"bucket": bname, "value_masked": mask(c["value"]),
                                 "n_variants": len(c["variants"]), "pages": c["pages"]})
    if len({p["bucket"] for p in suspect}) >= 2:
        hard.extend(suspect)
        suspect = []
    if strict is None:
        strict = GUARD_STRICT_SUSPECTS
    if strict and suspect:
        hard.extend(suspect)
        suspect = []
    return {"hard": hard, "suspect": suspect, "variants": variants}


def guard_group(items: list, pdf_bytes: bytes, cfg: dict, work_dir: Path) -> dict:
    """均匀抽 GUARD_SAMPLE_PAGES 页 → 按**标签族**比对页间 住院号 / 姓名。
    渲染源 = **合并后的 PDF 字节**（render_pdf_page_png），不是原图：分辨率与 s1 一致，
    且 PDF 结构错误（/Kids 指错对象 ⇒ pdfium 静默回落 612x792）会在这里暴露。
    页序 = items 的自然序 = PDF 页序。

    返回 status ∈ {ok, multi_patient, multi_patient_suspect, guard_unreadable, guard_error}。
    ★ merge_blocked 只在 hard（同族两个编号各自被 ≥2 页支持）时为真。
      suspect（只有 1 页支持的另一编号）照样合并，但一定进报警清单 —— 整例丢掉
      等于凭空少一份病历，和「凭空多一份」同样是错。
    ★ 取的是每页**全部**候选（带来源标签），不是 apply_rule 的第一条：
      产线自己跨页做多数表决（frontpage_rules.vote_candidates），
      守卫只取第一条 = 页眉那条，而页眉正是实测会被串污染的那条。
    """
    render_cfg = cfg.get("render") or {"target_px": 3900000, "max_side_pt": 3000.0,
                                       "max_upscale": 3.0, "min_scale": 0.5}
    idxs = guard_sample_indices(len(items))
    per_page = []
    vals = {c: [] for c in GUARD_FIELDS}
    fam = id_label_family()
    buckets = {"adm": [], "case": [], "name": []}
    noise_ids = []
    for i in idxs:
        item = items[i]
        png = Path(work_dir) / ("page_%04d.png" % i)
        rr = render_pdf_page_png(pdf_bytes, i, png, render_cfg,
                                 expect_pt=display_points(item)[:2])
        got = guard_read_fields(png, cfg)
        rec = {"page_index": i, "page_no_1based": i + 1, "file": Path(item["path"]).name,
               "sha256": item["sha256"], "render": {k: rr[k] for k in ("scale", "w", "h")},
               "pdf_pt": rr["pdf_pt"], "page_size_note": rr["note"],
               "text_len": got["text_len"], "truncated": got["truncated"],
               "error": got["error"], "values": {}}
        if rr["note"]:
            rec["error"] = ((rec["error"] + ";") if rec["error"] else "") + rr["note"]
        for col in GUARD_FIELDS:
            v = got["values"].get(col)
            rec["values"][col] = {"value_masked": mask_name(v) if col == "patient_name" else (v or ""),
                                  "sha8": sha8(v)}
            if v:
                vals[col].append({"page_index": i, "value": v})
            for cd in got.get("cands", {}).get(col, []):
                if col == "adm_no":
                    if id_is_noise(cd["value"]):
                        noise_ids.append({"page_index": i, "value": cd["value"],
                                          "label": cd["label"]})
                        continue
                    buckets[fam.get(cd["label"], "adm")].append((cd["value"], i))
                else:
                    # ★ 存**原值**，打码推迟到 guard_verdict 输出处（见那里的 masks 说明）
                    buckets["name"].append((str(cd["value"]).strip(), i))
            rec["cands_" + col] = [{"value_masked": (mask_name(c["value"])
                                                     if col == "patient_name" else c["value"]),
                                    "label": c["label"],
                                    "family": (fam.get(c["label"], "adm")
                                               if col == "adm_no" else "name")}
                                   for c in got.get("cands", {}).get(col, [])]
        per_page.append(rec)

    vd = guard_verdict(buckets, {"adm": id_relation, "case": id_relation,
                                 "name": name_relation},
                       masks={"name": mask_name})
    distinct = {c: sorted({x["value"] for x in vals[c]}) for c in GUARD_FIELDS}
    conflicts = {p["bucket"]: p["clusters"] for p in vd["hard"]}
    unreadable = [c for c in GUARD_FIELDS if not distinct[c]]
    ocr_errors = [p for p in per_page if p.get("error")]
    bad_geometry = [p for p in per_page if p.get("page_size_note")]

    if vd["hard"]:
        status = "multi_patient"
    elif bad_geometry:
        status = "guard_error"
    elif ocr_errors and len(ocr_errors) == len(per_page):
        status = "guard_error"
    elif vd["suspect"]:
        status = "multi_patient_suspect"
    elif unreadable:
        status = "guard_unreadable"
    else:
        status = "ok"
    return {"status": status, "sampled_pages": idxs, "per_page": per_page,
            "distinct": {c: [{"value_masked": mask_name(v) if c == "patient_name" else v,
                              "sha8": sha8(v)} for v in distinct[c]] for c in GUARD_FIELDS},
            "conflicts": conflicts,
            "hard_conflicts": vd["hard"], "suspect_conflicts": vd["suspect"],
            "id_variants": vd["variants"], "noise_ids": noise_ids,
            "unreadable_fields": unreadable, "n_ocr_errors": len(ocr_errors),
            "bad_geometry_pages": [p["page_index"] for p in bad_geometry],
            "merge_blocked": bool(vd["hard"])}


# --------------------------------------------------------------- 合并驱动
def case_id_for(src_root: Path, d: Path) -> str:
    rel = Path(d).resolve().relative_to(Path(src_root).resolve())
    return C.case_id_from_rel(rel)


def safe_part(s) -> str:
    """路径段消毒（与 webui/ingest._safe_id 同规则）：非法字符换 '_'，空段换 '_'。"""
    s = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", str(s or "").strip())
    return s or "_"


def case_hospital_from_rel(rel) -> tuple[str, str]:
    """rel（图片目录相对扫描根）→ (院区, 病例)。**层深不限**，只认「最后一段 = 病例」。

    ★ 往上找院区时必须**跳过与病例目录同名的段**。实测两种形状
      （/raid5/history_13_15，2026-10-16，2449 个散页目录 / 142317 页）：
        history_13/caseimages_20250516/<院区>/<病例>/*.jpg        ← 病例目录只有一层
        history_15/乐清市人民医院/<病例>/<病例>/*.jpg             ← 病例目录套了两层
      照抄 parts[-2] 的取法在第二种形状上把**院区层写成病例号**：深度还是 3，
      G1 形状守卫查不出来，但 ① 标准化就诊医院 注入的是病例号（脏数据），
      ② 两份副本（乐清市人民医院 / 乐清市人民医院_20250520）会撞成同一个输出路径
      ⇒ 实测 2449 个目录只剩 1772 个唯一路径，677 个 PDF 被后一份静默覆盖。
    """
    parts = [p for p in Path(str(rel)).parts if p not in ("", ".", "..")]
    if not parts:
        return "unknown", "_"
    cs = parts[-1]
    idx = len(parts) - 2
    while idx >= 0 and safe_part(parts[idx]) == safe_part(cs):
        idx -= 1
    hs = parts[idx] if idx >= 0 else "unknown"
    return hs, cs


def tree_layers(src_root: Path, d: Path) -> tuple[str, str, str]:
    """(院区, 病例, 备注) —— 备注非空 ⇒ 院区层是**从扫描根名补出来的**，要人看一眼。

    rel 里翻不出院区层只有两种情况，都按 s1_ocr.discover 的 leaf_fallback 同口径补
    （round34 E4b：不补的话 标准化就诊医院 注入不到 ⇒ 去重键少分量）：
      * 用户选到了**院区层**（rel = 病例/病例）⇒ 院区 = 这一层的目录名
      * 用户选到了**病例层**（rel 为空）      ⇒ 病例 = 根名、院区 = 根的上一级
      * 用户选到了**病例层**但图片目录套了一层同名子目录（rel = 病例，且这一段 == 根名）
        ⇒ 同上：病例 = 根名、院区 = 根的上一级。实测 history_15 是
        `<院区>/<病例>/<病例>/*.jpg`，只选到 <病例> 时 rel 只有一段，
        照「rel 有一段 = 选到院区层」那条补会把**病例号写进院区层**
        （标准化就诊医院 = 病例号 = 脏数据，且两份副本还撞同一输出路径）。
    补出来的名字是**目录名原样**（不是猜中文院名）；备注会一路带回网页，
    让操作的人自己决定要不要把源根往上选一层。
    """
    root = Path(src_root).resolve()
    rel = Path(d).resolve().relative_to(root)
    hs, cs = case_hospital_from_rel(rel)
    note = ""
    rp = [p for p in Path(str(rel)).parts if p not in ("", ".", "..")]
    if hs == "unknown" and cs == "_":
        cs, hs = root.name, root.parent.name
        note = ("扫描根本身就是病例目录 ⇒ 院区/病例用「根的上一级 __ 根名」补回两级"
                "（与 s1_ocr.discover 的 leaf_fallback 同口径）")
    elif hs == "unknown" and len(rp) == 1 and safe_part(rp[0]) == safe_part(root.name):
        cs, hs = root.name, root.parent.name
        note = ("你选到了病例目录（图片在它下面那层同名子目录里）⇒ 病例 = 根名、"
                "院区 = 根的上一级 %r。若那一级不是院区名，请把源根往上选一层。"
                % root.parent.name)
    elif hs == "unknown":
        hs = root.name
        note = ("你选到了院区层或更浅 ⇒ 院区层用这一层的目录名 %r 补。"
                "若它不是院区名，请把源根往上选一层。" % root.name)
    return hs, cs, note


def tree_target(src_root: Path, d: Path, out_root: Path) -> tuple[str, Path]:
    """图片目录 rel 的祖辈 = 院区/病例 ⇒ PDF 落 <out_root>/<院区>/<病例>/<病例>.pdf。

    这样暂存区本身就是产线认的深度 3 形状（s1_ocr.discover 直接能读），
    而且 case_id 与产线后面从**暂存根**算出来的 case_id 逐字一致（manifest 不串号）。

    ★ d 本身是**图片目录 = 病例目录** ⇒ 病例 = rel 的最后一段；院区 = 再往上第一个
      与病例名不同的段（病例目录自己套两层时必须多跳一层，见 case_hospital_from_rel）。
      别照抄 ingest.normalize_target 的 parts[-3]/parts[-2] —— 那个规则的对象是**文件** relpath。
      取错层的症状：院区层变成病例号、病例层变成上一层目录名，case_id 全串。
    """
    hs_raw, cs_raw, _note = tree_layers(src_root, d)
    hs, cs = safe_part(hs_raw), safe_part(cs_raw)
    cid = C.case_id_from_rel("%s/%s" % (hs, cs))
    return cid, Path(out_root) / hs / cs / ("%s.pdf" % cs)


def merge_image_dir(src_dir: Path, out_pdf: Path, cfg: dict, work_dir: Path,
                    guard: bool = True, case_id: str = "", name_guard: bool = True) -> dict:
    """一个散页目录 → 1 个 PDF。守卫不过 ⇒ 不产出，返回 blocked。"""
    cls = classify_dir(src_dir)
    imgs = cls["images"]
    row = {"case_id": case_id or Path(src_dir).name, "source_dir": str(src_dir),
           "n_images": len(imgs), "n_pdf_in_dir": len(cls["pdfs"]),
           "n_other": len(cls["other"]), "n_zero_byte": len(cls["zero_byte"]),
           "ignored": dict(cls["ignored"]), "status": "", "guard_status": "",
           "pdf_path": str(out_pdf), "pdf_sha256": "", "pdf_bytes": 0, "pages": []}
    if not imgs:
        row["status"] = "no_images"
        return row

    # 零成本守卫：文件名数字前缀冲突（不 OCR，只看名字）。OCR 守卫关掉时这条仍然默认在。
    if name_guard:
        sig = name_prefix_signal([Path(p).name for p in imgs], Path(src_dir).name)
        row["name_signal"] = sig["status"]
        if sig["status"] == "multi":
            row["status"] = "blocked_mixed_name_prefix"
            row["rejected"] = sig
            return row
    else:
        row["name_signal"] = "off"

    items = [image_item(p) for p in imgs]
    bad = [it for it in items if not it.get("stream")]
    if bad:
        row["status"] = "rejected_unreadable_image"
        row["rejected"] = [{"file": Path(it["path"]).name, "note": it.get("note")} for it in bad]
        return row

    # 先**在内存里**建 PDF，再对建出来的字节做守卫：守卫看的就是产线将要读的那份东西。
    # 守卫拦下 ⇒ 一个字节都不落盘（不许「先合了再说」）。
    built = build_pdf(items, out_pdf)
    if guard:
        g = guard_group(items, built["data"], cfg, work_dir)
        row["guard_status"] = g["status"]
        row["guard"] = g
        if g["merge_blocked"]:
            row["status"] = "blocked_multi_patient"
            return row
        if g["status"] == "guard_error":
            row["status"] = "blocked_guard_error"
            return row
    pre = None
    if Path(out_pdf).exists():
        old = C.sha256_file(Path(out_pdf))
        if old == built["pdf_sha256"]:
            row["status"] = "skipped_identical"
            row["pdf_sha256"] = old
            row["pdf_bytes"] = Path(out_pdf).stat().st_size
            row["pages"] = [{"page_no": k + 1, "source_file": Path(it["path"]).name,
                             "sha256": it["sha256"], "bytes": it["bytes"],
                             "w": it["w"], "h": it["h"], "colorspace": it["colorspace"],
                             "embed": it["embed"], "orientation": it.get("orientation") or 1,
                             "dpi": list(it.get("density") or [72, 72])}
                            for k, it in enumerate(items)]
            return row
        pre = {"target": str(out_pdf), "preexisting_sha256": old,
               "preexisting_bytes": Path(out_pdf).stat().st_size}
    # ★ 目标 PDF 已存在且字节不同：若目标就在**源目录自己身上**（sidecar）⇒ 覆盖 = 销毁原件，
    #   而这一例本来就有 PDF（用散页覆盖它 = 换掉一份病历）⇒ 禁止合并 + 报警，不许「先合了再说」。
    if pre and Path(out_pdf).resolve().parent == Path(src_dir).resolve():
        row["status"] = "blocked_preexisting_pdf"
        row["rejected"] = dict(pre, note="源目录内已有 PDF，覆盖会销毁原件；"
                                        "需人工确认这一例用散页还是用原 PDF")
        return row
    if pre:
        row["replaced_pdf_sha256"] = pre["preexisting_sha256"]
    write_pdf_atomically(built, out_pdf)
    row["status"] = "merged"
    row["pdf_sha256"] = built["pdf_sha256"]
    row["pdf_bytes"] = built["pdf_bytes"]
    row["pages"] = [{"page_no": k + 1, "source_file": Path(it["path"]).name,
                     "sha256": it["sha256"], "bytes": it["bytes"],
                     "w": it["w"], "h": it["h"], "colorspace": it["colorspace"],
                     "embed": it["embed"], "orientation": it.get("orientation") or 1,
                     "dpi": list(it.get("density") or [72, 72])}
                    for k, it in enumerate(items)]
    return row


MANIFEST_COLS = ["case_id", "source_dir", "page_no", "source_file", "src_bytes", "src_sha256",
                 "w_px", "h_px", "dpi", "colorspace", "embed", "orientation",
                 "n_pages", "pdf_path", "pdf_sha256", "pdf_bytes", "status", "guard_status",
                 "name_signal", "n_pdf_in_dir", "replaced_pdf_sha256"]


def write_manifest(path: Path, rows: list) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(MANIFEST_COLS)
        for r in rows:
            wr.writerow(r)
    os.chmod(path, 0o600)


def write_alerts(path: Path, alerts: list) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(alerts, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)


def write_guard(path: Path, records: list) -> None:
    """每个目录的守卫**明细**（含 sampled_pages / 逐页读到的值掩码 / distinct）。

    只写 status=ok 是不够的 —— 「守卫过了」必须能查出它到底读了哪几页、读到什么值。
    姓名按 PII 红线打码（首字 + *），值留 sha8 供对账。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)


def plan_targets(src_root: Path, dirs: list, out_root: Path, sidecar: bool,
                 tree_out: bool) -> list:
    """(cid, src_dir, out_pdf|None, dup) —— 撞名防线必须在**串行**阶段算完。

    并行只碰「一个目录 → 一个 PDF」这一步；「两个目录抢同一个 PDF」的判定不能并行，
    否则谁先占上取决于调度顺序，产物就不可复现（round42 的 blocked_duplicate_target）。
    dup = (key, 已占用它的源目录)；out_pdf=None 表示这个目录被禁止合并。
    """
    plan, seen = [], {}
    for d in dirs:
        if tree_out:
            cid, out_pdf = tree_target(src_root, d, out_root)
        else:
            cid = case_id_for(src_root, d)
            out_pdf = d / ("%s.pdf" % Path(d).name) if sidecar else out_root / ("%s.pdf" % cid)
        key = str(out_pdf)
        prev = seen.get(key)
        if prev is not None and prev != str(d):
            plan.append((cid, d, None, (key, prev)))
        else:
            seen[key] = str(d)
            plan.append((cid, d, out_pdf, None))
    return plan


def merge_one(task: tuple) -> dict:
    """进程池入口 —— 必须模块级才可 pickle。

    task = (src_dir, out_pdf, cfg, work_dir, guard, case_id, name_guard)
    """
    src_dir, out_pdf, cfg, work_dir, guard, cid, name_guard = task
    return merge_image_dir(Path(src_dir), Path(out_pdf), cfg, Path(work_dir),
                           guard=guard, case_id=cid, name_guard=name_guard)


def merge_tree(src_root: Path, out_root: Path, cfg: dict, guard: bool = True,
               sidecar: bool = False, work_dir: Path | None = None,
               tree_out: bool = False, progress=None, workers: int = 1,
               name_guard: bool = True, limit: int = 0) -> dict:
    """src_root 下每个含图片的目录 → 一个 PDF。

    sidecar=False：PDF 落 out_root/<case_id>.pdf（跑批用，run/wide/<batch>/00_input/）。
    sidecar=True ：PDF 落在图片目录自己身上（ingest 用，让 normalize_target 的三层规则成立）。
    tree_out=True ：PDF 落 out_root/<院区>/<病例>/<病例>.pdf（网页暂存区用 ——
                    源在 raid5 这类大盘上时，就地归一成深度 3 的只读源根，
                    既不复制图片、也不写回源目录）。
    progress      ：可选回调 progress(i, total, dir, row)，2449 目录级别的跑批要能看进度。
    """
    src_root = Path(src_root)
    out_root = Path(out_root)
    work_dir = Path(work_dir or (out_root / "_guard"))
    work_dir.mkdir(parents=True, exist_ok=True)
    dirs = find_image_dirs(src_root)
    if limit:
        # ★ 必须在 find_image_dirs 之后截：调用方自己截一份目录列表是没用的，
        #   merge_tree 会重新扫全量（实测踩过 —— --limit 200 照样跑完 2449 个目录）。
        dirs = dirs[:int(limit)]
    rows, alerts, summary = [], [], {"src_dirs": len(dirs), "merged": 0, "skipped": 0,
                                     "blocked": 0, "pages": 0, "no_image_dirs": 0,
                                     # ★ 零成本文件名守卫的逐目录判定必须落账。
                                     #   none = 这条查不了（文件名里没有数字前缀），不是「查过且干净」。
                                     #   不记下来 ⇒ summary 只报 alerts=0，读的人以为守卫过了。
                                     "name_signal_hist": {}}
    guard_records = []
    total = len(dirs)
    plan = plan_targets(src_root, dirs, out_root, sidecar, tree_out)
    tasks = [(d, p, cfg, work_dir / cid, guard, cid)
             for (cid, d, p, _dup) in plan if p is not None]
    tasks = [t + (name_guard,) for t in tasks]
    for _cid, _d, _p, _dup in plan:
        if _p is not None:
            _p.parent.mkdir(parents=True, exist_ok=True)

    # 合并是 IO + 字节拼接，单线程时 2449 目录要几小时 —— workers>1 走进程池。
    # ★ 结果按 src_dir 回填，manifest / alerts 仍按 dirs 原顺序产出 ⇒ 产物可复现。
    results: dict[str, dict] = {}

    def _bump(n: int, d: Path, r: dict) -> None:
        if progress:
            try:
                progress(n, total, d, r)
            except Exception:
                pass                    # 进度写盘失败不能影响合并本身

    if workers and workers > 1 and len(tasks) > 1:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=int(workers)) as ex:
            futs = {ex.submit(merge_one, t): t for t in tasks}
            for n, fut in enumerate(as_completed(futs), 1):
                t = futs[fut]
                try:
                    r = fut.result()
                except Exception as exc:      # 单个目录崩不能带走整批
                    r = {"case_id": t[5], "source_dir": str(t[0]), "n_images": 0,
                         "status": "blocked_worker_error", "guard_status": "",
                         "pdf_path": str(t[1]), "pdf_sha256": "", "pdf_bytes": 0,
                         "pages": [], "rejected": {"error": "%s: %s" % (type(exc).__name__, exc)}}
                results[str(t[0])] = r
                _bump(n, Path(t[0]), r)
    else:
        for n, t in enumerate(tasks, 1):
            r = merge_one(t)
            results[str(t[0])] = r
            _bump(n, Path(t[0]), r)

    for cid, d, out_pdf, dup in plan:
        if out_pdf is None:
            key, prev = dup
            # ★ 两个不同的散页目录映射到同一个 PDF ⇒ 后一份会**静默覆盖**前一份
            #   （= 凭空造一份不存在的病历）。禁止合并，报警 + 进人工确认清单。
            r = {"case_id": cid, "source_dir": str(d), "status": "blocked_duplicate_target",
                 "guard_status": "", "pdf_path": key,
                 "rejected": {"target": key, "other_source_dir": prev}}
        else:
            r = results[str(d)]
        ns = r.get("name_signal") or "not_run"
        summary["name_signal_hist"][ns] = summary["name_signal_hist"].get(ns, 0) + 1
        g = r.get("guard") or {}
        if g:
            guard_records.append({"case_id": cid, "source_dir": str(d),
                                  "status": g.get("status"),
                                  "merge_blocked": g.get("merge_blocked"),
                                  "sampled_pages": g.get("sampled_pages"),
                                  "distinct": g.get("distinct"),
                                  "conflicts": g.get("conflicts"),
                                  "unreadable_fields": g.get("unreadable_fields"),
                                  "n_ocr_errors": g.get("n_ocr_errors"),
                                  "bad_geometry_pages": g.get("bad_geometry_pages"),
                                  "per_page": g.get("per_page")})
        if r["status"] == "merged":
            summary["merged"] += 1
            summary["pages"] += r["n_images"]
        elif r["status"] == "skipped_identical":
            summary["skipped"] += 1
            summary["pages"] += r["n_images"]
        elif r["status"].startswith("blocked") or r["status"].startswith("rejected"):
            summary["blocked"] += 1
            alerts.append({"case_id": cid, "source_dir": str(d),
                           "kind": {"blocked_multi_patient": "multi_patient",
                                    "blocked_guard_error": "guard_error",
                                    "rejected_unreadable_image": "unreadable_image",
                                    "blocked_duplicate_target": "duplicate_target",
                                    "blocked_mixed_name_prefix": "mixed_name_prefix",
                                    "blocked_preexisting_pdf": "preexisting_pdf"
                                    }.get(r["status"], r["status"]),
                           "detail": r.get("rejected") or g.get("conflicts"),
                           "guard_status": r.get("guard_status", ""),
                           "guard": {k: g.get(k) for k in
                                     ("sampled_pages", "distinct", "conflicts",
                                      "hard_conflicts", "suspect_conflicts", "id_variants",
                                      "noise_ids", "unreadable_fields", "per_page")} if g else None})
            continue
        else:
            summary["no_image_dirs"] += 1
            continue
        if g.get("status") in ("guard_unreadable", "guard_error", "multi_patient_suspect"):
            alerts.append({"case_id": cid, "source_dir": str(d), "kind": g["status"],
                           "detail": (g.get("suspect_conflicts")
                                      if g["status"] == "multi_patient_suspect" else
                                      {"unreadable_fields": g.get("unreadable_fields"),
                                       "n_ocr_errors": g.get("n_ocr_errors")}),
                           "guard_status": g["status"], "merge_blocked": False,
                           "guard": {k: g.get(k) for k in
                                     ("sampled_pages", "distinct", "suspect_conflicts",
                                      "id_variants", "noise_ids", "unreadable_fields",
                                      "per_page")}})
        for pg in r.get("pages", []):
            rows.append([cid, str(d), pg["page_no"], pg["source_file"], pg["bytes"],
                         pg["sha256"], pg["w"], pg["h"], ";".join(map(str, pg["dpi"])),
                         pg["colorspace"], pg["embed"], pg["orientation"],
                         r["n_images"], r["pdf_path"], r["pdf_sha256"], r["pdf_bytes"],
                         r["status"], r.get("guard_status", ""), r.get("name_signal", ""),
                         r.get("n_pdf_in_dir", ""), r.get("replaced_pdf_sha256", "")])
    return {"rows": rows, "alerts": alerts, "summary": summary, "dirs": dirs,
            "guard_records": guard_records}


# --------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="散页图片目录 → 病例 PDF（无损嵌入）+ 多患者守卫")
    ap.add_argument("--src-root", required=True, help="含散页图片目录的源根（只读）")
    ap.add_argument("--out-root", required=True, help="PDF 落点（run/wide/<batch>/00_input）")
    ap.add_argument("--no-guard", action="store_true",
                    help="只用于对照；正式跑批不许关（守卫是 NO-GO 闸门）")
    ap.add_argument("--sidecar", action="store_true", help="PDF 写进图片目录自己（ingest 用）")
    ap.add_argument("--workers", type=int, default=1,
                    help="并行合并进程数（1=串行；合并是 IO+字节拼接，8~16 常见 5~10 倍）")
    ap.add_argument("--no-name-guard", action="store_true",
                    help="关掉零成本的文件名数字前缀守卫（只用于对照）")
    a = ap.parse_args()
    cfg = C.load_pipeline()
    res = merge_tree(Path(a.src_root), Path(a.out_root), cfg, guard=not a.no_guard,
                     sidecar=a.sidecar, workers=a.workers,
                     name_guard=not a.no_name_guard)
    write_manifest(Path(a.out_root) / "_merge_manifest.csv", res["rows"])
    write_alerts(Path(a.out_root) / "_merge_alerts.json", res["alerts"])
    write_guard(Path(a.out_root) / "_merge_guard.json", res["guard_records"])
    s = res["summary"]
    print("src_dirs=%d merged=%d skipped=%d blocked=%d pages=%d skipped_no_image=%d"
          % (s["src_dirs"], s["merged"], s["skipped"], s["blocked"], s["pages"],
             s["no_image_dirs"]))
    print("alerts=%d -> %s" % (len(res["alerts"]), Path(a.out_root) / "_merge_alerts.json"))
    for al in res["alerts"]:
        print("  ALERT %s kind=%s blocked=%s" % (mask_case_id(al["case_id"]), al["kind"],
                                                 al.get("merge_blocked", True)))
    return 0 if s["blocked"] == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
