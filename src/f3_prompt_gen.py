#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F3 提示词与 Schema 生成器（functional-spec §F3.2）

核心约束：**只把「需模型」的列放进 schema，规则层能产出的列一律不进提示词。**
实测依据（叶文达那一份 structured.json）：
    模型当轮输出 823 条，项目去重只有 368 条 → 55% 是重复的
    类别分布里 化验 203 + 一般情况 168 + 用药 87 + 检查 22 + 查体 15 = 495 条（60%）
    全部落在规则层的射程内
所以把 428 列摘出去，模型不用再逐条转写它们。

输出：
    f3_schema.json    —— 给 vLLM 的 response_format.json_schema
    f3_system.txt     —— 系统提示词（字段清单写死）

用法：
    python3 f3_prompt_gen.py                     # 生成全部
    python3 f3_prompt_gen.py --section 查体       # 只看某段的字段子集
    python3 f3_prompt_gen.py --estimate          # 按实测基线估算节省
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VARS = HERE / "variables_f3.json"
ALIAS = HERE / "section_alias.json"
OUT_SCHEMA = HERE / "f3_schema_v2.json"
OUT_SYS = HERE / "f3_system_v2.txt"
# ★ 故意不叫 f3_schema.json / f3_system.txt —— 服务器 ~/medrec/src/ 下**已经存在**
#   同名文件，被 run_f3.sh 用 --schema/--system 引用着。那两个是
#   {"相关": bool, "条目": [{类别,项目,值,出处}]} 的自由抽取式 schema；
#   这里是「67 个写死字段」的受约束式 schema，是**另一种设计**。
#   名字撞上会把线上配置直接覆盖掉。

MODEL_KINDS = ("llm_text", "small", "small_then_llm")

# ── 实测基线 ─────────────────────────────────────────────────────────────
# ★ 2026-09-25 重测（run_f3.sh，qwen-27b@8102，--jobs 8，--max-chars 2800）：
#     30 chunk，墙钟 122.3 s，条目 475，失败 1 块（补跑 1 轮后 0）
#   **单轮墙钟 122 s**，不是下面那个 770 s。
BASE = {
    "chunks": 30, "wall_seconds": 122.3, "jobs": 8, "items": 475,
    "prompt_tokens": 239219, "completion_tokens": 29675,
}

# ⚠️ 旧基线。出自一份 `qwen-small @ 8101` 的陈年 structured.json（8101 已停用）。
#    它的 `seconds` 是 **28 个 chunk 的时间之和**，不是墙钟 —— 在 --jobs 8 下，
#    墙钟约等于它的 1/8。我曾经拿它当墙钟用了六轮，把单轮算成 770 s（实际 122 s），
#    **高估 6 倍**。留着只做对照，**不要再拿它算时间**。
STALE = {"chunks": 28, "seconds_summed": 770, "items": 823,
         "prompt_tokens": 215620, "completion_tokens": 42964,
         "model": "qwen-small", "url": "8101"}

# 实测的段尺寸（batch_0924_1527 / 202424089790叶文达 的 sections_text.json）
#   ★ `_其他` 35,045 字 = 66%，`诊断` 14,640 字 = 28% —— 两段合起来 94%
#   剩下 8 段合计约 3,300 字（辅助检查 988 / 现病史 595 / 个人史 578 /
#   查体 532 / 医嘱 290 / 既往史 157 / 家族史 119 / 主诉 30）
SECTION_CHARS = {"_其他": 35045, "诊断": 14640, "辅助检查": 988, "现病史": 595,
                 "个人史": 578, "查体": 532, "医嘱": 290, "既往史": 157,
                 "家族史": 119, "主诉": 30}

# chunk 分布：实测共 30 个 chunk；按 2800 字/chunk 折算 ——
#   `_其他` 35045/2800 ≈ 13，实测 15；`诊断` 14640/2800 ≈ 5，实测 7
CHUNK_DIST = {"主诉": 1, "现病史": 1, "既往史": 1, "个人史": 1, "家族史": 1,
              "查体": 1, "辅助检查": 1, "诊断": 7, "医嘱": 1, "_其他": 15}

# `_其他` 细分后，15 个 chunk 大致会落到哪些子段
#   （按 F6 那次真实抽取看到的标题猜：住院病案首页 / 入院记录 / 出院记录 /
#     查房记录 / 专科检查 —— 等真实 sections_text 的子标题出来要校准）
OTHER_SPLIT = {"_其他:住院病案首页": 3, "_其他:入院记录": 2, "_其他:出院记录": 3,
               "_其他:查房记录": 5, "_其他:专科检查": 2}

