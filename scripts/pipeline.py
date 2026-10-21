#!/usr/bin/env python3
# pipeline.py v2 - 病历取数链路编排：幂等 / 带日志 / 写前备份 / 红线内置 / 批次自动识别
# 用法:
#   python3 ~/medrec/scripts/pipeline.py status
#   python3 ~/medrec/scripts/pipeline.py stage1-ocr <输入目录> [--dry] [--log]
#   python3 ~/medrec/scripts/pipeline.py stage2-extract [--dry] [--batch DIR] [-- 额外参数]
#   python3 ~/medrec/scripts/pipeline.py stage3-verify [--batch DIR]
#   python3 ~/medrec/scripts/pipeline.py stage4-apply [--write] [--batch DIR]
#   python3 ~/medrec/scripts/pipeline.py all
import argparse, datetime, os, shutil, subprocess, sys, urllib.request

HOME        = "/home/ubuntu"
PY          = "/home/ubuntu/anaconda3/envs/vl-env/bin/python"
EXTRACT     = "/home/ubuntu/vital_extract.py"
APPLY       = "/home/ubuntu/apply_vitals.py"
MEDREC      = "/home/ubuntu/medrec/src/medrec"
JOBS        = "/home/ubuntu/medrec/jobs"
CSV         = "/home/ubuntu/vitals_final.csv"
BADCSV      = "/home/ubuntu/vitals_bad.csv"
SRCCSV      = "/home/ubuntu/vitals_src.csv"
REPORTS     = "/home/ubuntu/medrec/reports"
LOGDIR      = "/home/ubuntu/medrec/logs/pipeline"
UPD_STATUS  = "/home/ubuntu/medrec/scripts/update-agents-status.py"

SERVICES = [("PaddleX(OCR)",      "http://127.0.0.1:8100/health"),
            ("vLLM-qwen-27b",     "http://127.0.0.1:8102/v1/models"),
            ("vLLM-PaddleOCR-VL", "http://127.0.0.1:8110/health")]

def ts():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def log_path(stage):
    os.makedirs(LOGDIR, exist_ok=True)
    p = os.path.join(LOGDIR, "%s_%s.log" % (stage, ts()))
    latest = os.path.join(LOGDIR, "latest.log")
    try:
        if os.path.exists(latest) or os.path.islink(latest):
            os.remove(latest)
        os.symlink(p, latest)
    except OSError:
        pass
    return p

def run(cmd, logfile, label=""):
    print("+ " + " ".join(cmd), flush=True)
    with open(logfile, "a", encoding="utf-8") as f:
        f.write("\n== %s ==\n+ %s\n" % (label, " ".join(cmd)))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        buf = []
        for line in p.stdout:
            buf.append(line)
            f.write(line)
        p.wait()
        f.write("[exit %d]\n" % p.returncode)
    return p.returncode, "".join(buf)

def probe(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False

def backup(path):
    if os.path.exists(path):
        b = path + ".bak." + ts()
        shutil.copy2(path, b)
        return b
    return None

def show_services():
    rows = []
    for name, url in SERVICES:
        up = probe(url)
        rows.append(up)
        print("  %-20s %s" % (name, "UP" if up else "down"))
    return all(rows)

def job_dirs():
    if not os.path.isdir(JOBS):
        return []
    ds = [os.path.join(JOBS, d) for d in os.listdir(JOBS)
          if os.path.isdir(os.path.join(JOBS, d))]
    return sorted(ds, key=os.path.getmtime, reverse=True)

def newest_batch():
    ds = job_dirs()
    return ds[0] if ds else None

def batch_of(a):
    b = getattr(a, "batch", None) if a is not None else None
    return b or newest_batch()

def n_cases(batch):
    c = os.path.join(batch or "", "compiled")
    if not os.path.isdir(c):
        return 0
    return len([d for d in os.listdir(c) if os.path.isdir(os.path.join(c, d))])

def cmd_status(a):
    log = log_path("status")
    print("== 服务 ==")
    show_services()
    print("== 批次 ==")
    ds = job_dirs()
    if not ds:
        print("  找不到任何 job 目录（%s）" % JOBS)
    for i, d in enumerate(ds[:5]):
        mark = " ← 最新" if i == 0 else ""
        print("  %s  病例 %d%s" % (d, n_cases(d), mark))
    if len(ds) > 5:
        print("  …（共 %d 个）" % len(ds))
    print("== GPU ==")
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True).stdout
        for l in out.strip().splitlines():
            print("  " + l.strip())
    except Exception as e:
        print("  nvidia-smi 不可用：%s" % e)
    print("== 取数汇总（同时刷新 AGENTS.md 自动区）==")
    if os.path.exists(UPD_STATUS):
        rc, out = run([PY, UPD_STATUS], log, "update-agents-status")
        print(out.strip() or "(无输出)")
    else:
        print("  找不到 %s" % UPD_STATUS)
    print("\n日志：%s" % log)
    return 0

