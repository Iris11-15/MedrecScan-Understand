#!/usr/bin/env bash
# F6 批跑驱动：一份文档 = route.py 一次 Laya 调用 + arbiter.py 一次 Qwen 调用
# 用法: bash f6.sh <文本文件或目录> [输出目录]
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
IN="${1:?用法: f6.sh <文本文件或目录> [输出目录]}"
OUT="${2:-$SRC/jobs/f6}"
mkdir -p "$OUT"
export QWEN_MODEL="${QWEN_MODEL:-qwen-27b}"

one() {
  local txt="$1" stem
  stem="$(basename "${txt%.txt}")"
  python3 "$SRC/route.py"   --text-file "$txt" --out "$OUT/$stem.route.json" >/dev/null
  python3 "$SRC/arbiter.py" --text-file "$txt" --route "$OUT/$stem.route.json" \
          --out "$OUT/$stem.final.json" --prov "$OUT/$stem.prov.jsonl" >/dev/null
  python3 - "$stem" "$OUT/$stem.final.json" <<'PY'
import json, sys
stem, path = sys.argv[1], sys.argv[2]
d = json.load(open(path, encoding="utf-8"))
n = lambda s: sum(1 for r in d["items"].values() if r["source"] == s)
print(f"  {stem:24} 总分={d['total_final']:>3}  规则判0={n('rule_no_evidence'):>2}  "
      f"Laya采纳={n('laya'):>2}  Qwen仲裁={d['n_qwen']:>2}  pending={d['n_pending']}")
PY
}

if [ -d "$IN" ]; then
  for f in "$IN"/*.txt; do one "$f"; done
else
  one "$IN"
fi
echo "输出目录: $OUT"
