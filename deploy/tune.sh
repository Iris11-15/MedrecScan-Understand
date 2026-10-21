#!/usr/bin/env bash
# tune.sh —— round52 Part B：只读探测，唯一输出 tuning.json（brief §5）。
# 边界（brief §5.1 / §6）：不改 config/、不改 scripts/config.env、不起/不停/不重启任何服务。
# 只读自证：跑前跑后对 config/ 与 scripts/config.env 做 md5sum 快照，不一致 -> rc=3。
set -euo pipefail

MEDREC_ROOT="${MEDREC_ROOT:-$HOME/medrec}"
PY="${MEDREC_PY:-/home/ubuntu/anaconda3/envs/vl-env/bin/python}"
OUT_JSON="${TUNE_OUT:-$MEDREC_ROOT/workspace/tuning.json}"
P_STRATA="${PORT_STRATA:-8080}"
P_VLLM="${PORT_OCR_VL:-8110}"
PROBE_IMG="${TUNE_PROBE_IMG:-$MEDREC_ROOT/smoke/probe_page.png}"
OCR_MODEL_NAME="${TUNE_OCR_MODEL:-PaddleOCR-VL}"
L2_N="${TUNE_L2_N:-10}"
L2_MAX_TOKENS="${TUNE_MAX_TOKENS:-3072}"
LEVELS="L0,L1,L2"
SWEEP=0
SWEEP_ARMS="${SWEEP_ARMS:-16x16,24x32,32x48}"
SWEEP_CASES="${SWEEP_CASES:-100}"
SWEEP_MIN_CASES="${SWEEP_MIN_CASES:-20}"
SWEEP_DIR="${SWEEP_DIR:-$MEDREC_ROOT/workspace/tune-sweep}"
BENCH_DIR="${BENCH_DIR:-}"
FORCE_PROFILE="${TUNE_PROFILE:-}"
TODAY="$(date +%Y%m%d)"

usage() {
  printf "%s\n" \
    "用法: tune.sh [--level L0,L1,L2] [--sweep] [--out FILE] [--help]" \
    "" \
    "  --level LIST     要跑的层，逗号分隔（默认 L0,L1,L2；L3 不在默认里）" \
    "  --sweep          显式开启 L3 吞吐扫描（默认不跑：一次 20-60 min，brief §5.2）" \
    "  --out FILE       tuning.json 落点（默认 MEDREC_ROOT/workspace/tuning.json）" \
    "  --bench-dir D    L3 的病历目录（院区/病例/*.pdf）；不给则 L3 跳过并说明原因" \
    "  --arms LIST      L3 扫描臂，形如 KxC（R15_CASE_PARALLEL x R23_OCR_CONCURRENCY），默认 16x16,24x32,32x48" \
    "  --cases N        L3 每臂例数上限（默认 100）" \
    "  --min-cases N    采信一个臂所需最少例数（默认 20；不足则该臂不进 tuning.json）" \
    "  --profile NAME   强制指纹名（4090-4x / l40-2x / other），用于对照与演示" \
    "  --help           本帮助" \
    "" \
    "只读自证：跑前跑后对 config/ 与 scripts/config.env 做 md5sum 快照，不一致就报错退出（rc=3）。" \
    "本脚本不起服务、不停服务、不重启服务、不改任何配置（brief §5.1）。"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --level) LEVELS="$2"; shift 2 ;;
    --level=*) LEVELS="${1#--level=}"; shift ;;
    --sweep) SWEEP=1; shift ;;
    --out) OUT_JSON="$2"; shift 2 ;;
    --out=*) OUT_JSON="${1#--out=}"; shift ;;
    --bench-dir) BENCH_DIR="$2"; shift 2 ;;
    --bench-dir=*) BENCH_DIR="${1#--bench-dir=}"; shift ;;
    --arms) SWEEP_ARMS="$2"; shift 2 ;;
    --arms=*) SWEEP_ARMS="${1#--arms=}"; shift ;;
    --cases) SWEEP_CASES="$2"; shift 2 ;;
    --cases=*) SWEEP_CASES="${1#--cases=}"; shift ;;
    --min-cases) SWEEP_MIN_CASES="$2"; shift 2 ;;
    --min-cases=*) SWEEP_MIN_CASES="${1#--min-cases=}"; shift ;;
    --profile) FORCE_PROFILE="$2"; shift 2 ;;
    --profile=*) FORCE_PROFILE="${1#--profile=}"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf "[FAIL] 未知参数：%s\n" "$1" >&2; usage; exit 2 ;;
  esac
