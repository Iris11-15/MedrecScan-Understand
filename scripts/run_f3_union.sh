#!/usr/bin/env bash
set -uo pipefail
D="${1:?usage: run_f3_union.sh <compiled_dir> [rounds]}"
N="${2:-3}"
PY=/home/ubuntu/anaconda3/envs/medrec/bin/python
SRC=/home/ubuntu/medrec/src
TMP=$(mktemp -d)
for i in $(seq 1 "$N"); do
  printf '  round %d/%d ... ' "$i" "$N"
  "$PY" "$SRC/structure.py" "$D" --url http://127.0.0.1:8102 --model qwen-27b \
    --schema "$SRC/f3_schema.json" --system "$SRC/f3_system.txt" \
    --max-chars 2800 --max-tokens 16000 --jobs 8 --out "$TMP/u$i.json" 2>&1 | tail -1
done
"$PY" "$SRC/union_merge.py" "$D" "$TMP" "$N"
rm -rf "$TMP"