TOK_PER_FIELD = 40          # 每字段约 40 tokens（值 + 块 + JSON 骨架）


def wall_seconds(summed_seconds, jobs):
    """CPU/GPU 时间之和 → 墙钟。**这两个数在有并发时差一个 jobs 的倍数**，
    上一轮的 6 倍误差就出在这里。"""
    return summed_seconds / max(jobs, 1)


def load_model_cols():
    """→ OrderedDict{col: entry}，只含需模型的列"""
    v = json.loads(VARS.read_text(encoding="utf-8"))
    out = {}
    for sheet, items in {k: v for k, v in v.items()
                         if not str(k).startswith("_")}.items():
        for e in items:
            if (e.get("extract") or {}).get("kind") in MODEL_KINDS:
                out[e["col"]] = {**e, "sheet": sheet}
    return out


def load_alias():
    if not ALIAS.exists():
        return {}
    return json.loads(ALIAS.read_text(encoding="utf-8"))


def fields_for_section(cols, section, alias):
    """某段该问哪些字段 —— 靠 source_sections ∧ alias 反查"""
    inv = {}
    for logical, keys in (alias or {}).items():
        if logical.startswith("_"):
            continue
        for k in keys:
            inv.setdefault(k, set()).add(logical)
    wanted = inv.get(section, set()) | {section}
    if section.startswith("_其他:"):          # 细分伪段落
        base = section.split(":", 1)[1]
        wanted |= {base}
    out = []
    for col, e in cols.items():
        srcs = set(e.get("source_sections") or [])
        if srcs & wanted or (not srcs):
            out.append(col)
    return out