def cmd_stage1(a):
    log = log_path("stage1-ocr")
    print("stage1-ocr：识别层（PDF → compiled.md / tables.json）")
    if getattr(a, "log", False):
        cands = sorted([p for p in os.listdir(LOGDIR) if p.startswith("stage1_run_")])
        if not cands:
            print("  还没有 stage1 运行日志"); return 0
        p = os.path.join(LOGDIR, cands[-1])
        print("  最新运行日志：%s" % p)
        with open(p, encoding="utf-8") as f:
            lines = f.read().splitlines()[-25:]
        print("\n".join("  " + l for l in lines))
        return 0
    ok = show_services()
    if not ok:
        print("⚠ 有服务没起来 —— 启停属红线，由人执行：bash ~/medrec/scripts/13-svc-all.sh")
        return 1
    inp = getattr(a, "input", None)
    if not inp:
        print("\n用法：pipeline.py stage1-ocr <输入目录> [--dry] [--log]")
        print("  例：pipeline.py stage1-ocr ~/test1")
        return 2
    inp = os.path.expanduser(inp)
    if not os.path.exists(inp):
        print("找不到输入目录：%s" % inp); return 2
    medrec = MEDREC if os.path.exists(MEDREC) else (shutil.which("medrec") or "")
    if not medrec:
        print("找不到 medrec（试过 %s 和 PATH）" % MEDREC); return 2
    cmd = [medrec, "run", inp]
    nf = len(os.listdir(inp)) if os.path.isdir(inp) else 0
    print("\n命令：%s" % " ".join(cmd))
    print("  输入：%s（%d 个条目）" % (inp, nf))
    if getattr(a, "dry", False):
        print("(--dry：只打印，不执行)"); return 0
    bglog = os.path.join(LOGDIR, "stage1_run_%s.log" % ts())
    with open(bglog, "w", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    print("\n已后台启动：pid=%d" % p.pid)
    print("  运行日志：%s" % bglog)
    print("  跟踪：tail -f %s" % bglog)
    print("  之后看状态：%s %s status" % (PY, os.path.join(HOME, "medrec/scripts/pipeline.py")))
    print("  注意：跑完会生成一个新的 job 目录，status/stage2/stage3 会自动指向它")
    return 0

def cmd_stage2(a):
    log = log_path("stage2-extract")
    b = batch_of(a)
    cmd = [PY, EXTRACT, "--root", b, "--final", "--csv-final", CSV] + list(getattr(a, "extra", []) or [])
    print("stage2-extract：取数 → %s" % CSV)
    print("  当前批次：%s（病例 %d）" % (b, n_cases(b)))
    print("命令：%s" % " ".join(cmd))
    if getattr(a, "dry", False):
        print("(--dry：只打印，不执行)"); return 0
    with open(CSV + ".batch", "w", encoding="utf-8") as _mf:
        _mf.write(b or "")
    bak = backup(CSV)
    if bak:
        print("旧 CSV 已备份 → %s" % bak)
    rc, out = run(cmd, log, "stage2")
    tail = [l for l in out.strip().splitlines() if l.strip()][-6:]
    print("\n".join(tail))
    if rc == 0 and os.path.exists(UPD_STATUS):
        run([PY, UPD_STATUS, "--csv", CSV], log, "refresh-AGENTS-auto")
        print("→ AGENTS.md 自动区已刷新")
    print("日志：%s" % log)
    return rc

def cmd_stage3(a):
    log = log_path("stage3-verify")
    os.makedirs(REPORTS, exist_ok=True)
    rep = os.path.join(REPORTS, "待核清单_%s.md" % datetime.datetime.now().strftime("%Y%m%d_%H%M"))
    parts = ["# 待核清单 %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
             "\n批次：%s（病例 %d）\n" % (batch_of(a), n_cases(batch_of(a)))]
    jobs = [("bad", [PY, EXTRACT, "--root", batch_of(a), "--all", "--bad", "--why", "--csv-bad", BADCSV]),
            ("src", [PY, EXTRACT, "--root", batch_of(a), "--src", "--csv-src", SRCCSV])]
    for label, cmd in jobs:
        rc, out = run(cmd, log, "stage3-" + label)
        parts.append("\n## %s（exit %d）\n\n%s\n" % (label, rc, out.strip()[-8000:]))
        if rc != 0:
            print("⚠ %s 退出码 %d（详见日志）" % (label, rc))
    with open(rep, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print("待核清单 → %s" % rep)
    print("日志：%s" % log)
    return 0

def cmd_stage4(a):
    log = log_path("stage4-apply")
    b = batch_of(a)
    xlsx = os.path.join(b or "", "structured_final", "登记表.xlsx")
    if not os.path.exists(APPLY):
        print("找不到 %s（这一步先留空）" % APPLY); return 2
    if not os.path.exists(xlsx):
        print("找不到登记表：%s" % xlsx); return 2
    base = [PY, APPLY, "--csv", a.csv, "--xlsx", xlsx]
    if not a.write:
        rc, out = run(base, log, "stage4-dry")
        print(out.strip()[-3000:])
        print("\n(--dry：以上是它将做的改动；真正写盘属红线，由人执行下面这条)")
        print("  %s\n" % " ".join(base + ["--write"]))
        return rc
    if os.environ.get("PIPELINE_ALLOW_WRITE") != "1":
        print("拒绝写盘（红线）。要由人显式执行，请带环境变量重跑：")
        print("  PIPELINE_ALLOW_WRITE=1 %s\n" % " ".join(base + ["--write"]))
        return 3
    bak = backup(xlsx)
    if bak:
        print("登记表已备份 → %s" % bak)
    rc, out = run(base + ["--write"], log, "stage4-write")
    print(out.strip()[-3000:])
    print("日志：%s" % log)
    return rc

def main():
    ap = argparse.ArgumentParser(description="病历取数链路编排 v2（幂等 / 带日志 / 写前备份 / 红线内置）")
    sub = ap.add_subparsers(dest="stage", required=True)
    sub.add_parser("status", help="全链路状态 + 刷新 AGENTS.md 自动区").set_defaults(fn=cmd_status)
    p1 = sub.add_parser("stage1-ocr", help="识别层：medrec run <输入目录>（后台跑）")
    p1.add_argument("input", nargs="?", help="输入目录，例：~/test1")
    p1.add_argument("--dry", action="store_true")
    p1.add_argument("--log", action="store_true", help="只打印最近一次运行的日志尾部")
    p1.set_defaults(fn=cmd_stage1)
    p2 = sub.add_parser("stage2-extract", help="取数 → vitals_final.csv")
    p2.add_argument("--dry", action="store_true")
    p2.add_argument("--batch")
    p2.add_argument("extra", nargs="*", help="透传给 vital_extract.py 的参数")
    p2.set_defaults(fn=cmd_stage2)
    p3 = sub.add_parser("stage3-verify", help="对账 → reports/待核清单_*.md")
    p3.add_argument("--batch")
    p3.set_defaults(fn=cmd_stage3)
    p4 = sub.add_parser("stage4-apply", help="写登记表（默认 dry-run，写盘需人带环境变量）")
    p4.add_argument("--write", action="store_true")
    p4.add_argument("--batch")
    p4.add_argument("--csv", default=CSV)
    p4.set_defaults(fn=cmd_stage4)
    sub.add_parser("all", help="status → stage2 → stage3").set_defaults(fn=None)
    a = ap.parse_args()
    if a.stage == "all":
        ns = argparse.Namespace(extra=[], dry=False, write=False, csv=CSV, batch=None,
                                input=None, log=False)
        for fn in (cmd_status, cmd_stage2, cmd_stage3):
            rc = fn(ns)
            if rc:
                return rc
        return 0
    return a.fn(a)

if __name__ == "__main__":
    sys.exit(main())
