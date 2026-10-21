"""启动前守卫 G1–G6（round37 §五）。每次提交都跑，失败就拒绝，不许静默继续。"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import zipfile
from pathlib import Path

import pii

# 产物写根（round42c）：指到 raid5。`/` 只剩几十 GB，38 GB 级语料的产物放不下；
# raid5 是今后的常用目录。旧批次**不搬不删**，留在 LEGACY_RUN_ROOT 里只读可见。
RUN_ROOT = Path(os.environ.get("MEDREC_WEBUI_RUN_ROOT") or "/raid5/medrec-run/wide")
LEGACY_RUN_ROOT = Path(os.environ.get("MEDREC_WEBUI_RUN_ROOT_LEGACY")
                       or "/home/ubuntu/medrec/run/wide")
RUN_ROOTS = [p for p in (RUN_ROOT, LEGACY_RUN_ROOT) if str(p)]


def ensure_run_root() -> Path:
    """写根不存在就建，并强制 700（产物里全是患者影像与 PII，红线⑨）。"""
    try:
        RUN_ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(RUN_ROOT, 0o700)
    except OSError:
        pass
    return RUN_ROOT

STEPS_ALL = ["locate", "s1", "s2", "s3", "s4", "s5", "s6"]
OCR_URL = "http://127.0.0.1:8110/v1/models"
LLM_URL = "http://127.0.0.1:8080/v1/models"

# G6：每个阶段的上游哨兵（缺了 run_steps.wait_done_marker() 会无条件轮询 900 s/例）
UPSTREAM = {
    "s1": ("00_index/manifest.tsv", "file", "locate"),
    "s2": ("01_ocr", "done", "s1"),
    "s3": ("02_text", "done", "s2"),
    "s4": ("03_extract", "done", "s3"),
    "s5": ("04_norm", "done", "s4"),
    "s6": ("05_qc", "qc", "s5"),
}

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_ok(url: str, timeout: float = 5.0) -> dict:
    try:
        with _NO_PROXY.open(url, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            return {"ok": resp.status == 200, "status": resp.status,
                    "models": _models(body), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "models": [], "error": f"{type(exc).__name__}: {exc}"}


def _models(body: str) -> list[str]:
    try:
        data = json.loads(body)
        return [m.get("id", "?") for m in data.get("data", [])]
    except Exception:  # noqa: BLE001
        return []


def services() -> dict:
    return {"ocr_8110": http_ok(OCR_URL), "llm_8080": http_ok(LLM_URL)}


# ---------------------------------------------------------------- G1 源目录形状
SHAPE_MAX_FILES = 200000
SHAPE_MAX_SECONDS = 20.0


def shape(src: Path, max_examples: int = 6, max_files: int = SHAPE_MAX_FILES,
          max_seconds: float = SHAPE_MAX_SECONDS, require_pdf: bool = True) -> dict:
    """产线认的形状 = <源根>/<院区>/<病例目录>/<文件>.pdf（s1_ocr.discover 深度 3）。

    require_pdf=False（round42d）：调用方自己会处理「只有散页图片」的源根
    （网页「本机路径」预检）⇒ 「一个 .pdf 都没有」降级成 warning，不当硬错误。
    深度 3 的硬校验只对**真的有 PDF** 的源根生效 —— 散页语料的层深天生不一
    （实测 /raid5/history_13_15：同一根下既有 <院区>/<病例>/*.jpg 也有
    <病例>/<病例>/*.jpg），拿 PDF 的深度规则去卡它必然误报。
    """
    out = {"ok": False, "src": str(src), "pdfs": 0, "cases": 0, "hospitals": [],
           "depth_hist": {}, "examples": [], "errors": [], "warnings": [],
           "total_bytes": 0, "scanned_files": 0, "truncated": False}
    if not src.exists():
        out["errors"].append(f"路径不存在：{src}")
        return out
    if not src.is_dir():
        out["errors"].append(f"不是目录：{src}")
        return out
    rows = []
    deadline = time.monotonic() + max_seconds
    scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        scanned += len(filenames)
        if scanned > max_files or time.monotonic() > deadline:
            truncated = True
            break
        for fn in filenames:
            if not fn.lower().endswith(".pdf"):
                continue
            rel = Path(dirpath, fn).relative_to(src)
            try:
                size = Path(dirpath, fn).stat().st_size      # 文件字节数（G2 空间守卫用它）
            except OSError:
                size = 0
            rows.append((len(rel.parts), rel, size))
    out["scanned_files"] = scanned
    out["truncated"] = truncated
    if truncated:
        out["errors"].append(
            f"★ 源根太大，扫描在 {scanned} 个文件 / {max_seconds:.0f} s 内没走完 ⇒ 不下结论。"
            f"请把源根选深一层（选到「装着院区目录的那一层」，而不是整个磁盘/挂载点）。"
            f"上限：max_files={max_files}、max_seconds={max_seconds:.0f}")
        return out
    out["pdfs"] = len(rows)
    if not rows:
        msg = f"{src} 下没有找到任何 .pdf"
        (out["errors"] if require_pdf else out["warnings"]).append(msg)
        return out
    for depth, _rel, size in rows:
        key = str(depth)
        out["depth_hist"][key] = out["depth_hist"].get(key, 0) + 1
        out["total_bytes"] += size
    bad = [r for r in rows if r[0] != 3]
    if bad:
        out["errors"].append(
            "源根形状不对：产线认 <源根>/<院区>/<病例目录>/<文件>.pdf（深度 3），"
            f"实测深度分布 {out['depth_hist']}。例：" +
            "; ".join(pii.scrub_text(str(r[1])) for r in bad[:max_examples]))
        if all(r[0] <= 2 for r in rows):
            out["errors"].append(
                "★ 你选的很可能是**叶子目录**（病例目录本身）。round33 实测：叶子目录当源根 ⇒ "
                "case_id 退化成 '.'，多例撞名、产物互相覆盖。正确给法：往上选到**装着院区目录的那一层**，"
                "例如 /home/ubuntu/data/测试（下面是 安吉/、奉化/…，再下面是病例目录，再下面是 pdf）。")
        if rows and all(r[0] >= 4 for r in rows):
            # 层深比产线口径**深**：多半是解压多出来的外层（<zip名>/<日期>/…）。
            # 光说「形状不对」用户没法修 ⇒ 直接算出该往下选几层、给一条具体路径。
            down = min(r[0] for r in rows) - 3
            prefix: list[str] = []
            for segs in zip(*[r[1].parts[:-1] for r in rows if len(r[1].parts) >= 2]):
                if len(set(segs)) == 1:
                    prefix.append(segs[0])
                else:
                    break
            deeper = str(src) + "".join("/" + s for s in prefix[:down])
            out["errors"].append(
                f"★ PDF 全在深度 ≥ 4 ⇒ 你选的源根比产线口径**浅了 {down} 层**"
                "（通常是解压多出来的外层，如 <zip名>/、<日期>/）。"
                f"把源根往下选 {down} 层再校验，例如：{pii.scrub_text(deeper)}。"
                "（散页图片不受这条限制 —— 只有图片时走「暂存归一」，它按层深不限的规则重排成深度 3。）")
        return out
    out["cases"] = len({str(r[1].parent) for r in rows})
    out["hospitals"] = sorted({r[1].parts[0] for r in rows})
    out["ok"] = True
    return out


# ---------------------------------------------------------------- G4 目录名含姓名
NAME_IN_DIR = re.compile(r"^\d{5,8}-[\u4e00-\u9fff]{2,4}-20[0-9]{6}$")


def name_in_dirname(src: Path, limit: int = 8) -> list[str]:
    hits = []
    for dirpath, dirnames, _ in os.walk(src):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for d in dirnames:
            if NAME_IN_DIR.match(d):
                hits.append(pii.scrub_text(Path(dirpath, d).relative_to(src).as_posix()))
    return sorted(set(hits))[:limit]


# ---------------------------------------------------------------- G2 可写 + 空间
def writable_space(total_bytes: int, at: "Path | None" = None) -> dict:
    at = Path(at) if at else ensure_run_root()
    out = {"ok": True, "errors": [], "free_bytes": 0, "need_bytes": total_bytes * 3,
           "at": str(at)}
    probe = at if at.exists() else (at.parent if at.parent.exists() else ensure_run_root())
    if not os.access(probe, os.W_OK | os.X_OK):
        out["errors"].append(f"不可写：{probe}")
        out["ok"] = False
    st = os.statvfs(probe)
    free = st.f_bavail * st.f_frsize
    out["free_bytes"] = free
    if free < total_bytes * 3:
        out["errors"].append(
            f"{probe} 剩余空间 {free / 1e9:.2f} GB < 输入 {total_bytes / 1e9:.2f} GB × 3 = "
            f"{total_bytes * 3 / 1e9:.2f} GB（G2）")
        out["ok"] = False
    return out


# ---------------------------------------------------------------- G5 压缩包三检
ZIP_PROFILES = {
    "upload": {"max_bytes": 2 * 1024 ** 3, "max_entries": 5000},
    "local": {"max_bytes": 200 * 1024 ** 3, "max_entries": 500000},
}
ZIP_MAX_BYTES = ZIP_PROFILES["upload"]["max_bytes"]
ZIP_MAX_ENTRIES = ZIP_PROFILES["upload"]["max_entries"]


def zip_checks(path: Path, profile: str = "upload") -> dict:
    prof = ZIP_PROFILES.get(profile, ZIP_PROFILES["upload"])
    out = {"ok": False, "errors": [], "entries": 0, "uncompressed_bytes": 0,
           "profile": profile}
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        out["errors"].append(f"打不开压缩包：{type(exc).__name__}: {exc}")
        return out
    infos = zf.infolist()
    out["entries"] = len(infos)
    out["uncompressed_bytes"] = sum(i.file_size for i in infos)
    slips = [i.filename for i in infos
             if i.filename.startswith("/") or ".." in Path(i.filename).parts]
    if slips:
        out["errors"].append(f"① zip slip：条目含 .. 或绝对路径 ⇒ 拒绝。例：{slips[:5]}")
    if out["uncompressed_bytes"] > prof["max_bytes"]:
        out["errors"].append(
            f"② 解压后总字节 {out['uncompressed_bytes'] / 1e9:.2f} GB > "
            f"{prof['max_bytes'] / 1024 ** 3:.0f} GiB（{profile} 档）⇒ 拒绝")
    if out["entries"] > prof["max_entries"]:
        out["errors"].append(
            f"③ 条目数 {out['entries']} > {prof['max_entries']}（{profile} 档）⇒ 拒绝")
    links = [i.filename for i in infos
             if (i.external_attr >> 16) & 0o170000 == 0o120000]
    if links:
        out["errors"].append(f"① 附加：压缩包里是符号链接 ⇒ 拒绝。例：{links[:5]}")
    out["ok"] = not out["errors"]
    zf.close()
    return out


# ---------------------------------------------------------------- G6 --steps 上游守卫
def steps_upstream(batch_dir: Path, steps: list[str]) -> dict:
    """G6：只跑指定阶段前，必须能确定「上游产物从哪来」。

    两种合法来源：① 上游阶段本次一起跑；② 批次目录里已有上游产物（哨兵）。
    两者都没有 ⇒ 拒绝 —— run_steps.wait_done_marker() 会对每例无条件轮询 900 s
    （round34 实测：106 例 = 106×900 s 卡死）。
    """
    out = {"ok": True, "errors": [], "checked": []}
    unknown = [st for st in steps if st not in STEPS_ALL]
    if unknown:
        out["errors"].append(f"未知阶段：{unknown}；可选 = {','.join(STEPS_ALL)}")
        out["ok"] = False
    chosen = set(steps)
    manifest = batch_dir / "00_index" / "manifest.tsv"
    # 上游不在本次选择里的阶段 ⇒ 需要批次目录里已有清单 + 已有上游产物
    need_existing = [st for st in steps
                     if st in UPSTREAM and UPSTREAM[st][2] not in chosen]
    if need_existing and not manifest.is_file():
        out["errors"].append(
            f"选了 {','.join(need_existing)}，但它们的上游不在本次选择里，而 {manifest} 不存在"
            f" ⇒ 既没有 case 清单、也没有上游产物可核对。新批次必须从 locate 起跑"
            "（或把上游阶段一起勾上）。")
        out["ok"] = False
        return out
    cases = manifest_cases(manifest)
    for st in steps:
        rel, kind, up = UPSTREAM.get(st, (None, None, None))
        if not rel:
            continue
        if up in chosen:
            out["checked"].append(f"{st}<-{up}: 本次一起跑（先跑 {up}）")
            continue
        pth = batch_dir / rel
        if kind == "file":
            if not pth.is_file():
                out["errors"].append(f"选了 {st} 但缺上游 {up} 的产物 {pth}")
            continue
        missing = [c for c in cases if not _case_ready(pth / c, kind)]
        out["checked"].append(f"{st}<-{up}: {len(cases) - len(missing)}/{len(cases)}")
        if missing:
            out["errors"].append(
                f"选了 {st} 但上游 {up} 对 {len(missing)}/{len(cases)} 例没有产物"
                f"（哨兵 {pth}/<case>/{'.done' if kind == 'done' else 'qc.json'}）。"
                f"缺的例（打码后）：{pii.scrub_text(','.join(missing[:5]))}。"
                "不补上游就选后段 = run_steps.wait_done_marker() 每例白等 900 s（round34 实测卡死）。")
    out["ok"] = not out["errors"]
    return out


def _case_ready(d: Path, kind: str) -> bool:
    if kind == "done":
        return (d / ".done").is_file()
    return (d / "qc.json").is_file()


def manifest_cases(manifest: Path) -> list[str]:
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if not lines:
        return []
    head = lines[0].split("\t")
    try:
        idx = head.index("case_id")
    except ValueError:
        return []
    out = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        parts = ln.split("\t")
        if len(parts) > idx:
            out.append(parts[idx])
    return out