done

has() { case ",$LEVELS," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

snap() {
  find "$MEDREC_ROOT/config" -type f 2>/dev/null | LC_ALL=C sort | xargs -r md5sum
  md5sum "$MEDREC_ROOT/scripts/config.env" 2>/dev/null || true
}
SNAP_BEFORE="$(snap)"
printf "[..] 只读快照（跑前）：config/ + scripts/config.env 共 %s 个文件\n" "$(printf "%s\n" "$SNAP_BEFORE" | grep -c .)"

echo "== L0 指纹 =="
GPU_ROWS="$(nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv,noheader 2>/dev/null || true)"
GPU_COUNT=0; GPU_NAME="unknown"; VRAM_MIB=0; BW=0
if [ -z "$GPU_ROWS" ]; then
  printf "[FAIL] nvidia-smi 无输出：驱动没装或本机没有 NVIDIA 卡 —— L0 无法定指纹\n" >&2
else
  printf "%s\n" "$GPU_ROWS"
  GPU_COUNT="$(printf "%s\n" "$GPU_ROWS" | grep -c .)"
  GPU_NAME="$(printf "%s\n" "$GPU_ROWS" | head -1 | cut -d, -f2 | sed "s/^ //")"
  VRAM_MIB="$(printf "%s\n" "$GPU_ROWS" | cut -d, -f3 | tr -dc "0-9\n" | sort -n | head -1)"
  case "$GPU_NAME" in
    *"RTX 4090"*) BW=1008 ;;
    *L40S*) BW=864 ;;
    *L40*) BW=864 ;;
    *) BW=0 ;;
  esac
fi
NPROC="$(nproc 2>/dev/null || echo 0)"
DISK_GB="$(df -BG --output=avail "$MEDREC_ROOT" 2>/dev/null | tail -1 | tr -dc "0-9")"
DISK_GB="${DISK_GB:-0}"
if [ -n "$FORCE_PROFILE" ]; then
  PROFILE="$FORCE_PROFILE"
  printf "[..] 指纹被 --profile 强制为 %s（对照/演示用）\n" "$PROFILE"
else
  PROFILE="other"
  case "$GPU_NAME" in
    *"RTX 4090") if [ "$GPU_COUNT" = 4 ]; then PROFILE="4090-4x"; fi ;;
    *L40*) if [ "$GPU_COUNT" = 2 ]; then PROFILE="l40-2x"; fi ;;
  esac
fi
if [ "$BW" = 0 ]; then
  printf "[WARN] 卡型号 %s 不在带宽表里（表内只有 RTX 4090=1008 GB/s、L40/L40S=864 GB/s）⇒ L2 只能给绝对阈值（>5ms 判机器有问题），给不出预期 TPOT\n" "$GPU_NAME"
else
  printf "[OK] 指纹：%s x %s（%s MiB/卡，显存带宽 %s GB/s）｜CPU %s 核｜%s 空闲 %s GB\n" "$GPU_NAME" "$GPU_COUNT" "$VRAM_MIB" "$BW" "$NPROC" "$MEDREC_ROOT" "$DISK_GB"
fi
printf "[..] profile = %s\n" "$PROFILE"

# brief §5.4 卡型号查找表。L40x2 在 brief 里是「待测」⇒ 只给保守默认，source 记 default。
SRC_TBL=""; SGPU="1"; KV_RES=32768
R15=24; R23=32; PCONC=16
case "$PROFILE" in
  4090-4x) SRC_TBL="table:4090-4x"; SGPU="2,3" ;;
  l40-2x)  printf "[..] l40-2x 在 brief §5.4 里是「待测」：先按保守默认 R15=24 R23=32，跑 --sweep 才会变成实测值\n" ;;
  *)       printf "[..] profile=%s 不在查找表里：按 brief §5.4「其他」行给保守默认 R15=24 R23=32\n" "$PROFILE" ;;
