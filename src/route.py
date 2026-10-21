#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F6 分流层：规则前置 + Laya 候选 + margin 门控。
契约：一份文档 = 一次 Laya 调用（items = 命中异常证据的题集）—— 不按题拆调用，
      因为每种新 shape 都要付一次 Triton 编译（实测 ~120 ms）。
定档优先级：rule_no_evidence(0) > laya(margin>=M) > escalate(交 arbiter.py)
"""
import argparse
import json
import os
import sys
import urllib.request
# 规则层现在也出档位：复用离线已评测的 label_nihss.label(text)（留出集 23 例 99.1%）。
# 8111 上那个基座检查点在「有证据」的格子上只有 15.4%，档位不该交给它。
# 路径可用 NIHSS_LABEL_DIR 覆盖；导入失败则静默退回旧行为（不炸产线）。
import sys as _sys, os as _os
_LDIR = _os.environ.get("NIHSS_LABEL_DIR", _os.path.expanduser("~/laya-ft"))
if _LDIR not in _sys.path:
    _sys.path.insert(0, _LDIR)
try:
    import label_nihss as _L
except Exception as _e:
    _L = None
    print("[warn] label_nihss 导入失败，退回「档位全交 Laya」：%s" % _e, file=_sys.stderr)
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = Path(os.environ.get("MEDREC_VARS", HERE / "variables.json"))
TIMEOUT = float(os.environ.get("LAYA_TIMEOUT", "180"))

# 与 F4 的 neg_grounded 同一套思路：紧邻否定词即视为阴性，不做全篇否定推理
NEG_WORDS = ("无", "未见", "未闻及", "未诉", "不伴", "否认", "排除", "阴性", "没有")
NEG_WINDOW = 3


def _our_keys(f6_key):
    """F6 的 13 项键 -> label_nihss 的 15 项键。

    F6 把 5a/5b 合成 5_上肢运动、6a/6b 合成 6_下肢运动（合并后满分 34），
    所以这两项要取两侧较差的那个（max）—— 与合并题面的口径一致。
    """
    base = f6_key.split("_", 1)[0]      # "1a_意识水平"->"1a"; "10_构音障碍"->"10"
    if base == "5":
        return ("5a", "5b")
    if base == "6":
        return ("6a", "6b")
    return (base,)


def negated(text, idx):
    pre = text[max(0, idx - NEG_WINDOW):idx]
    return any(pre.endswith(n) for n in NEG_WORDS)


WIN = 15       # 部位词之后的结果观察窗
WIN_PRE = 0    # 不看部位词之前：上一项检查的结果会渗进来（梁宗志 轮替→不稳 就是这么来的）


def _scan_ab(text, words):
    out = []
    for w in words:
        i = text.find(w)
        while i >= 0:
            if not negated(text, i):
                out.append(w)
                break
            i = text.find(w, i + 1)
    return out


def _covered(seg, i, w, nm_words):
    """ab 命中是否整个落在某个正常短语内部（"无变浅" 里的 "变浅"）"""
    for n in nm_words:
        j = seg.find(n)
        while j >= 0:
            if j <= i and i + len(w) <= j + len(n):
                return True
            j = seg.find(n, j + 1)
    return False


def _window_hit(seg, wab, wnm):
    hits = []
    for w in wab:
        i = seg.find(w)
        while i >= 0:
            if not _covered(seg, i, w, wnm) and not negated(seg, i):
                hits.append(w)
                break
            i = seg.find(w, i + 1)
    return hits


def find_evidence(text, item):
    """返回 (异常证据, 正常证据)

    ab   : 独立异常短语，命中即放行（如 "共济失调"）
    site : 检查部位词（如 "指鼻"）；在其邻近窗口内先排除正常短语，
           再找异常词 —— 专门解决「检查名 ≠ 异常结果」
    """
    ab = _scan_ab(text, item.get("ab", []))
    nm = [w for w in item.get("nm", []) if w in text]

    site = item.get("site") or []
    wab, wnm = item.get("win_ab", []), item.get("win_nm", [])
    for sw in site:
        i = text.find(sw)
        while i >= 0:
            seg = text[max(0, i - WIN_PRE):i] + text[i + len(sw): i + len(sw) + WIN]
            hit = _window_hit(seg, wab, wnm)
            if hit:
                ab.append("%s→%s" % (sw, "/".join(hit)))
                break
            if any(n in seg for n in wnm):
                i = text.find(sw, i + len(sw))
                continue
            ab.append("%s→未明" % sw)
            break
    return ab, nm


def call_laya(url, model, text, items, scale, timeout=TIMEOUT):
    body = json.dumps({"text": text, "scale": scale, "items": items, "model": model},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/score", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def margin_of(res):
    probs = ((res.get("raw") or {}).get("probabilities")) or {}
    if len(probs) < 2:
        return 1.0
    v = sorted((float(x) for x in probs.values()), reverse=True)
    return v[0] - v[1]


def run(text, cfg, dry=False):
    M = float(cfg.get("margin_min", 0.10))
    out = {"scale": cfg["scale"], "items": {}, "total": 0,
           "n_rule_zero": 0, "n_rule": 0, "n_laya": 0, "n_escalate": 0, "laya_called": False}
    hits, plan = {}, []
    rlv, runk = {}, set()
    if _L is not None:
        try:
            rlv, runk, _rwhy = _L.label(text)
        except Exception as _e:
            print("[warn] label_nihss.label 失败，本份退回 Laya：%s" % _e, file=_sys.stderr)
    for key, item in cfg["items"].items():
        ab, nm = find_evidence(text, item)
        hits[key] = {"ab": ab, "nm": nm}
        mine = _our_keys(key)
        if _L is not None and any(k not in runk for k in mine):
            # 我们自己那套规则认得这一项 -> 由它定档；不看 F6 词表、不问 Laya。
            # 同时补上 F6 词表最大的漏：没有 肌力4级，459 个运动项格子被当成没提判 0。
            out["items"][key] = {"level": max(rlv[k] for k in mine),
                                 "source": "rule", "evidence": hits[key],
                                 "margin": None, "escalate": False}
            out["n_rule"] += 1
            continue
        if ab:
            plan.append(key)
        else:
            out["items"][key] = {"level": 0, "source": "rule_no_evidence",
                                 "evidence": hits[key], "margin": None, "escalate": False}
            out["n_rule_zero"] += 1

    if plan and not dry:
        res = call_laya(cfg.get("laya_url"), cfg.get("laya_model", "multilingual"),
                        text, plan, cfg["scale"])
        out["laya_called"] = True
        out["laya_usage"] = res.get("usage")
        got = res.get("items") or {}
        for key in plan:
            r = got.get(key)
            if not r:
                out["items"][key] = {"level": None, "source": "laya_missing",
                                     "evidence": hits[key], "margin": None, "escalate": True}
                out["n_escalate"] += 1
                continue
            m = margin_of(r)
            esc = m < M
            out["items"][key] = {
                "level": r.get("level"),
                "expected": r.get("expected"),
                "margin": round(m, 4),
                "answer_confidence": (r.get("raw") or {}).get("answer_confidence"),
                "source": "laya",
                "evidence": hits[key],
                "escalate": esc,
            }
            out["n_laya"] += 1
            if esc:
                out["n_escalate"] += 1
    elif plan:
        for key in plan:
            out["items"][key] = {"level": None, "source": "dry_run",
                                 "evidence": hits[key], "margin": None, "escalate": False}

    out["total"] = sum((v.get("level") or 0) for v in out["items"].values())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--out")
    ap.add_argument("--dry", action="store_true", help="只跑规则层，不调服务")
    a = ap.parse_args()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    text = Path(a.text_file).read_text(encoding="utf-8")
    r = run(text, cfg, dry=a.dry)
    js = json.dumps(r, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(js, encoding="utf-8")
    print(js)
    print(f"\n[摘要] 规则判0={r['n_rule_zero']}  Laya采纳={r['n_laya']}  "
          f"升级={r['n_escalate']}  总分={r['total']}", file=sys.stderr)


if __name__ == "__main__":
    main()
