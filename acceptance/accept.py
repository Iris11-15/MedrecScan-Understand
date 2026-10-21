#!/usr/bin/env python3
"""只读标尺。禁止被流水线代码修改。"""
import json, argparse, pathlib, collections

AP = argparse.ArgumentParser()
AP.add_argument("--root", default="/home/ubuntu/medrec/jobs/batch_1001_2335")
AP.add_argument("--base", default="/home/ubuntu/medrec/jobs/batch_0928_1538")
AP.add_argument("--cases-file", default="/home/ubuntu/medrec/acceptance/slice.txt")
AP.add_argument("--wide", type=int, default=50)
AP.add_argument("--json", default="")
A = AP.parse_args()

ROOT, BASE = pathlib.Path(A.root), pathlib.Path(A.base)
VIT = ("体温", "脉搏", "呼吸", "收缩压", "舒张压", "血压", "心率", "血氧", "T(℃)", "HR(", "SPO2")

def load(p):
    d = json.loads(p.read_text(encoding="utf-8"))
    t = d["tables"] if isinstance(d, dict) else d
    return t if isinstance(t, list) else list(t.values())

def i0(v):
    try: return int(v or 0)
    except Exception: return 0

def names_of(root):
    comp = root / "compiled"
    if not comp.exists(): return []
    ns = sorted(d.name for d in comp.iterdir() if d.is_dir())
    sf = pathlib.Path(A.cases_file)
    if sf.exists():
        want = {l.strip() for l in sf.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith("#")}
        ns = [n for n in ns if n in want]
    return ns

def scan(root):
    out = {}
    for n in names_of(root):
        tp = root / "compiled" / n / "tables.json"
        if not tp.exists(): continue
        try: tabs = load(tp)
        except Exception as e:
            out[n] = {"tabs": [], "err": str(e)}; continue
        info = []
        for i, t in enumerate(tabs):
            rows = [r for r in (t.get("rows") or []) if isinstance(r, list)]
            nc = i0(t.get("n_cols"))
            lens = {len(r) for r in rows}
            ok = bool(rows) and len(lens) == 1 and nc in lens      # 矩形不变量
            hd = t.get("header")
            blob = " ".join(" ".join(map(str, r)) for r in rows) + " " + \
                   (" ".join(map(str, hd)) if isinstance(hd, list) else str(hd or ""))
            info.append({"i": i, "nr": len(rows), "nc": nc, "ok": ok, "hr": i0(t.get("header_rows")),
                         "type": t.get("type"), "vit": sum(1 for k in VIT if k in blob),
                         "ne": sum(1 for r in rows for c in r if str(c).strip())})
        out[n] = {"tabs": info}
    return out

def agg(d):
    tabs = [t for v in d.values() for t in v["tabs"]]
    ok = sum(1 for t in tabs if t["ok"])
    wide = [t for t in tabs if t["nc"] >= A.wide]
    vloc = sum(1 for v in d.values() if any(t["vit"] >= 2 for t in v["tabs"]))
    return {"cases": len(d), "tables": len(tabs), "rect_ok": ok,
            "rect_pct": round(100.0 * ok / len(tabs), 2) if tabs else 0.0,
            "wide": len(wide), "widest": max([t["nc"] for t in wide], default=0),
            "vital_located": vloc,
            "vital_pct": round(100.0 * vloc / len(d), 1) if d else 0.0}

NEW, OLD = scan(ROOT), scan(BASE)
an, ao = agg(NEW), agg(OLD)

print("=" * 72)
print("标尺读数（Codex 迭代只看这三行；数值越高越好）")
print(f"M1 矩形不变量   NEW {an['rect_ok']}/{an['tables']} = {an['rect_pct']}%   (OLD {ao['rect_pct']}%)")
print(f"M2 宽表(>={A.wide}列)  NEW {an['wide']} 张 · 最宽 {an['widest']} 列   (OLD {ao['wide']} 张 · 最宽 {ao['widest']} 列)")
print(f"M3 体征表可定位 NEW {an['vital_located']}/{an['cases']} = {an['vital_pct']}%   (OLD {ao['vital_pct']}%)")
if not OLD:
    print("!! OLD 侧 0 个 case —— slice 里的名字旧批没有，OLD 仅供参考")
if not pathlib.Path(A.cases_file).exists():
    print(f"!! 没有 {A.cases_file} —— 现在跑的是全批，读数不可比，先建切片！")

print("\n" + "=" * 72)
print("失败明细（Codex 从这里挑最集中的改）")
bad = [(n, v) for n, v in sorted(NEW.items()) if any(not t["ok"] for t in v["tabs"])]
print(f"矩形不变量失败 case：{len(bad)} 个")
for n, v in bad[:10]:
    b = [t for t in v["tabs"] if not t["ok"]]
    print(f"  {n}  坏表 {len(b)}/{len(v['tabs'])}  最宽坏表 {max(t['nc'] for t in b)} 列  idx={[t['i'] for t in b][:8]}")
nv = [n for n, v in NEW.items() if not any(t["vit"] >= 2 for t in v["tabs"])]
print(f"定位不到体征表 case：{len(nv)} 个  {nv[:10]}")

print("\n" + "=" * 72)
print("诊断：全局最宽表结构（这一段贴给我）")
def widest(d, home):
    best = None
    for n, v in d.items():
        for t in v["tabs"]:
            if best is None or t["nc"] > best[2]["nc"]: best = (n, home, t)
    return best
for tag, d, home in (("NEW", NEW, ROOT), ("OLD", OLD, BASE)):
    w = widest(d, home)
    if not w: print(f"\n[{tag}] 无表"); continue
    n, home, t = w
    print(f"\n[{tag}] {n}  表[{t['i']}]  {t['nc']}列 × {t['nr']}行  header_rows={t['hr']} "
          f"type={t['type']} 非空={t['ne']} 矩形={t['ok']}")
    tt = load(home / "compiled" / n / "tables.json")[t["i"]]
    rows = [r for r in (tt.get("rows") or []) if isinstance(r, list)]
    print(f"    ★行长分布: {collections.Counter(len(r) for r in rows).most_common(5)}")
    hd = tt.get("header")
    print(f"    header[:25] = {[str(x) for x in hd][:25] if isinstance(hd, list) else str(hd)[:300]}")
    for k in list(range(min(3, len(rows)))) + ([-1] if len(rows) > 3 else []):
        r = [str(x) for x in rows[k]]
        print(f"    row[{k}] len={len(r)}: {r[:25]}{' ...(+%d)' % (len(r)-25) if len(r) > 25 else ''}")

if A.json:
    pathlib.Path(A.json).write_text(
        json.dumps({"new": an, "old": ao, "cases_new": NEW}, ensure_ascii=False), encoding="utf-8")
    print(f"\n已写 {A.json}")
