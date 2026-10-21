#!/usr/bin/env bash
# F3 结构化抽取：并发快跑 + 失败块自动串行补跑（实测最优）
# 用法: run_f3.sh <compiled_dir> [额外 structure.py 参数...]
set -uo pipefail
D="${1:?用法: run_f3.sh <compiled_dir>}"
shift
PY=/home/ubuntu/anaconda3/envs/medrec/bin/python
SRC=/home/ubuntu/medrec/src
COMMON=(--url http://127.0.0.1:8102 --model qwen-27b
        --schema "$SRC/f3_schema.json" --system "$SRC/f3_system.txt"
        --max-chars 2800 --max-tokens 5000)

# 阶段 1：并发 8
"$PY" "$SRC/structure.py" "$D" "${COMMON[@]}" --jobs 8 "$@" 2>&1 | tail -3

# 阶段 2：失败块串行补跑（最多 3 轮）
for i in 1 2 3; do
  F=$("$PY" - "$D" <<'PY'
import json,sys,os
p=os.path.join(sys.argv[1],'structured.json')
try: d=json.load(open(p,encoding='utf-8'))
except Exception: print(0); raise SystemExit
print(sum(1 for c in d.get('chunks') or [] if not c.get('ok')))
PY
)
  [ "$F" = "0" ] && { echo "  ✓ 全部成功（补跑 $((i-1)) 轮）"; break; }
  echo "  ↻ 第 $i 轮补跑：还剩 $F 块"
  "$PY" "$SRC/structure.py" "$D" "${COMMON[@]}" --jobs 1 --resume "$@" 2>&1 | tail -2
done
