#!/usr/bin/env python3
# update-agents-status.py v3 - 把最新取数汇总写进 ~/medrec/AGENTS.md 的 AUTO 区(幂等)
# v3: 分桶列按内容打分自动识别; 备份写改动前内容; 分桶名归一(短); 读 <csv>.batch 记录批次
import argparse, csv, glob, os, re, subprocess, sys, time
from collections import Counter

AGENTS  = "/home/ubuntu/medrec/AGENTS.md"
PY      = "/home/ubuntu/anaconda3/envs/vl-env/bin/python"
EXTRACT = "/home/ubuntu/vital_extract.py"
OUTCSV  = "/home/ubuntu/vitals_final.csv"
BEGIN   = "<!-- AUTO:STATUS BEGIN -->"
END     = "<!-- AUTO:STATUS END -->"
HINTS   = ("两来源一致", "两来源不一致", "仅正文", "仅护理", "无来源", "缺失", "待核")

def newest_csv(explicit):
    if explicit:
        return explicit
    cands = glob.glob("/home/ubuntu/vitals*final*.csv") + glob.glob("/home/ubuntu/vitals_all.csv")
    cands = [c for c in cands if os.path.getsize(c) > 0]
    return max(cands, key=os.path.getmtime) if cands else None

def run_extract():
    p = subprocess.run([PY, EXTRACT, "--final", "--csv-final", OUTCSV],
                       capture_output=True, text=True, timeout=7200)
    return "\n".join([l for l in (p.stdout or "").splitlines() if l.strip()][-4:])

def load(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return (rows[0], rows[1:]) if rows else ([], [])

def pick(head, *keys):
    for i, h in enumerate(head):
        for k in keys:
            if k in h:
                return i
    return -1

def pick_bucket(head, body):
    cand = []
    for i in range(len(head)):
        vals = [r[i].strip() for r in body if i < len(r)]
        if not vals:
            continue
        hits = sum(1 for v in vals if len(v) <= 16 and any(k in v for k in HINTS))
        cand.append((hits / float(len(vals)), -sum(len(v) for v in vals) / float(len(vals)), i))
    if not cand:
        return -1
    cand.sort(reverse=True)
    return cand[0][2]

def norm_bucket(v):
    v = (v or "").strip()
    if ("缺失" in v) or ("无来源" in v):
        return "无来源"
    if "不一致" in v:
        return "两来源不一致"
    if "仅正文" in v:
        return "仅正文"
    if "仅护理" in v:
        return "仅护理单"
    if "一致" in v:
        return "两来源一致"
    return v[:24] or "(空)"

def build(csv_path, tail_text=""):
    head, body = load(csv_path)
    if not body:
        return "- 取数 CSV 无数据: " + csv_path
    i_hosp = pick(head, "院区", "医院")
    i_buck = pick_bucket(head, body)
    if i_hosp < 0 or i_buck < 0:
        return "- 认不出 CSV 列(表头: " + " | ".join(head[:12]) + ")"
    L = ["- 更新时间: %s | 数据源: %s (%d 行)" %
         (time.strftime("%Y-%m-%d %H:%M"), os.path.basename(csv_path), len(body))]
    bmark = csv_path + ".batch"
    if os.path.exists(bmark):
        try:
            b = open(bmark, encoding="utf-8").read().strip()
            if b:
                L.append("- 批次: " + b)
        except OSError:
            pass
    cnt = Counter(norm_bucket(r[i_buck]) for r in body if i_buck < len(r))
    L.append("- 分桶(列 %s): %s" % (head[i_buck],
             " | ".join("%s %d" % (k, v) for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]))))
    per, miss = Counter(), Counter()
    for r in body:
        if i_hosp >= len(r):
            continue
        h = r[i_hosp].strip() or "未知"
        per[h] += 1
        v = r[i_buck] if i_buck < len(r) else ""
        if ("缺失" in v) or ("无来源" in v):
            miss[h] += 1
    L.append("- 按院区(例数 / 无来源): " +
             "、".join("%s %d/%d" % (h, n, miss[h]) for h, n in sorted(per.items(), key=lambda kv: -kv[1])))
    todo = "、".join("%s %d" % (h, n) for h, n in sorted(miss.items(), key=lambda kv: -kv[1]) if n > 0)
    L.append("- 待办(按无来源降序): " + (todo or "无"))
    if tail_text:
        for l in tail_text.splitlines():
            L.append("- 上次: " + l.strip())
    return "\n".join(L)

def splice(path, block):
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    txt = old if old else "# AGENTS.md\n"
    body = BEGIN + "\n" + block + "\n" + END
    if BEGIN in txt and END in txt:
        txt = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda m: body, txt, flags=re.S)
    else:
        m = re.search(r"^##\s*三、.*$", txt, flags=re.M)
        if m:
            txt = txt[:m.end()] + "\n" + body + "\n" + txt[m.end():]
        else:
            txt = txt.rstrip() + "\n\n## 当前状态(自动更新区)\n" + body + "\n"
    if old:
        with open(path + ".bak.status", "w", encoding="utf-8") as f:
            f.write(old)
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    return bool(old)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    tail = ""
    if a.run:
        print("跑取数中(可能几分钟)...", flush=True)
        tail = run_extract()
    csvp = newest_csv(a.csv)
    if not csvp:
        sys.exit("找不到取数 CSV; 先跑 vital_extract.py --all --csv-final " + OUTCSV)
    had = splice(AGENTS, build(csvp, tail))
    print("已更新 " + AGENTS + ("(上一版备份到 .bak.status)" if had else ""))
    print()
    with open(AGENTS, encoding="utf-8") as f:
        m = re.search(re.escape(BEGIN) + r".*?" + re.escape(END), f.read(), flags=re.S)
    print(m.group(0) if m else "(未找到 AUTO 区)")

if __name__ == "__main__":
    main()