def build_schema(cols, fields):
    """vLLM json_schema：每个字段是一个 {值, 块, 状态} 对象。

    ★ `状态` 是必须的，不是装饰。

    只有 `值` 一个字符串时，「这个病人**没有**二尖瓣狭窄」和
    「文书里**没提**二尖瓣狭窄」都会被写成空字符串 ——
    **两者在输出上完全一样**。实测：166 列升级里模型对 89 列返回空值，
    分不清是哪种，`merge()` 只能一律判待确认。
    而按登记表语义，前者应当是**阴性**（`missing_means: 否若段在`），
    那一批列就这么白白丢掉了。

    三态：有值 / 明确没有（文书说了没有，或该病在诊断列表里确实不存在的写法）/
    未提及（文书里找不到依据，不能推断）。
    """
    props = {}
    for col in fields:
        props[col] = {
            "type": "object",
            "properties": {
                "值": {"type": "string"},
                "块": {"type": "string"},
                "状态": {"type": "string",
                        "enum": ["有值", "明确没有", "未提及"]},
            },
            "required": ["值", "块", "状态"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": props,
        "required": list(fields),
        "additionalProperties": False,
    }


HEADER = """你是卒中登记表的字段抽取器。**只输出 JSON，不要任何解释、不要 markdown 代码块。**

规则：
1. 逐个字段判断，**只依据原文中明确写到的内容**。
2. **不要根据其他字段推断，也不要把别处的结论搬过来。**
3. 每个字段给出「块」：你在原文里依据的那个块 ID（形如 p3-b7）。没有依据时填空字符串。
4. **严格只输出下面列出的字段**，不要增删字段、不要改字段名、不要加嵌套层级。
5. 字段名原样照抄（含括号和单位），不要简写。

**每个字段必须给「状态」，三选一，不要都用「未提及」：**

- `有值`      —— 原文写到了这个字段的内容。「值」填原文的写法（数字带单位就带单位）。
- `明确没有`  —— 原文**明确**表示没有 / 阴性 / 未使用。例如「否认XX」「未用XX」
                「XX（-）」。这时「值」留空字符串。
- `未提及`    —— 原文里找不到这个字段的任何依据，**不能**推断有没有。
                「值」留空字符串。

判断「明确没有」时要有原文依据（否定词、阴性符号、诊断列表里确实没有）。
**拿不准就填「未提及」** —— 把「找不到」写成「明确没有」会凭空造出阴性结论，
比留空危险得多。
"""


def build_system(cols, fields):
    lines = [HEADER, f"\n字段清单（共 {len(fields)} 个）：\n"]
    by_sheet = {}
    for col in fields:
        by_sheet.setdefault(cols[col]["sheet"], []).append(col)
    for sheet, cs in by_sheet.items():
        lines.append(f"【{sheet}】")
        for c in cs:
            lines.append(f"  - {c}")
        lines.append("")
    return "\n".join(lines)


def estimate(cols, all_fields, alias):
    """按实测基线估算。

    ★ 时间口径：**必须用墙钟**。`seconds` 是 chunk 时间之和，
      有并发时差一个 `--jobs` 的倍数 —— 我上一轮就是在这里把单轮算成了 770 s（实际 122 s）。
    """
    ch_n, wall, jobs = BASE["chunks"], BASE["wall_seconds"], BASE["jobs"]
    print("=== 按实测基线估算（2026-09-25 重测，run_f3.sh / qwen-27b@8102）===")
    print(f"  实测：{ch_n} chunk，**墙钟 {wall} s**（--jobs {jobs}），"
          f"条目 {BASE['items']}，单轮")
    print(f"  对照旧基线（qwen-small@8101，勿用于算时间）："
          f"{STALE['chunks']} chunk，seconds 求和 {STALE['seconds_summed']} s，"
          f"条目 {STALE['items']}")
    print(f"  ⚠️ 时间口径：`seconds` 求和 ÷ jobs = 墙钟；"
          f"{STALE['seconds_summed']} ÷ {jobs} = {wall_seconds(STALE['seconds_summed'], jobs):.0f} s"
          f" —— 和实测的 {wall} s 同量级，对得上\n")

    per_item_now = BASE["items"] / ch_n
    print(f"  现状每 chunk 约 {per_item_now:.0f} 条条目，"
          f"整份 {BASE['items']} 条（自由抽取，含重复）")

    grab = set((alias.get("_expand") or {}).keys())   # 混装段（如 _其他），必须细分

    def total_fields(dist, tag):
        tot, empty, grab_hit = 0, [], []
        for sec, n in dist.items():
            f = fields_for_section(cols, sec, alias)
            if not f and n >= 2:
                (grab_hit if sec in grab else empty).append((sec, n))
            tot += n * len(f)
            print(f"    {sec:22} {n:>2} chunk × {len(f):>3} 字段 = {n*len(f):>4} 字段")
        if empty:
            print(f"    （{empty} 得 0 字段是**正确**的：那几段本来就没有需模型字段）")
        if grab_hit:
            print(f"    ❌ {tag}：{grab_hit} 得 0 字段是**错的** —— "
                  f"{[s for s, _ in grab_hit]} 是混装段，半个文档都在里面，必须先细分")
        return tot, grab_hit

    print("\n  【方案 A】只用 10 个真实段做过滤（_其他 整段算）：")
    fa, hit_a = total_fields(CHUNK_DIST, "方案 A")

    print("\n  【方案 B】把 _其他 按标题再细分：")
    dist_b = {k: v for k, v in CHUNK_DIST.items() if k != "_其他"}
    dist_b.update(OTHER_SPLIT)
    fb, hit_b = total_fields(dist_b, "方案 B")

    print(f"\n  ⚠️ **方案 A 不成立**：它把 `_其他` 那 {CHUNK_DIST['_其他']} 个 chunk"
          f"（**占文档 66%**）压成 0 字段，等于对病程记录/出院记录/病案首页一个字段都不问。")

    # ── 用**实测 token** 算，不再用条目数换算 ──────────────────────────
    doc_chars = sum(SECTION_CHARS.values())
    sys_per_call = (BASE["prompt_tokens"] - doc_chars) / BASE["chunks"]
    calls_after = BASE["chunks"] // 4 + 1
    prompt_after = sys_per_call * calls_after + doc_chars
    total_now = BASE["prompt_tokens"] + BASE["completion_tokens"]
    print(f"\n  【实测 token 账】prompt {BASE['prompt_tokens']:,}"
          f"  = 文档正文 {doc_chars:,} + 系统提示 {sys_per_call:,.0f} × {BASE['chunks']} 次")
    print(f"                  completion {BASE['completion_tokens']:,}"
          f"  = {BASE['items']} 条 × "
          f"{BASE['completion_tokens']/BASE['items']:.1f} tokens/条")
    print(f"                  合计 {total_now:,}   墙钟 {BASE['wall_seconds']} s")
    print(f"  合并 chunk 后系统提示只发 {calls_after} 次 → "
          f"prompt ≈ {prompt_after:,.0f}（{prompt_after/BASE['prompt_tokens']:.0%}）")

    for tag, f_count, hit in (("A（不成立）", fa, hit_a), ("B（可行）", fb, hit_b)):
        tok_out = f_count * TOK_PER_FIELD
        total_after = prompt_after + tok_out
        ratio = total_after / total_now
        sec1 = BASE["wall_seconds"] * ratio
        print(f"\n  {tag}：字段 {f_count}，输出 ≈ {tok_out:,} tokens"
              f"（{tok_out/BASE['completion_tokens']:.0%}）")
        print(f"    合计 {total_now:,} → {total_after:,.0f} tokens"
              f"（**{ratio:.2f}×**）   单轮 {BASE['wall_seconds']} s → {sec1:.0f} s")
        for rounds, note in ((3, "保持 3 轮"), (1, "轮数 3→1（假设，未验）")):
            per_doc = sec1 * rounds
            print(f"      {note}：单份 {per_doc:,.0f} s  →  "
                  f"373,233 份 ≈ {373233*per_doc/3600:,.0f} h")

    print(f"\n  【现状实测】单轮 {BASE['wall_seconds']} s"
          f"（3 轮 = {BASE['wall_seconds']*3:,.0f} s/份 → "
          f"{373233*BASE['wall_seconds']*3/3600:,.0f} h）")
    print("  ⚠️ 输出侧的「每字段 40 tokens」仍是估算 —— 受约束 schema 下的实测值还没取到。")

    # ★ 升级率对成本到底有多大影响 —— 实测下来的答案是「几乎没有」
    print("\n  【升级率的成本敏感度】把 0~100% 全扫一遍：")
    for pct, label in ((0.0, "全挡下"), (0.131, "★ 真实结构下界"),
                       (0.402, "我的 fixture"), (1.0, "全升级")):
        nf = int(495 * pct)
        tot = prompt_after + nf * TOK_PER_FIELD
        print(f"    升级 {pct:>5.1%}（{nf:>3} 列）→ 合计 {tot:,.0f} tokens "
              f"→ 单轮 {BASE['wall_seconds']*tot/total_now:>4.1f} s")
    lo = prompt_after
    hi = prompt_after + 495 * TOK_PER_FIELD
    print(f"    → **0% 到 100% 只差 {BASE['wall_seconds']*hi/total_now - BASE['wall_seconds']*lo/total_now:.1f} s"
          f"（{hi/lo-1:.0%}）**；而「系统提示发 30 次 → 发 8 次」一项就值 2.4×")
    print("    ⇒ **成本杠杆是提示词批量，不是升级率。** 升级率影响的是质量与人工复核量。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", help="只生成该段的字段子集")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--out-schema", default=str(OUT_SCHEMA))
    ap.add_argument("--out-sys", default=str(OUT_SYS))
    a = ap.parse_args()

    cols = load_model_cols()
    alias = load_alias()
    print(f"需模型的列: {len(cols)} / 495")
    from collections import Counter
    print("  kind 分布:", dict(Counter(e["extract"]["kind"] for e in cols.values())))
    print("  sheet 分布:", dict(Counter(e["sheet"] for e in cols.values())))

    if a.estimate:
        print()
        estimate(cols, list(cols), alias)
        return

    fields = list(cols) if not a.section else fields_for_section(cols, a.section, alias)
    if a.section:
        print(f"\n段「{a.section}」→ {len(fields)} 个字段")

    schema = build_schema(cols, fields)
    system = build_system(cols, fields)

    Path(a.out_schema).write_text(json.dumps(schema, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    Path(a.out_sys).write_text(system, encoding="utf-8")

    print(f"\n已写 {a.out_schema}  ({len(json.dumps(schema, ensure_ascii=False)):,} 字符)")
    print(f"已写 {a.out_sys}     ({len(system):,} 字符 ≈ {len(system):,} tokens)")

    rule_leak = [c for c in fields
                 if cols[c]["extract"]["kind"] not in MODEL_KINDS]
    assert not rule_leak, f"规则列漏进 schema: {rule_leak[:5]}"
    assert len(fields) == len(set(fields)), "字段名有重复"
    assert set(schema["required"]) == set(fields)
    print("校验通过：无规则列泄漏 / 字段名无重复 / required 与 properties 一致")


if __name__ == "__main__":
    main()
