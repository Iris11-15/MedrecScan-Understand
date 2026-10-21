"""三种输入的接收与归一（round37 §六）。

统一原则：网页只把输入变成产线认得的**源根目录** <源根>/<院区>/<病例>/<文件>.pdf，
不解析 PDF、不 OCR、不调模型、不抽字段。

落点：run/wide/_ingest/.tmp/<upload_id>/…（暂存）→ 起跑时 os.replace 到 run/wide/_ingest/<batch>/。
人给的本机路径一律只读引用，不复制、不改名（§6.4）。
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import subprocess
import shutil
import zipfile
from pathlib import Path

import guards
import pii

INGEST_ROOT = Path("/home/ubuntu/medrec/run/wide/_ingest")
TMP_ROOT = INGEST_ROOT / ".tmp"
UNPACK_ROOT = TMP_ROOT / "_unpack"

MAX_FILE_BYTES = 2 * 1024 ** 3          # 单次上传上限 2 GiB（§6.2）
CHUNK = 1 << 16
BAD_PARTS = {"", ".", "..", "__MACOSX"}
ZIP_DIR = "_zips"                      # 上传的 .zip 先平铺放这里，解压后再归一

IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}   # 与归一模块白名单一致

# 预检是同步 HTTP 请求 ⇒ 扫描必须有上限（跑批侧走不限上限的 find_image_dirs）
INSPECT_MAX_IMG_DIRS = 20000
INSPECT_MAX_IMG_SECONDS = 60.0
INSPECT_MAX_PREVIEW = 6

# 守卫成本（每散页目录抽 首页/中间/末页 各 1 次 OCR）：round42 实测 10–15 s/目录，取 12 s。
# 只给页面一个量级，不是承诺；真实进度以 .stage_status.json 为准。
# 每散页目录 = 合并 + 守卫 3 页 OCR。旧值 12 s 是拍脑袋；round42i 实测
# stage-20261016-191212：11.4 min / 85 目录 = 8.08 s/目录（单 worker、8110 独占）。
GUARD_SEC_PER_DIR = 8.1
# 关 OCR 守卫时的成本（只读图 + 拼 PDF）。round42l 实测 /raid5/history_13_15：
# 2449 目录 / 142317 页 / 358.2 s @8 workers ⇒ 1.17 s/目录（折回串行口径）。
FAST_SEC_PER_DIR = 1.2
# 合并并行进程数（round42l）。守卫是每目录 3 页 OCR，单进程时 2449 目录要 5.2 h；
# 合并本身（读图 + 拼 PDF）实测 2449 目录 / 8 进程 = 358 s。
# ★ 并行只加速，不改变产物：同一批目录开/关守卫出来的 PDF 逐字节相同（25/25 实测）。
MERGE_WORKERS = max(1, int(os.environ.get("MEDREC_MERGE_WORKERS") or 8))
# ★ 合并前要不要跑「多患者」OCR 守卫（每目录 首页/中间/末页 各 1 页）。
#   用户 2026-10-17 确认病例规律：**存放图片的文件夹里的全部图片 = 同一份病例**
#   ⇒ 默认不跑（它占了 5.2 h 里的绝大部分）。要恢复旧行为：
#     export MEDREC_MERGE_GUARD=on 后重启 8090。
#   零成本的文件名数字前缀守卫不受这个开关影响，始终开（merge_tree name_guard 默认 True）。
MERGE_GUARD = (os.environ.get("MEDREC_MERGE_GUARD") or "off").strip().lower() in ("1", "on", "true", "yes")


def sec_per_dir() -> float:
    """预估用的每目录成本：开守卫 8.1 s，关守卫 1.2 s。"""
    return GUARD_SEC_PER_DIR if MERGE_GUARD else FAST_SEC_PER_DIR
SRC_ROOT = Path("/home/ubuntu/medrec/src")
MERGE_FILES = {"_merge_manifest.csv", "_merge_alerts.json", "_merge_guard.json"}

# 本机路径输入（round42b）：允许浏览/引用的根，以及**暂存归一**的落点根。
# 暂存必须与源同盘：38 GB 级的解压 + 合并落 / 会当场撑爆（/ 只剩几十 GB）。
LOCAL_ROOTS = [Path(p) for p in
               (os.environ.get("MEDREC_LOCAL_ROOTS") or "/raid5:/home/ubuntu").split(":")
               if p.strip()]
STAGE_ROOTS = [Path(p) for p in
               (os.environ.get("MEDREC_STAGE_ROOTS") or "/raid5/.medrec_stage").split(":")
               if p.strip()]
STAGE_SUB = "_stage"                  # 没有同盘 STAGE_ROOTS 时的兜底暂存根
BROWSE_MAX = 2000                     # 实测院区层下最多 791 个病例目录 ⇒ 400 会截断
ZIP_PROFILE_LOCAL = "local"


def new_batch_name(prefix: str = "web") -> str:
    import time
    return time.strftime(prefix + "-%Y%m%d-%H%M%S")


def tmp_dir(upload_id: str) -> Path:
    return TMP_ROOT / _safe_id(upload_id)


def stage_candidates() -> list[Path]:
    return list(STAGE_ROOTS) + [TMP_ROOT / STAGE_SUB]


def find_stage_root(upload_id: str) -> Path | None:
    sid = _safe_id(upload_id)
    for r in stage_candidates():
        if (r / sid).is_dir():
            return r
    return None


def find_stage_dir(upload_id: str) -> Path | None:
    r = find_stage_root(upload_id)
    return (r / _safe_id(upload_id)) if r else None


def stage_tree_dir(stage: Path) -> Path:
    """暂存区里真正当源根用的那一层（_unpack / _guard / 状态文件都在它外面）。"""
    return Path(stage) / "tree"


def stage_status_path(stage: Path) -> Path:
    return Path(stage) / ".stage_status.json"


def chain_path(stage: Path) -> Path:
    """「归一完自动提取」的链式请求文件（round42k）。

    为什么落文件而不是放内存：归一是小时级，网页随时会关 / 刷新 / 换浏览器；
    链必须由 **8090 进程里的轮询线程**执行，不能由浏览器轮询后代跑（那样一关页面就断）。
    """
    return Path(stage) / "chain.json"


def read_chain(stage: Path) -> dict:
    f = chain_path(stage)
    if not f.is_file():
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def write_chain(stage: Path, ch: dict) -> None:
    ch["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = Path(str(chain_path(stage)) + ".tmp")
    tmp.write_text(json.dumps(ch, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, chain_path(stage))


def iter_stage_dirs() -> list[Path]:
    """所有暂存目录（链式轮询要扫全部暂存根，不能只看第一个）。"""
    out: list[Path] = []
    for root in stage_candidates():
        if not root.is_dir():
            continue
        try:
            out.extend([d for d in sorted(root.iterdir()) if d.is_dir()])
        except OSError:
            continue
    return out


def write_stage_status(stage: Path, st: dict) -> None:
    st["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = Path(str(stage_status_path(stage)) + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, stage_status_path(stage))


def read_stage_status(upload_id: str) -> dict:
    d = find_stage_dir(upload_id)
    if not d:
        return {"ok": False, "state": "none", "stage_id": str(upload_id),
                "errors": [f"没有暂存目录：{upload_id}（先点「暂存归一」）"]}
    f = stage_status_path(d)
    if not f.is_file():
        return {"ok": True, "state": "running", "stage_id": str(upload_id),
                "stage_dir": str(d), "tree": str(stage_tree_dir(d)), "errors": []}
    try:
        st = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "state": "error", "stage_dir": str(d),
                "errors": [f"读暂存状态失败：{type(exc).__name__}: {exc}"]}
    st.setdefault("errors", [])
    st["stage_dir"] = str(d)
    st["tree"] = str(stage_tree_dir(d))
    st["ok"] = st.get("state") != "error"
    return st


def local_ok(raw: str) -> tuple[Path | None, str]:
    """本机路径必须在 MEDREC_LOCAL_ROOTS 之下（resolve 后判定 ⇒ 防 symlink 逃逸）。"""
    s = str(raw or "").strip()
    if not s:
        return None, "路径为空"
    p = Path(s)
    if not p.is_absolute():
        return None, f"必须是绝对路径：{s!r}"
    try:
        rp = p.resolve()
    except OSError as exc:
        return None, f"路径解析失败：{type(exc).__name__}: {exc}"
    if not rp.exists():
        return None, f"路径不存在：{pii.scrub_text(str(rp))}"
    roots = []
    for r in LOCAL_ROOTS:
        try:
            roots.append(r.resolve())
        except OSError:
            continue
    if not roots:
        return None, "MEDREC_LOCAL_ROOTS 为空 ⇒ 本机路径输入整体禁用"
    if not any(rp == r or str(rp).startswith(str(r) + os.sep) for r in roots):
        return None, ("路径不在允许的本机根之下（只允许：" +
                      "、".join(str(r) for r in roots) + "）")
    return rp, ""


def stage_root_for(src: Path) -> Path:
    """选与源**同一文件系统**的暂存根；没有 ⇒ 回落 run/wide/_ingest/_stage。"""
    probe = src if src.is_dir() else src.parent
    while probe != probe.parent and not probe.exists():
        probe = probe.parent
    try:
        dev = os.stat(probe).st_dev
    except OSError:
        dev = None
    for r in stage_candidates():
        try:
            r.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(r, 0o700)      # 暂存根里是患者影像 ⇒ 根也必须 700（红线⑨）
            if dev is None or os.stat(r).st_dev == dev:
                return r
        except OSError:
            continue
    return TMP_ROOT / STAGE_SUB


def browse(raw: str) -> dict:
    """单层列目录 + .zip（不递归、不跟 symlink、上限 BROWSE_MAX）。"""
    target = str(raw or "").strip() or (str(LOCAL_ROOTS[0]) if LOCAL_ROOTS else "/")
    p, err = local_ok(target)
    out = {"ok": not bool(err), "path": "", "parent": "", "items": [], "truncated": False,
           "allowed_roots": [str(r) for r in LOCAL_ROOTS], "errors": [] if not err else [err]}
    if err:
        return out
    if not p.is_dir():
        out["errors"].append(f"不是目录：{pii.scrub_text(str(p))}")
        return out
    out["path"] = str(p)
    out["parent"] = str(p.parent) if str(p.parent) != str(p) else ""
    dirs, zips = [], []
    try:
        for e in os.scandir(p):
            try:
                if e.is_symlink():
                    continue
                if e.name.startswith("."):
                    continue
                if e.is_dir():
                    dirs.append((e.name, "dir", 0))
                elif e.is_file() and e.name.lower().endswith(".zip"):
                    zips.append((e.name, "zip", e.stat().st_size))
            except OSError:
                continue
    except OSError as exc:
        out["errors"].append(f"列目录失败：{type(exc).__name__}: {exc}")
        return out
    items = sorted(dirs) + sorted(zips)
    out["truncated"] = len(items) > BROWSE_MAX
    out["items"] = [{"name": pii.scrub_text(n), "kind": k, "bytes": b}
                    for n, k, b in items[:BROWSE_MAX]]
    return out


def zip_summary(path: Path, profile: str = ZIP_PROFILE_LOCAL) -> dict:
    """不解压，只读中央目录：条目/解压后字节/pdf 数/图片数/含图片的目录数/顶层名。"""
    out = {"ok": False, "errors": [], "entries": 0, "uncompressed_bytes": 0, "pdfs": 0,
           "images": 0, "image_dirs": 0, "top_levels": [], "needs_merge": False}
    chk = guards.zip_checks(path, profile=profile)
    out["errors"] += chk["errors"]
    out["entries"] = chk["entries"]
    out["uncompressed_bytes"] = chk["uncompressed_bytes"]
    try:
        with zipfile.ZipFile(path) as zf:
            img_dirs = set()
            tops = set()
            for i in zf.infolist():
                if i.is_dir():
                    continue
                parts = Path(i.filename).parts
                if not parts:
                    continue
                tops.add(parts[0])
                suf = Path(i.filename).suffix.lower()
                if suf == ".pdf":
                    out["pdfs"] += 1
                elif suf in IMG_EXTS:
                    out["images"] += 1
                    img_dirs.add(str(Path(*parts[:-1])) if len(parts) > 1 else ".")
    except (OSError, zipfile.BadZipFile) as exc:
        out["errors"].append(f"读中央目录失败：{type(exc).__name__}: {exc}")
        return out
    out["image_dirs"] = len(img_dirs)
    out["top_levels"] = sorted(pii.scrub_text(t) for t in tops)[:12]
    out["needs_merge"] = (out["pdfs"] == 0 and out["images"] > 0)
    out["est_minutes"] = int(-(-out["image_dirs"] * sec_per_dir() // 60 // MERGE_WORKERS))
    out["merge_workers"] = MERGE_WORKERS
    out["merge_guard"] = MERGE_GUARD
    out["ok"] = not out["errors"]
    return out


def scan_images(root: Path, max_dirs: int = INSPECT_MAX_IMG_DIRS,
                max_seconds: float = INSPECT_MAX_IMG_SECONDS,
                max_preview: int = INSPECT_MAX_PREVIEW) -> dict:
    """扫源根下的散页图片目录（**层深不限**）：计数 / 字节 / 归一后的 <院区>/<病例> 预览。

    ★ round42h：预检和校验必须用**同一套**扫描口径。之前只有 inspect_local 走这里，
      plan() 仍按 PDF 口径 ⇒ 同一个 /raid5/history_13_15 预检 ok（2449 个散页目录）、
      校验报「下没有找到任何 .pdf」，两条自相矛盾，页面只剩一条看不懂的红灯。
    """
    out = {"image_dirs": 0, "images": 0, "image_bytes": 0, "img_truncated": False,
           "image_depth_hist": {}, "hospitals_from_images": [], "tree_preview": [],
           "notes": [], "errors": [], "est_minutes": 0}
    IN = None
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    try:
        from wide import input_normalize as IN
    except Exception as exc:
        out["errors"].append(f"加载归一模块失败：{type(exc).__name__}: {exc}")
        return out
    try:
        dirs, img_trunc = IN.scan_image_dirs(root, max_dirs=max_dirs, max_seconds=max_seconds)
    except Exception as exc:
        out["errors"].append(f"扫图片目录失败：{type(exc).__name__}: {exc}")
        return out
    out["image_dirs"] = len(dirs)
    out["img_truncated"] = img_trunc
    out["est_minutes"] = int(-(-len(dirs) * sec_per_dir() // 60 // MERGE_WORKERS))
    out["merge_workers"] = MERGE_WORKERS
    out["merge_guard"] = MERGE_GUARD
    hist: dict[str, int] = {}
    hs_seen: dict[str, int] = {}
    notes: set[str] = set()
    for d in dirs:
        try:
            rel = d.relative_to(root)
        except ValueError:
            rel = Path(d.name)
        key = str(len(rel.parts))
        hist[key] = hist.get(key, 0) + 1
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in IMG_EXTS:
                out["images"] += 1
                try:
                    out["image_bytes"] += f.stat().st_size
                except OSError:
                    pass
        try:
            hs, cs = IN.case_hospital_from_rel(rel)
            if hs == "unknown":
                # rel 里翻不出院区层 ⇒ 走 tree_layers 的补层（与 discover 同口径），
                # 并把「院区层是从你选的这一层目录名补的」如实带回页面
                hs, cs, note = IN.tree_layers(root, d)
                if note:
                    notes.add(note)
        except Exception:
            continue
        hs, cs = IN.safe_part(hs), IN.safe_part(cs)
        hs_seen[hs] = hs_seen.get(hs, 0) + 1
        if len(out["tree_preview"]) < max_preview:
            out["tree_preview"].append(pii.scrub_text("%s/%s/%s.pdf" % (hs, cs, cs)))
    out["notes"] = sorted(notes)
    out["image_depth_hist"] = hist
    out["hospitals_from_images"] = [
        {"hospital": pii.scrub_text(h), "cases": n}
        for h, n in sorted(hs_seen.items(), key=lambda kv: (-kv[1], kv[0]))[:12]]
    return out


def find_done_stage(src: Path) -> dict:
    """同一源根**已归一完成**的暂存树（页面刷新 / 换浏览器后 stage_id 丢了时用来指路）。

    合并 + 守卫 OCR 是小时级（2449 目录 ≈ 6.5 h）⇒ 再点一次「暂存归一」重跑一遍
    是不可接受的浪费，所以这里按 realpath 精确匹配源根，命中就复用。
    """
    try:
        want = os.path.realpath(str(src))
    except OSError:
        return {}
    for root in stage_candidates():
        if not root.is_dir():
            continue
        try:
            kids = sorted(root.iterdir())
        except OSError:
            continue
        for d in kids:
            if not d.is_dir():
                continue
            try:
                st = read_stage_status(d.name)
            except ValueError:
                continue
            if st.get("state") not in ("done", "done_with_alerts"):
                continue
            if os.path.realpath(str(st.get("src") or "")) != want:
                continue
            tree = stage_tree_dir(d)
            if tree.is_dir():
                return {"stage_id": d.name, "tree": str(tree), "state": st.get("state"),
                        "merged": st.get("merged", 0), "alerts": st.get("alerts", 0)}
    return {}


def find_running_stage(src: Path) -> dict:
    """同一源根**正在归一中**的 worker（前端接上它，别再起第二个）。

    ★ round42i 实测：前端 api() 返回的是 {code, body} 包装，但 startStage 直接当 body 用
      ⇒ d.ok 永远 undefined ⇒ 后端明明起成功了，页面照样报「暂存归一被拒：未知」
      ⇒ 用户连点三次 ⇒ 三个 worker 打同一个语料、各写各的树（3× OCR + 3× 磁盘），
      而且每个都要跑完整 2449 目录 ⇒ 一点没快，纯浪费。
      所以起之前先按 realpath 匹配源根：有正在跑的 ⇒ 把它的 stage_id 还给前端。
    """
    try:
        want = os.path.realpath(str(src))
    except OSError:
        return {}
    for root in stage_candidates():
        if not root.is_dir():
            continue
        try:
            kids = sorted(root.iterdir())
        except OSError:
            continue
        for d in kids:
            if not d.is_dir():
                continue
            try:
                st = read_stage_status(d.name)
            except ValueError:
                continue
            if st.get("state") != "running":
                continue
            if os.path.realpath(str(st.get("src") or "")) != want:
                continue
            pid = int(st.get("pid") or 0)
            # 状态文件说 running 但进程早没了（断电/崩溃）⇒ 别把用户永久锁死
            if pid > 0 and not Path("/proc/%d" % pid).exists():
                continue
            return {"stage_id": d.name, "stage_dir": str(d), "state": "running",
                    "tree": str(stage_tree_dir(d)), "pid": pid,
                    "done_dirs": int(st.get("done_dirs") or 0),
                    "total_dirs": int(st.get("total_dirs") or 0),
                    "merged": int(st.get("merged") or 0),
                    "updated": st.get("updated") or ""}
    return {}


def inspect_local(raw: str) -> dict:
    """本机路径的预检：.zip 走中央目录汇总；目录走「PDF 形状 + 散页图片」两路。

    ★ round42d：目录**层深不限**。实测 /raid5/history_13_15 里两种形状并存
      （<院区>/<病例>/*.jpg 与 <病例>/<病例>/*.jpg），且一个 PDF 都没有 ⇒
      旧写法先调 guards.shape() 直接报「没有找到任何 .pdf」，页面在 !ok 处 return，
      「暂存归一」按钮永远点不动。现在：
        * 有 PDF      ⇒ 照旧走 G1 深度 3 硬校验（产线只认这个形状）
        * 只有图片    ⇒ needs_merge=true + 层深分布 + 归一后的 <院区>/<病例> 预览，不是错误
        * 两者都没有  ⇒ 才是真错误
    """
    p, err = local_ok(raw)
    if err:
        return {"ok": False, "errors": [err], "kind": "", "path": str(raw)}
    if p.is_file() and p.suffix.lower() == ".zip":
        z = zip_summary(p)
        z.update({"kind": "zip", "path": str(p), "src_bytes": p.stat().st_size})
        return z
    if not p.is_dir():
        return {"ok": False, "errors": [f"只支持 .zip 或目录：{pii.scrub_text(str(p))}"],
                "kind": "", "path": str(p)}
    shp = guards.shape(p, require_pdf=False)
    res = {"kind": "dir", "path": str(p), "ok": shp["ok"], "errors": list(shp["errors"]),
           "warnings": list(shp["warnings"]),
           "pdfs": shp["pdfs"], "cases": shp["cases"], "hospitals": shp["hospitals"],
           "depth_hist": shp["depth_hist"], "total_bytes": shp["total_bytes"],
           "truncated": shp["truncated"], "needs_merge": False, "image_dirs": 0,
           "images": 0, "image_bytes": 0, "img_truncated": False, "image_depth_hist": {},
           "hospitals_from_images": [], "tree_preview": []}
    im = scan_images(p)
    res["errors"] += im["errors"]
    res["image_dirs"] = im["image_dirs"]
    res["images"] = im["images"]
    res["image_bytes"] = im["image_bytes"]
    res["img_truncated"] = im["img_truncated"]
    res["est_minutes"] = im["est_minutes"]
    res["merge_workers"] = im.get("merge_workers")
    res["merge_guard"] = im.get("merge_guard")
    res["image_depth_hist"] = im["image_depth_hist"]
    res["hospitals_from_images"] = im["hospitals_from_images"]
    res["tree_preview"] = im["tree_preview"]
    if im["img_truncated"]:
        n_dir_n = im["image_dirs"]
        res["warnings"].append(
            f"★ 图片目录扫描在 {n_dir_n} 个目录 / {INSPECT_MAX_IMG_SECONDS:.0f} s 内没走完"
            f"⇒ 下面的计数只是**下限**（跑批时会扫全）。")
    for nt in im["notes"]:
        res["warnings"].append(pii.scrub_text("★ 层形提示：" + nt))
    if res["pdfs"] == 0 and res["image_dirs"] > 0:
        # 只有散页图片：这是「要先暂存归一」，不是错误 ⇒ 把形状类的报错降级成提示
        res["errors"] = []
        res["truncated"] = False
        res["needs_merge"] = True
        res["ok"] = True
    elif res["pdfs"] == 0 and res["image_dirs"] == 0 and not res["errors"]:
        res["errors"].append(
            f"{pii.scrub_text(str(p))} 下既没有 .pdf，也没有散页图片"
            f"（{' '.join(sorted(IMG_EXTS))}）⇒ 这一层不能当源根。")
        res["ok"] = False
    elif res["pdfs"] > 0 and res["image_dirs"] > 0:
        # 混着放：走 PDF 原路 = 散页图片被静默忽略 ⇒ 必须说破，不能让人以为都吃进去了
        res["warnings"].append(
            "★ 这个源根**同时**有 .pdf 和散页图片：走 PDF 原路时那 "
            f"{res['image_dirs']} 个散页目录会被**忽略**（暂存归一只重排图片目录，"
            "不会把 PDF 搬进暂存树）。要处理散页 ⇒ 单独选只含图片的那一层，归一后再跑。")
    return res


def final_dir(batch: str) -> Path:
    return INGEST_ROOT / _safe_id(batch)


def _safe_id(s: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", str(s or "").strip())
    if not s or s.startswith("."):
        raise ValueError(f"非法名字：{s!r}")
    return s


def safe_parts(relpath: str) -> list[str]:
    """把浏览器给的 relpath 切成安全的路径段；含 .. / 绝对路径 ⇒ 抛错。"""
    raw = str(relpath or "").replace("\\", "/").split("/")
    parts = [p.strip() for p in raw if p.strip() not in BAD_PARTS]
    for p in parts:
        if ".." in p or p.startswith("/") or any(ord(c) < 32 for c in p):
            raise ValueError(f"路径段不安全：{p!r}（完整 relpath={relpath!r}）")
    return parts


def normalize_target(relpath: str) -> tuple[str, str, str]:
    """归一成 (院区, 病例目录, 文件名)。

    规则（§6.3）：病例目录 = 文件的直接父目录；院区 = 病例目录的父目录；
    没有院区层 ⇒ 补 unknown；没有病例层（直接给 PDF）⇒ 病例目录 = 文件名主干。
    外层多出来的层（例如人选的文件夹本身）一律忽略 —— 只看「文件的祖辈」。
    """
    parts = safe_parts(relpath)
    if not parts:
        raise ValueError("空 relpath")
    name = parts[-1]
    if name.lower().endswith(".zip"):
        raise ValueError(f"压缩包条目不能直接当病例：{name!r}（.zip 走解压归一）")
    if len(parts) >= 3:
        return parts[-3], parts[-2], name
    if len(parts) == 2:
        return "unknown", parts[0], name
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    return "unknown", _safe_id(stem), name


# ------------------------------------------------------------------ multipart 流式接收
class SeekBody:
    """给 ingest 用的最小 read() 包装（只按 Content-Length 给字节）。"""

    def __init__(self, rfile, length: int):
        self.f, self.left = rfile, int(length)

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.left
        n = min(n, self.left)
        if n <= 0:
            return b""
        data = self.f.read(n)
        self.left -= len(data)
        return data

    def readline(self, limit: int = 4096) -> bytes:
        out = bytearray()
        while len(out) < limit:
            ch = self.read(1)
            if not ch:
                break
            out += ch
            if ch == b"\n":
                break
        return bytes(out)


class _Body:
    """按 Content-Length 定界的请求体读取器，带 pushback（boundary 之后的字节要还给流）。"""

    def __init__(self, rfile, length: int):
        self.f, self.left, self.push = rfile, int(length), bytearray()

    def unread(self, data: bytes) -> None:
        self.push += data

    def read(self, n: int) -> bytes:
        out = bytearray()
        if self.push and n > 0:
            out += self.push[:n]
            del self.push[:n]
            n -= len(out)
        n = min(n, self.left)
        if n > 0:
            data = self.f.read(n)
            if not data:
                raise ValueError(f"请求体比 Content-Length 短 {self.left} 字节（连接被切断）")
            self.left -= len(data)
            out += data
        return bytes(out)

    def readline(self, limit: int = 4096) -> bytes:
        out = bytearray()
        while len(out) < limit:
            ch = self.read(1)
            if not ch:
                break
            out += ch
            if ch == b"\n":
                break
        return bytes(out)


def _disposition(header_block: bytes) -> tuple[str, str]:
    text = header_block.decode("utf-8", "replace")
    name = re.search(r'name="([^"]*)"', text)
    fname = re.search(r'filename="([^"]*)"', text)
    return (name.group(1) if name else "", fname.group(1) if fname else "")


def receive_upload(rfile, content_length: int, content_type: str,
                   upload_id: str) -> dict:
    """流式写 run/wide/_ingest/.tmp/<uid>/，不把整个文件读进内存（§6.2）。"""
    out = {"ok": False, "upload_id": upload_id, "files": [], "fields": {},
           "errors": [], "total_bytes": 0}
    m = re.search(r"boundary=(.+)", content_type)
    if not m:
        out["errors"].append("Content-Type 里没有 multipart boundary")
        return out
    boundary = m.group(1).strip().strip('"').encode()
    body = _Body(rfile, content_length)
    dest = tmp_dir(upload_id)
    dest.mkdir(parents=True, exist_ok=True)
    delim = b"--" + boundary

    # 找到第一个 boundary（丢掉前置的 epilogue）
    while True:
        line = body.readline(8192)
        if not line:
            out["errors"].append("请求体里找不到 multipart 起始 boundary")
            return out
        if line.startswith(delim):
            break

    while True:
        headers = bytearray()
        while True:
            ln = body.readline(8192)
            if not ln or ln in (b"\r\n", b"\n"):
                break
            headers += ln
        name, filename = _disposition(bytes(headers))
        marker = b"\r\n" + delim
        buf, nbytes, part_path = b"", 0, None
        try:
            if filename:
                if filename.lower().endswith(".zip"):
                    target_dir = dest / ZIP_DIR
                    fname = Path(filename).name
                else:
                    hospital, case, fname = normalize_target(filename)
                    target_dir = dest / _safe_id(hospital) / _safe_id(case)
                target_dir.mkdir(parents=True, exist_ok=True)
                part_path = target_dir / _safe_id(fname)
            with open(part_path, "wb") if part_path else _Null() as fh:
                while True:
                    chunk = body.read(CHUNK)
                    if not chunk:
                        raise ValueError("请求体在 boundary 之前结束")
                    buf += chunk
                    idx = buf.find(marker)
                    if idx >= 0:
                        seg = buf[:idx]
                        fh.write(seg)
                        nbytes += len(seg)
                        body.unread(buf[idx + len(marker):])
                        buf = b""
                        break
                    keep = len(marker)
                    if len(buf) > keep:
                        seg, buf = buf[:-keep], buf[-keep:]
                        fh.write(seg)
                        nbytes += len(seg)
                    if nbytes > MAX_FILE_BYTES:
                        raise ValueError(f"单个文件超过 2 GiB 上限（{nbytes} 字节）")
        except ValueError as exc:
            out["errors"].append(str(exc))
            if part_path:
                part_path.unlink(missing_ok=True)
            return out
        if part_path:
            os.chmod(part_path, 0o600)
            out["files"].append({"field": name,
                                 "rel": pii.scrub_text(str(part_path.relative_to(dest))),
                                 "bytes": nbytes})
            out["total_bytes"] += nbytes
        else:
            out["fields"][name] = buf.decode("utf-8", "replace").strip()
        tail = body.read(2)
        if tail == b"--":
            break
        if tail not in (b"\r\n", b"\n"):
            out["errors"].append(f"multipart 结构异常（boundary 后是 {tail!r}）")
            return out
    out["ok"] = not out["errors"]
    return out


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write(self, _b):
        return 0


# ------------------------------------------------------------------ zip 解压 + 归一
def _load_input_normalize():
    """懒加载产线归一模块（webui 的 sys.path 里没有 src/）。

    失败返回 (None, 错误串) —— 不许静默吞。归一模块只在真要 OCR 时才 import s1_ocr，
    所以 webui 进程本身不需要 paddle。
    """
    try:
        if str(SRC_ROOT) not in sys.path:
            sys.path.insert(0, str(SRC_ROOT))
        from wide import common as C
        from wide import input_normalize as IN
        return (IN, C), ""
    except Exception as exc:
        return None, f"导入 wide.input_normalize 失败：{type(exc).__name__}: {exc}"


def merge_images_in_place(work: Path, out: dict) -> int:
    """zip 里没有 .pdf、只有散页图片 ⇒ 先无损合并成 PDF（含多患者守卫），再走原路。

    sidecar=True：PDF 写在**图片目录自己身上** ⇒ relpath 的祖辈就是 院区/病例，
    normalize_target 的三层规则原样成立，下面的移动循环不用改。
    守卫（首页/中间/末页 比对 住院号 + 姓名）不过 ⇒ 该目录禁止合并，报警写
    _merge_alerts.json 并把每条报警进 errors ⇒ 整批 ok=False，不许「先合了再说」。
    """
    loaded, err = _load_input_normalize()
    if loaded is None:
        out["errors"].append(err)
        return 0
    IN, C = loaded
    try:
        cfg = C.load_pipeline()
    except Exception as exc:
        out["errors"].append(f"读 config/pipeline.yaml 失败：{type(exc).__name__}: {exc}")
        return 0
    alerts_path = work / "_merge_alerts.json"
    manifest_path = work / "_merge_manifest.csv"
    try:
        res = IN.merge_tree(work, work, cfg, guard=MERGE_GUARD, sidecar=True,
                            work_dir=work / "_guard", workers=MERGE_WORKERS)
    except Exception as exc:
        out["errors"].append(f"散页图片合并失败：{type(exc).__name__}: {exc}")
        return 0
    IN.write_manifest(manifest_path, res["rows"])
    IN.write_alerts(alerts_path, res["alerts"])
    IN.write_guard(work / "_merge_guard.json", res["guard_records"])
    s = res["summary"]
    out["merge"] = {"src_dirs": s["src_dirs"], "merged": s["merged"],
                    "skipped_identical": s["skipped"], "blocked": s["blocked"],
                    "pages": s["pages"], "alerts": len(res["alerts"]),
                    "manifest": str(manifest_path), "alerts_path": str(alerts_path)}
    out["merge"]["guard"] = MERGE_GUARD
    out["merge"]["workers"] = MERGE_WORKERS
    for al in res["alerts"]:
        out["errors"].append(
            "散页守卫报警：%s kind=%s ⇒ %s（详见 %s）"
            % (pii.scrub_text(al["case_id"]), al["kind"],
               ("该目录禁止合并，需人工确认" if al.get("merge_blocked", True)
                else "已照常合并，但需人工复核这一例"),
               alerts_path))
    if s["merged"] + s["skipped"] == 0:
        out["errors"].append(
            "压缩包里没有任何 .pdf，也没能合并出 PDF（图片目录 %d 个，全部被拦）"
            % s["src_dirs"])
    return s["merged"] + s["skipped"]


def unpack_zip(zip_path: Path, upload_id: str) -> dict:
    """先过 G5 三检，再按目录/PDF 规则归一进 .tmp/<uid>/。

    round42：zip 里可以没有 .pdf —— 只有散页图片时先调归一模块合并（条款 B）。
    有 .pdf 走原路，行为不变。
    """
    out = {"ok": False, "errors": [], "moved": 0, "rule": "", "merge": None}
    chk = guards.zip_checks(zip_path)
    if not chk["ok"]:
        out["errors"] = chk["errors"]
        return out
    work = UNPACK_ROOT / _safe_id(upload_id)
    if work.exists():
        work = Path(str(work) + f"-{os.getpid()}")
    work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(work)
    pdfs = [p for p in work.rglob("*.pdf")]
    merged = 0
    if not pdfs:
        merged = merge_images_in_place(work, out)
        pdfs = [p for p in work.rglob("*.pdf")]
    if not pdfs:
        out["errors"].append("压缩包里没有任何 .pdf，也没有可合并的散页图片")
        return out
    depths = {len(p.relative_to(work).parts) for p in pdfs}
    deep = any(d >= 3 for d in depths)
    out["rule"] = "目录规则（有层级）" if deep else "PDF 规则（每个 PDF 一个病例目录）"
    dest = tmp_dir(upload_id)
    for p in work.rglob("*"):
        if not p.is_file():
            continue
        if merged and (p.suffix.lower() in IMG_EXTS or p.name in MERGE_FILES):
            continue          # 已合并进 PDF；合并审计文件不进病例目录
        rel = p.relative_to(work).as_posix()
        try:
            hospital, case, fname = normalize_target(rel)
        except ValueError as exc:
            out["errors"].append(str(exc))
            continue
        target_dir = dest / _safe_id(hospital) / _safe_id(case)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / _safe_id(fname)
        shutil.move(str(p), str(target))
        os.chmod(target, 0o600)
        out["moved"] += 1
    out["ok"] = not out["errors"]
    return out


# ------------------------------------------------------------------ 归一结果预览
def plan(root: Path) -> dict:
    """给页面看的归一结果（§6.5）：将创建 N 个病例目录 / 共 M 个 PDF / 院区：…

    ★ round42h：只有散页图片的源根**不再**报「下没有找到任何 .pdf」了事 —— 校验与预检
      必须同一套口径（同一个源根不能预检 ok、校验红灯）。三种情况：
        * 有 PDF        ⇒ 照旧，深度 3 硬校验 + 字节数交给 G2 空间守卫
        * 只有散页图片  ⇒ 给散页计数 / 归一后的院区层预览，并把错误写成「下一步点哪里」。
                          合并 + 守卫 OCR 是小时级，塞不进这个同步 HTTP 请求 ⇒ 仍然是
                          error（不给跑），但内容是可执行的下一步，不是一句「没有 pdf」
        * 两者都没有    ⇒ 才是真错误
    """
    shp = guards.shape(root, require_pdf=False)
    shp["needs_merge"] = False
    shp["image_dirs"] = 0
    shp["images"] = 0
    shp["image_bytes"] = 0
    if not shp["pdfs"]:
        im = scan_images(root)
        shp["errors"] += im["errors"]
        shp["image_dirs"] = im["image_dirs"]
        shp["images"] = im["images"]
        shp["image_bytes"] = im["image_bytes"]
        shp["image_depth_hist"] = im["image_depth_hist"]
        shp["hospitals_from_images"] = im["hospitals_from_images"]
        shp["tree_preview"] = im["tree_preview"]
        for nt in im["notes"]:
            shp["warnings"].append(pii.scrub_text("★ 层形提示：" + nt))
        if im["img_truncated"]:
            n_dir_n = im["image_dirs"]
            shp["warnings"].append(
                f"★ 图片目录扫描在 {n_dir_n} 个目录 / "
                f"{INSPECT_MAX_IMG_SECONDS:.0f} s 内没走完 ⇒ 计数只是**下限**（跑批会扫全）。")
        n_dirs = im["image_dirs"]
        if n_dirs:
            n_img = im["images"]
            img_gb = im["image_bytes"] / 1e9
            est = im["est_minutes"]
            shp["needs_merge"] = True
            shp["cases"] = n_dirs
            shp["hospitals"] = [h["hospital"] for h in im["hospitals_from_images"]]
            # 合并是无损嵌入 ⇒ 产物字节 ≈ 原图字节，空间守卫按这个量算
            shp["total_bytes"] = im["image_bytes"]
            shp["est_minutes"] = est
            shp["truncated"] = False
            shp["warnings"] = [w for w in shp["warnings"] if "没有找到任何 .pdf" not in w]
            done = find_done_stage(root)
            tail = ""
            if done:
                tail = (" 已有一份归一好的暂存树：stage_id=" + str(done["stage_id"]) +
                        "（" + pii.scrub_text(done["tree"]) + "，" + str(done["merged"]) +
                        " 个 PDF）⇒ 点「暂存归一」会直接复用，不会重跑几小时的合并。")
            shp["errors"].append(
                "★ 这个源根只有散页图片（" + str(n_dirs) + " 个目录 / " + str(n_img) +
                " 张 / " + ("%.1f" % img_gb) + " GB），一个 .pdf 都没有 ⇒ 产线（s1_ocr）读不进去。"
                "先点「暂存归一」把散页合成 PDF：每个目录抽首页/中间/末页做多患者守卫 OCR，"
                "≈ " + str(est) + " 分钟起。" + tail)
        else:
            # require_pdf=False 之后「一个 pdf 都没有」只剩 warning ⇒ 闸门看的是 errors，
            # 不补一条就会把「既没 pdf 也没图片」的空源根放行去跑批（跑出一个空批次）。
            shp["errors"].append(
                "★ 这个源根下既没有 .pdf，也没有散页图片（" +
                " ".join(sorted(IMG_EXTS)) + "）⇒ 产线读不到任何东西，不能当源根。"
                "往上选一层：产线认 <源根>/<院区>/<病例目录>/<文件>。")
    shp["name_hits"] = guards.name_in_dirname(root) if shp["ok"] else []
    if shp["name_hits"]:
        shp["warnings"].append(
            f"G4：{len(shp['name_hits'])} 个病例目录名里含患者姓名（形如 208895-***-20240723）。"
            "bin/medrec run 会把 case 目录名原样打进 stdout（产线侧未修）⇒ 网页展示日志时打码。")
    shp["root"] = str(root)
    return shp


def stage_marker(upload_id: str) -> Path:
    return TMP_ROOT / (_safe_id(upload_id) + ".staged")


def post_process_uploads(upload_id: str) -> dict:
    """上传收尾：暂存树里的 .zip 过 G5 三检后解压归一，原 zip 只留解压结果。"""
    out = {"ok": True, "zips": [], "errors": []}
    dest = tmp_dir(upload_id)
    for zp in sorted(q for q in dest.rglob("*.zip") if q.is_file()) if dest.is_dir() else []:
        res = unpack_zip(zp, upload_id)
        out["zips"].append({"name": pii.scrub_text(zp.name), "rule": res["rule"],
                            "moved": res["moved"]})
        out["errors"] += res["errors"]
        if res["ok"]:
            zp.unlink(missing_ok=True)
        else:
            out["ok"] = False
    return out


def stage_to_final(upload_id: str, batch: str) -> Path:
    """起跑时把暂存树搬到 _ingest/<batch>/（os.replace，不复制）。

    幂等：搬过之后再 plan/start ⇒ 复用已落地目录，但**批次名不能改**
    （改了会让「页面显示的命令」和实际源根不一致，F7 破功）。
    """
    src, dst = tmp_dir(upload_id), final_dir(batch)
    mk = stage_marker(upload_id)
    if not src.is_dir():
        if not mk.is_file():
            raise FileNotFoundError(f"没有找到上传暂存目录：{src}（先上传再提交）")
        prev = Path(mk.read_text(encoding="utf-8").strip())
        if not prev.is_dir():
            raise FileNotFoundError(f"暂存目录已搬走，但落地目录不存在：{prev}")
        if prev != dst:
            raise ValueError(f"这批上传已落地为 {prev}；批次名不能改（要换批次名请重新上传）。")
        return prev
    if dst.exists():
        raise FileExistsError(f"上传落地目录已存在：{dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    for dirpath, _dirnames, filenames in os.walk(dst):
        for fn in filenames:
            os.chmod(Path(dirpath, fn), 0o600)
    mk.write_text(str(dst) + "\n", encoding="utf-8")
    os.chmod(mk, 0o600)
    return dst


# ------------------------------------------------ 本机 zip / 散页目录 → 同盘暂存归一
def chmod_tree(root: Path, dir_mode: int = 0o700, file_mode: int = 0o600) -> None:
    """暂存区里全是患者影像 ⇒ 一律 700/600（红线⑨）。解压区尤其重要：
    zipfile.extractall 会照抄 zip 里存的权限，语料 zip 里往往是 644。"""
    root = Path(root)
    if not root.exists():
        return
    try:
        os.chmod(root, dir_mode)
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            try:
                os.chmod(Path(dirpath, d), dir_mode)
            except OSError:
                pass
        for fn in filenames:
            try:
                os.chmod(Path(dirpath, fn), file_mode)
            except OSError:
                pass


def stage_local(src: Path, upload_id: str) -> dict:
    """本机 .zip / 散页图片目录 → <暂存>/<sid>/tree/<院区>/<病例>/<病例>.pdf。

    为什么必须暂存：产线只认深度 3 的源根，而 raid5 上这类语料是
    <zip>/<history_13>/caseimages/<院区>/<病例>/*.jpg（zip 里根本没有 pdf，
    解压出来还多两层）。归一模块把每个散页目录合成 1 个 PDF，直接落成深度 3。

    三条硬约束：
      * 不复制图片、不写回源目录（PDF 只写进 tree；图片留在解压区/源目录）。
      * 多患者守卫照跑（首页/中间/末页 比对住院号+姓名）；被拦的目录**一个字节都不落盘**，
        报警进 _merge_alerts.json，条数进 .stage_status.json，绝不静默吞。
      * 幂等：同名 PDF 且 sha256 相同 ⇒ 跳过（归一模块自己判）。
    """
    out = {"ok": False, "errors": [], "stage_id": _safe_id(upload_id), "stage_dir": "",
           "tree": "", "merge": None}
    p, err = local_ok(str(src))
    if err:
        out["errors"].append(err)
        return out
    sid = out["stage_id"]
    root = stage_root_for(p)
    stage = root / sid
    stage.mkdir(parents=True, exist_ok=True)
    os.chmod(stage, 0o700)
    tree = stage_tree_dir(stage)
    tree.mkdir(parents=True, exist_ok=True)
    out.update({"stage_dir": str(stage), "tree": str(tree)})
    st = {"state": "running", "stage_id": sid, "src": pii.scrub_text(str(p)),
          "stage_root": str(root), "tree": str(tree),
          "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "updated": "",
          "phase": "预检", "total_dirs": 0, "done_dirs": 0, "merged": 0, "skipped": 0,
          "blocked": 0, "pages": 0, "alerts": 0, "pdf_bytes": 0, "errors": [],
          "current": "", "zip": None, "pid": os.getpid()}
    write_stage_status(stage, st)

    sv = guards.services()
    if not sv.get("ocr_8110", {}).get("ok"):
        msg = ("OCR 服务（8110）不活 ⇒ 多患者守卫跑不了 ⇒ 拒绝开始归一。"
               "红线⑦：不许为了跑通把守卫关掉。")
        st["state"] = "error"
        st["errors"].append(msg)
        write_stage_status(stage, st)
        out["errors"].append(msg)
        return out

    unpack = p
    if p.is_file() and p.suffix.lower() == ".zip":
        z = zip_summary(p)
        st["zip"] = {k: z.get(k) for k in
                     ("entries", "uncompressed_bytes", "pdfs", "images", "image_dirs")}
        if not z["ok"]:
            st["state"] = "error"
            st["errors"] += z["errors"]
            write_stage_status(stage, st)
            out["errors"] += z["errors"]
            return out
        ws = guards.writable_space(z["uncompressed_bytes"], at=root)
        if not ws["ok"]:
            st["state"] = "error"
            st["errors"] += ws["errors"]
            write_stage_status(stage, st)
            out["errors"] += ws["errors"]
            return out
        unpack = stage / "_unpack"
        if unpack.exists():
            unpack = stage / ("_unpack-%d" % os.getpid())
        unpack.mkdir(parents=True, exist_ok=True)
        st["phase"] = "解压中（%d 条目 / %s）" % (
            z["entries"], "%.1f GB" % (z["uncompressed_bytes"] / 1e9))
        write_stage_status(stage, st)
        try:
            with zipfile.ZipFile(p) as zf:
                zf.extractall(unpack)
            chmod_tree(unpack)
        except (OSError, zipfile.BadZipFile) as exc:
            msg = f"解压失败：{type(exc).__name__}: {exc}"
            st["state"] = "error"
            st["errors"].append(msg)
            write_stage_status(stage, st)
            out["errors"].append(msg)
            return out
    elif not p.is_dir():
        msg = f"只支持 .zip 或目录：{pii.scrub_text(str(p))}"
        st["state"] = "error"
        st["errors"].append(msg)
        write_stage_status(stage, st)
        out["errors"].append(msg)
        return out

    loaded, err2 = _load_input_normalize()
    if loaded is None:
        st["state"] = "error"
        st["errors"].append(err2)
        write_stage_status(stage, st)
        out["errors"].append(err2)
        return out
    IN, C = loaded
    try:
        cfg = C.load_pipeline()
    except Exception as exc:
        msg = f"读 config/pipeline.yaml 失败：{type(exc).__name__}: {exc}"
        st["state"] = "error"
        st["errors"].append(msg)
        write_stage_status(stage, st)
        out["errors"].append(msg)
        return out

    tally = {"merged": 0, "skipped": 0, "blocked": 0, "pages": 0, "pdf_bytes": 0,
             "alerts": 0}
    # alerts 以前只在**跑完**时才算得出（st["alerts"] 全程 0）⇒ 页面上「守卫拦下 0」
    # 是假的：实测 39 个目录时 blocked 已经是 1。这里按 merge_tree 同一判据实时计数。

    def prog(i: int, total: int, d, r: dict) -> None:
        s = str(r.get("status") or "")
        if s in ("merged", "skipped_identical"):
            tally["pages"] += int(r.get("n_images") or 0)
            tally["pdf_bytes"] += int(r.get("pdf_bytes") or 0)
            tally["merged" if s == "merged" else "skipped"] += 1
        elif s.startswith("blocked") or s.startswith("rejected"):
            tally["blocked"] += 1
            tally["alerts"] += 1
        if s == "merged" and str(r.get("guard_status") or "") in ("guard_unreadable",
                                                                  "guard_error",
                                                                  "multi_patient_suspect"):
            tally["alerts"] += 1
        st.update(tally)
        st["done_dirs"] = i
        st["total_dirs"] = total
        st["current"] = pii.scrub_text(str(r.get("case_id") or Path(d).name))
        write_stage_status(stage, st)

    st["phase"] = ("合并 + 守卫（每目录 5 页 OCR）" if MERGE_GUARD
                   else "合并（无 OCR 守卫 · 一个图片目录 = 一份病例）")
    st["guard"] = MERGE_GUARD
    st["workers"] = MERGE_WORKERS
    write_stage_status(stage, st)
    try:
        res = IN.merge_tree(unpack, tree, cfg, guard=MERGE_GUARD, tree_out=True,
                            work_dir=stage / "_guard", progress=prog,
                            workers=MERGE_WORKERS)
    except Exception as exc:
        msg = f"散页图片合并失败：{type(exc).__name__}: {exc}"
        st["state"] = "error"
        st["errors"].append(msg)
        write_stage_status(stage, st)
        out["errors"].append(msg)
        return out

    man = stage / "_merge_manifest.csv"
    alr = stage / "_merge_alerts.json"
    grd = stage / "_merge_guard.json"
    IN.write_manifest(man, res["rows"])
    IN.write_alerts(alr, res["alerts"])
    IN.write_guard(grd, res["guard_records"])
    for f in (man, alr, grd):
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
    for dirpath, _dn, fns in os.walk(tree):
        for fn in fns:
            try:
                os.chmod(Path(dirpath, fn), 0o600)
            except OSError:
                pass
    chmod_tree(tree)
    chmod_tree(stage / "_guard")        # 守卫中间产物里也有整页影像/OCR 文本
    s = res["summary"]
    out["merge"] = {"src_dirs": s["src_dirs"], "merged": s["merged"],
                    "skipped_identical": s["skipped"], "blocked": s["blocked"],
                    "pages": s["pages"], "alerts": len(res["alerts"]),
                    "manifest": str(man), "alerts_path": str(alr)}
    for al in res["alerts"]:
        st["errors"].append(
            "散页守卫报警：%s kind=%s ⇒ %s（详见 %s）"
            % (pii.scrub_text(al["case_id"]), al["kind"],
               ("该目录禁止合并，已排除在源根之外，需人工确认"
                if al.get("merge_blocked", True) else "已照常合并，但需人工复核这一例"),
               alr))
    st["alerts"] = len(res["alerts"])
    st["alert_path"] = str(alr)
    st["phase"] = "完成"
    st["state"] = "done" if not st["errors"] else "done_with_alerts"
    write_stage_status(stage, st)
    out["ok"] = (s["merged"] + s["skipped"]) > 0
    if not out["ok"]:
        out["errors"].append("一个 PDF 都没产出（图片目录 %d 个，全部被拦）" % s["src_dirs"])
    return out


def spawn_stage_local(src: Path, upload_id: str) -> dict:
    """后台起归一 worker（setsid 语义：父进程退出不被回收）。2449 目录要跑几小时。"""
    p, err = local_ok(str(src))
    if err:
        raise ValueError(err)
    sid = _safe_id(upload_id)
    root = stage_root_for(p)
    stage = root / sid
    prev = read_stage_status(sid)
    if prev.get("state") == "running":
        raise RuntimeError(f"stage_id={sid} 正在归一中，不能重复起")
    stage.mkdir(parents=True, exist_ok=True)
    os.chmod(stage, 0o700)
    write_stage_status(stage, {"state": "running", "stage_id": sid,
                               "src": pii.scrub_text(str(p)), "stage_root": str(root),
                               "tree": str(stage_tree_dir(stage)), "phase": "排队",
                               "total_dirs": 0, "done_dirs": 0, "errors": []})
    log = stage / "stage.log"
    argv = [sys.executable, "-u", str(Path(__file__).resolve()),
            "--stage-worker", str(p), sid]
    fd = os.open(str(log), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    os.chmod(str(log), 0o600)
    with open(fd, "ab") as fh:
        proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                cwd=str(SRC_ROOT.parent))
    return {"stage_id": sid, "pid": proc.pid, "stage_dir": str(stage),
            "stage_root": str(root), "tree": str(stage_tree_dir(stage)),
            "log": str(log), "command": " ".join(argv[:1] + ["<归一 worker>"])}


def _cli() -> int:
    a = sys.argv[1:]
    if a[:1] == ["--stage-worker"] and len(a) >= 3:
        res = stage_local(Path(a[1]), a[2])
        print(json.dumps({"ok": res["ok"], "merge": res["merge"],
                          "errors": res["errors"][:20]}, ensure_ascii=False))
        return 0 if res["ok"] else 1
    print("用法：ingest.py --stage-worker <本机 .zip 或目录> <stage_id>")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