esac

model_id() {
  local body="$1"
  printf "%s" "$body" | "$PY" -c "import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    print(\"\")
    raise SystemExit(0)
x=d.get(\"data\") or []
print(x[0].get(\"id\",\"(no id)\") if x else \"(empty)\")" 2>/dev/null || true
}

echo "== L1 服务自检 =="
L1_FAIL=0
check_service() { # $1=端口 $2=名称 $3=模型名期望片段
  local port="$1" name="$2" want="$3" body id
  body="$(curl -s --max-time 5 "http://127.0.0.1:${port}/v1/models" || true)"
  if [ -z "$body" ]; then
    printf "[FAIL] %s 在端口 %s 无响应 —— 先跑 start-all.sh（该服务现在没起）\n" "$name" "$port"
    return 1
  fi
  id="$(model_id "$body")"
  if [ -z "$id" ]; then
    printf "[FAIL] %s 端口 %s 有响应但解析不出 model id（前 120 字节：%s）\n" "$name" "$port" "$(printf "%s" "$body" | head -c 120)"
    return 1
  fi
  if printf "%s" "$id" | grep -qi "$want"; then
    printf "[OK] %s 端口 %s model=%s（期望含 %s）\n" "$name" "$port" "$id" "$want"
    return 0
  fi
  printf "[FAIL] %s 端口 %s model=%s 与期望 %s 不符 —— 产线按期望名发请求，这会直接 404/400\n" "$name" "$port" "$id" "$want"
  return 1
}
if has L1; then
  check_service "$P_VLLM" vLLM "$OCR_MODEL_NAME" || L1_FAIL=$((L1_FAIL+1))
  check_service "$P_STRATA" Strata "qwen3.8-flash-next" || L1_FAIL=$((L1_FAIL+1))
else
  printf "[SKIP] L1 未选（--level）\n"
fi

printf "== L2 单点基准（串行 %s 次，max_tokens=%s）==\n" "$L2_N" "$L2_MAX_TOKENS"
L2_TPOT=""; L2_MEAN_S=""; L2_SERIAL_REQ_S=""; L2_NOK=0
if ! has L2; then
  printf "[SKIP] L2 未选（--level）\n"
elif [ ! -f "$PROBE_IMG" ]; then
  printf "[FAIL] L2 探针图不存在：%s\n" "$PROBE_IMG" >&2
  printf "       它在 medrec-assets 包里的 smoke/probe_page.png，装完应在 MEDREC_ROOT/smoke/。\n" >&2
elif ! curl -s --max-time 5 "http://127.0.0.1:${P_VLLM}/v1/models" >/dev/null 2>&1; then
  printf "[FAIL] L2 需要 %s 在跑，现在没在跑。tune.sh 不起服务（brief §5.1）—— 先跑 start-all.sh 再来。\n" "$P_VLLM" >&2
else
  B64="$(base64 -w0 "$PROBE_IMG")"
  printf "[..] 探针图 %s（%s 字节 → base64 %s 字符）\n" "$PROBE_IMG" "$(stat -c %s "$PROBE_IMG")" "${#B64}"
  # base64 后的 body 有 ~180 KB：超过 Linux 单个 argv 上限 MAX_ARG_STRLEN=128 KiB
  # ⇒ 必须走 --data @file，不能 --data "$body"（否则 curl 直接 E2BIG，请求根本没发出去）
  BODY_FILE="${TMPDIR:-/tmp}/medrec-tune-l2-body-$$.json"
  sum_t=0; sum_tpot=0; sum_tok=0; n_ok=0
  for i in $(seq 1 "$L2_N"); do
    printf "{\"model\":\"%s\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/png;base64,%s\"}},{\"type\":\"text\",\"text\":\"OCR:\"}]}],\"max_tokens\":%s,\"temperature\":0,\"stream\":true,\"stream_options\":{\"include_usage\":true}}" "$OCR_MODEL_NAME" "$B64" "$L2_MAX_TOKENS" > "$BODY_FILE"
    resp="$(curl -sS --max-time 180 -H "Content-Type: application/json" --data @"$BODY_FILE" -w "\n@@T=%{time_total} F=%{time_starttransfer}@@" "http://127.0.0.1:${P_VLLM}/v1/chat/completions" 2>/dev/null || true)"
    line_t="$(printf "%s" "$resp" | tail -1 || true)"
    tt="$(printf "%s" "$line_t" | sed -n "s/.*T=\([0-9.]*\).*/\1/p" || true)"
    tf="$(printf "%s" "$line_t" | sed -n "s/.*F=\([0-9.]*\).*/\1/p" || true)"
    tok="$(printf "%s" "$resp" | sed -n "s/.*completion_tokens[\": ]*\([0-9][0-9]*\).*/\1/p" | tail -1 || true)"
    if [ -z "$tt" ] || [ -z "$tf" ] || [ -z "$tok" ]; then
      printf "  [%02d] [FAIL] 拿不到计时或 usage —— 响应前 160 字节：%s\n" "$i" "$(printf "%s" "$resp" | head -c 160 | tr "\n" " ")"
      continue
    fi
    tpot="$(awk -v t="$tt" -v f="$tf" -v n="$tok" "BEGIN{ if (n>1) printf \"%.3f\", (t-f)/(n-1)*1000; else printf \"%.3f\", 0 }")"
    printf "  [%02d] 端到端 %ss  TTFT %ss  生成 %s tok  TPOT %s ms\n" "$i" "$tt" "$tf" "$tok" "$tpot"
    sum_t="$(awk -v a="$sum_t" -v b="$tt" "BEGIN{printf \"%.6f\", a+b}")"
    sum_tpot="$(awk -v a="$sum_tpot" -v b="$tpot" "BEGIN{printf \"%.6f\", a+b}")"
    sum_tok=$((sum_tok + tok))
    n_ok=$((n_ok + 1))
  done
  L2_NOK="$n_ok"
  rm -f "$BODY_FILE" 2>/dev/null || true
  if [ "$n_ok" = 0 ]; then
    printf "[FAIL] L2 %s 次全失败：%s 在跑但请求没成功，看 %s 里的 vLLM 日志\n" "$L2_N" "$P_VLLM" "$MEDREC_ROOT/logs" >&2
  else
    L2_TPOT="$(awk -v s="$sum_tpot" -v n="$n_ok" "BEGIN{printf \"%.3f\", s/n}")"
    L2_MEAN_S="$(awk -v s="$sum_t" -v n="$n_ok" "BEGIN{printf \"%.3f\", s/n}")"
    L2_SERIAL_REQ_S="$(awk -v n="$n_ok" -v s="$sum_t" "BEGIN{printf \"%.3f\", n/s}")"
    EXP=""
    if [ "$BW" != 0 ]; then EXP="$(awk -v b="$BW" "BEGIN{printf \"%.2f\", 1800/b}")"; fi
    printf "[..] 均值：TPOT %s ms｜单请求 %ss｜串行吞吐 %s req/s｜生成 %s tok（%s/%s 次成功）\n" "$L2_TPOT" "$L2_MEAN_S" "$L2_SERIAL_REQ_S" "$sum_tok" "$n_ok" "$L2_N"
    if [ -n "$EXP" ]; then printf "[..] 预期 TPOT = 1800/%s = %s ms（brief §5.3：1.8 GB 权重 / 显存带宽）\n" "$BW" "$EXP"; fi
    if awk -v t="$L2_TPOT" "BEGIN{exit !(t < 1.5)}"; then
      printf "[WARN] 判定：TPOT %s ms < 1.5 ms —— 要么模型比预期小，要么测错了。这个数不能拿去调参，先查探针图与 max_tokens。\n" "$L2_TPOT"
    elif awk -v t="$L2_TPOT" "BEGIN{exit !(t > 5)}"; then
      printf "[FAIL] 判定：TPOT %s ms > 5 ms —— 机器有问题（驱动 / 时钟 / 带宽没吃满），先修机器，别调参（brief §5.3）。\n" "$L2_TPOT"
    elif [ -z "$EXP" ]; then
      printf "[WARN] 判定：卡型号不在带宽表里，给不出预期区间；%s ms 落在 1.5-5 ms 之间，只能算「没爆表」。\n" "$L2_TPOT"
    elif awk -v t="$L2_TPOT" -v e="$EXP" "BEGIN{exit !(t <= e*1.15)}"; then
      printf "[OK] 判定：TPOT %s ms 落在预期区间（预期 %s ms，容差 15%%）—— 这台机器正常，可以往下调参。\n" "$L2_TPOT" "$EXP"
    else
      printf "[WARN] 判定：TPOT %s ms 高于预期 %s ms 但 < 5 ms —— 带宽没吃满或在排队。先看 nvidia-smi 利用率/时钟，再谈调参。\n" "$L2_TPOT" "$EXP"
    fi
  fi
fi

echo "== L3 吞吐扫描 =="
L3_BEST_ARM=""; L3_BEST_S=""; L3_BEST_N=0; BEST_K=""; BEST_C=""
if [ "$SWEEP" != 1 ]; then
  printf "[SKIP] L3 默认不跑（brief §5.2：一次 20-60 min）。要跑：--sweep --bench-dir <病历目录>\n"
elif [ -z "$BENCH_DIR" ] || [ ! -d "$BENCH_DIR" ]; then
  printf "[FAIL] --sweep 已开但 --bench-dir 无效（%s）：L3 要真实病历目录（院区/病例/*.pdf）才能测端到端 s/例 ⇒ 本轮跳过 L3\n" "${BENCH_DIR:-<未给>}"
else
  printf "[..] 注意：L3 会在 %s 下另起批次（brief §6 说 tune.sh 只写 tuning.json，\n" "$SWEEP_DIR"
  printf "       但 §5.2 的 L3 必须真跑批才能测出 s/例 —— 产物全部隔离在 tune-sweep/，不碰正式 run_root）\n"
  mkdir -p "$SWEEP_DIR"
  CASES_FILE="$SWEEP_DIR/cases-$TODAY.txt"
  ( cd "$MEDREC_ROOT/src" && "$PY" -u -c "import sys
from pathlib import Path
sys.path.insert(0, \".\")
from wide.s1_ocr import discover
rows = discover(Path(sys.argv[1]))
for r in rows[:int(sys.argv[2])]:
    print(r[\"case_id\"])" "$BENCH_DIR" "$SWEEP_CASES" ) > "$CASES_FILE" 2>"$SWEEP_DIR/cases-$TODAY.err" || true
  NLIST="$(grep -c . "$CASES_FILE" 2>/dev/null || echo 0)"
  if [ "$NLIST" = 0 ]; then
    printf "[FAIL] 在 %s 里没发现任何 PDF（discover 报错见 %s）⇒ 跳过 L3\n" "$BENCH_DIR" "$SWEEP_DIR/cases-$TODAY.err"
  else
    printf "[..] 每臂 %s 例（清单 %s）｜臂：%s\n" "$NLIST" "$CASES_FILE" "$SWEEP_ARMS"
    for arm in ${SWEEP_ARMS//,/ }; do
      K="${arm%%x*}"; C="${arm##*x}"
      if ! printf "%s_%s" "$K" "$C" | grep -qE "^[0-9]+_[0-9]+$"; then
        printf "  [FAIL] 臂名 %s 不是 KxC 形式（例 24x32），跳过\n" "$arm"
        continue
      fi
      B="tune-sweep-$TODAY-$arm"
      LOGF="$SWEEP_DIR/$arm.log"
      printf "  [..] 臂 %s：R15_CASE_PARALLEL=%s R23_OCR_CONCURRENCY=%s\n" "$arm" "$K" "$C"
      t0="$(date +%s)"
      set +e
      ( cd "$MEDREC_ROOT" \
        && set -a \
        && { . "$MEDREC_ROOT/scripts/config.env" >/dev/null 2>&1 || true; } \
        && set +a \
        && R15_CASE_PARALLEL="$K" R23_OCR_CONCURRENCY="$C" \
          "$PY" -u bin/medrec_cli.py run "$BENCH_DIR" --batch "$B" --run-root "$SWEEP_DIR" \
              --cases "$CASES_FILE" --no-locate-cache --no-sample > "$LOGF" 2>&1 )
      rc=$?
      set -e
      t1="$(date +%s)"; wall=$((t1 - t0))
      MF="$SWEEP_DIR/$B/00_index/manifest.tsv"
      n=0
      if [ -f "$MF" ]; then n=$(( $(grep -c . "$MF") - 1 )); fi
      if [ "$rc" != 0 ]; then
        printf "  [FAIL] 臂 %s rc=%s 墙钟 %ss 例数 %s —— 日志尾巴：%s\n" "$arm" "$rc" "$wall" "$n" "$(tail -2 "$LOGF" 2>/dev/null | tr "\n" " | ")"
        continue
      fi
      WMETA="$SWEEP_DIR/$B/_medrec/RUN_META.json"
      NWARN="$( "$PY" -u -c "import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print(-1); raise SystemExit(0)
print(len(d.get(\"warnings\") or []))" "$WMETA" 2>/dev/null || echo -1)"
      if [ "$NWARN" != 0 ]; then
        printf "  [WARN] 臂 %s RUN_META.warnings=%s 条（非空则该趟不可信，见 %s）\n" "$arm" "$NWARN" "$WMETA"
      fi
      if [ "$n" -lt "$SWEEP_MIN_CASES" ]; then
        printf "  [WARN] 臂 %s 例数 %s < --min-cases %s ⇒ 不采信（墙钟 %ss）\n" "$arm" "$n" "$SWEEP_MIN_CASES" "$wall"
        continue
      fi
      spv="$(awk -v w="$wall" -v n="$n" "BEGIN{printf \"%.3f\", w/n}")"
      printf "  [OK] 臂 %s：%s 例 / %ss ⇒ %s s/例\n" "$arm" "$n" "$wall" "$spv"
      if [ -z "$L3_BEST_S" ] || awk -v a="$spv" -v b="$L3_BEST_S" "BEGIN{exit !(a<b)}"; then
        L3_BEST_S="$spv"; L3_BEST_ARM="$arm"; L3_BEST_N="$n"; BEST_K="$K"; BEST_C="$C"
      fi
    done
  fi
  if [ -z "$L3_BEST_ARM" ]; then
    printf "[WARN] L3 没有任何臂被采信 ⇒ pipeline 仍用 %s\n" "${SRC_TBL:-default}"
  else
    R15="$BEST_K"; R23="$BEST_C"; SRC_TBL="sweep:$TODAY"
    printf "[OK] L3 最优臂 %s（%s s/例，%s 例）⇒ R15=%s R23=%s，source=sweep:%s\n" "$L3_BEST_ARM" "$L3_BEST_S" "$L3_BEST_N" "$R15" "$R23" "$TODAY"
  fi
fi

echo "== OCR 服务端参数（读 8110 的 /proc/<pid>/cmdline，不看文档）=="
listener_pid() {
  ss -lntpH "sport = :$1" 2>/dev/null | sed -n "s/.*pid=\([0-9]*\).*/\1/p" | head -1
}
GMU=""; MNS=""; MNB=""; OCR_SRC="默认（brief §5.5：0.22 / 64 / 16384）"
PID8110="$(listener_pid "$P_VLLM" || true)"
if [ -n "$PID8110" ] && [ -r "/proc/$PID8110/cmdline" ]; then
  ARGV8110="$(tr "\0" " " < "/proc/$PID8110/cmdline")"
  GMU="$(printf "%s" "$ARGV8110" | sed -n "s/.*--gpu-memory-utilization \([0-9.]*\).*/\1/p")"
  MNS="$(printf "%s" "$ARGV8110" | sed -n "s/.*--max-num-seqs \([0-9]*\).*/\1/p")"
  MNB="$(printf "%s" "$ARGV8110" | sed -n "s/.*--max-num-batched-tokens \([0-9]*\).*/\1/p")"
  OCR_SRC="实测（/proc/$PID8110/cmdline）"
  printf "[OK] 8110 pid=%s\n" "$PID8110"
  printf "[..] argv=%s\n" "$ARGV8110"
else
  printf "[WARN] 读不到端口 %s 的进程 cmdline（服务没在跑 / 权限不足）⇒ 用 brief §5.5 的默认值，source 里标默认\n" "$P_VLLM"
fi
[ -n "$GMU" ] || GMU="0.22"
[ -n "$MNS" ] || MNS="64"
[ -n "$MNB" ] || MNB="16384"
printf "[..] gpu_mem_util=%s｜max_num_seqs=%s｜max_num_batched_tokens=%s（来源：%s）\n" "$GMU" "$MNS" "$MNB" "$OCR_SRC"
if [ "$VRAM_MIB" -ge 40000 ] 2>/dev/null && awk -v g="$GMU" "BEGIN{exit !(g > 0.25)}"; then
  printf "[WARN] 陷阱2：--gpu-memory-utilization %s 在 %s MiB 卡上 = %s GB，白占显存（brief §3.5 要求 0.2-0.25）。tune.sh 不改，只报；落地由人执行。\n" "$GMU" "$VRAM_MIB" "$(awk -v g="$GMU" -v m="$VRAM_MIB" "BEGIN{printf \"%.1f\", g*m/1024}")"
fi

echo "== 写 tuning.json =="
SRC="$SRC_TBL"
[ -n "$SRC" ] || SRC="default"
jnum() { if [ -z "$1" ]; then printf "null"; else printf "%s" "$1"; fi; }
mkdir -p "$(dirname "$OUT_JSON")"
{
  printf "{\n"
  printf "  \"fingerprint\": { \"gpu\": \"%s\", \"count\": %s, \"vram_mib\": %s, \"bandwidth_gbps\": %s, \"nproc\": %s, \"disk_free_gb\": %s },\n" "$GPU_NAME" "$GPU_COUNT" "$VRAM_MIB" "$BW" "$NPROC" "$DISK_GB"
  printf "  \"profile\": \"%s\",\n" "$PROFILE"
  printf "  \"measured\": { \"tpot_ms\": %s, \"single_req_s\": %s, \"ocr_req_s\": %s },\n" "$(jnum "$L2_TPOT")" "$(jnum "$L2_MEAN_S")" "$(jnum "$L2_SERIAL_REQ_S")"
  printf "  \"ocr\": { \"max_num_batched_tokens\": %s, \"gpu_mem_util\": %s, \"max_num_seqs\": %s },\n" "$MNB" "$GMU" "$MNS"
  printf "  \"strata\": { \"gpu\": [%s], \"kv_resident\": %s },\n" "$SGPU" "$KV_RES"
  printf "  \"pipeline\": { \"R15_CASE_PARALLEL\": %s, \"R23_OCR_CONCURRENCY\": %s, \"R23_PROBE_CONCURRENCY\": %s },\n" "$R15" "$R23" "$PCONC"
  printf "  \"source\": \"%s\"\n" "$SRC"
  printf "}\n"
} > "$OUT_JSON"
chmod 600 "$OUT_JSON"
printf "[OK] %s（source=%s）\n" "$OUT_JSON" "$SRC"
printf "[..] 落地由人执行：pipeline 两个键写进 scripts/config.env，ocr 三个键写进 vLLM 启动参数。tune.sh 自己一个配置都不改。\n"

SNAP_AFTER="$(snap)"
if [ "$SNAP_BEFORE" != "$SNAP_AFTER" ]; then
  printf "[FAIL] 只读自证失败：config/ 或 scripts/config.env 在本次运行中被改动\n" >&2
  diff <(printf "%s\n" "$SNAP_BEFORE") <(printf "%s\n" "$SNAP_AFTER") >&2 || true
  exit 3
fi
printf "[OK] 只读自证：config/ + scripts/config.env 跑前后 md5 逐文件一致（%s 个文件）\n" "$(printf "%s\n" "$SNAP_AFTER" | grep -c .)"
if [ "$L1_FAIL" != 0 ]; then
  printf "[WARN] 本轮 L1 有 %s 项不通过 —— tuning.json 已写，但那些数值不可用于调参\n" "$L1_FAIL"
fi
