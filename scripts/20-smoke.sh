#!/usr/bin/env bash
# =====================================================================
#  20-smoke.sh — 端到端冒烟验收
#
#  每一步都必须过，才能进下一步。任何一步 "✗" 都别往下走。
#  用法： bash 20-smoke.sh [样例PDF或图片路径]
# =====================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/config.env"
medrec_clean_path

SAMPLE="${1:-}"
PASS=0; FAIL=0
ok()   { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad()  { echo "  ✗ $*"; FAIL=$((FAIL+1)); }
warn() { echo "  ! $*"; }
hdr()  { echo; echo "── $* ─────────────────────────────────────────────"; }

# 先落变量再 grep：`ss | grep -q` 里 grep 匹配到就退出，会让上游 ss 吃到
# SIGPIPE(141)，在 pipefail 下把「正在监听」误报成「未监听」——这种假阴性
# 会让你以为服务没起来，跑去重复启动。
listen()   { local o; o=$(ss -ltn 2>/dev/null); printf '%s\n' "$o" | grep -q ":$1 "; }
http_has() { local o; o=$(curl -s -m "${3:-5}" "$1" 2>/dev/null); printf '%s' "$o" | grep -q "$2"; }

echo "=============================================================="
echo " 冒烟验收 · $(date '+%F %T') · $(hostname)"
echo "=============================================================="

# ---------------------------------------------------------------- 0 环境
hdr "0. 环境与路径"
[ -x "$PY_OCR" ]   && ok "ocr-env   $("$PY_OCR" -V 2>&1)"        || bad "ocr-env 缺失"
[ -x "$PY_LLM" ]   && ok "qwen-env  $("$PY_LLM" -V 2>&1)"        || bad "qwen-env 缺失"
[ -x "$PY_LAYAD" ] && ok "laya-env  $("$PY_LAYAD" -V 2>&1)"      || bad "laya-env 缺失"
echo "$PATH" | tr ':' '\n' | grep -qE 'fsl|freesurfer' \
  && warn "PATH 里仍有 fsl/freesurfer —— 服务用绝对路径，但子进程可能被抢" \
  || ok "PATH 未被 fsl 抢占"

# ---------------------------------------------------------------- 1 GPU
hdr "1. GPU 与显存"
nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu --format=csv,noheader \
  | sed 's/^/  GPU /'
FREE_MIN="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | sort -n | head -1)"
[ "${FREE_MIN:-0}" -gt 20000 ] && ok "最低空闲显存 ${FREE_MIN}MiB，够起 27B-Q4" \
  || warn "最低空闲显存 ${FREE_MIN}MiB —— vLLM 的 gpu-memory-utilization 是**预申请**，不够会直接启动失败而不是排队"
ZOMBIE="$(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader)"
[ -z "$ZOMBIE" ] && ok "无残留占卡进程" || { warn "有残留占卡进程："; echo "$ZOMBIE" | sed 's/^/      /'; }

# ---------------------------------------------------------------- 2 OCR
hdr "2. OCR 前端识别"
if listen "$PORT_OCR_PADDLE"; then
  R="$(curl -s -m 5 -X POST "http://127.0.0.1:$PORT_OCR_PADDLE/layout-parsing" \
       -H 'Content-Type: application/json' -d '{}' 2>/dev/null)"
  echo "$R" | grep -q errorCode && ok "A 路 PaddleX 活着（:$PORT_OCR_PADDLE）" \
                                 || bad "A 路端口在，但 /layout-parsing 没回 errorCode"
else
  warn "A 路 PaddleX 未监听（:$PORT_OCR_PADDLE）"
fi
if listen "$PORT_OCR_VL"; then
  M="$(curl -s -m 5 "http://127.0.0.1:$PORT_OCR_VL/v1/models" 2>/dev/null)"
  echo "$M" | grep -q "$MODEL_OCR_VL_NAME" && ok "B 路 PaddleOCR-VL 活着（:$PORT_OCR_VL）" \
                                           || bad "B 路端口在，但 /v1/models 里没有 $MODEL_OCR_VL_NAME"
else
  warn "B 路 vLLM 未监听（:$PORT_OCR_VL）—— 用 A 路原生产线也可以"
fi

if [ -n "$SAMPLE" ]; then
  if [ -f "$SAMPLE" ]; then
    # --vl-url 只给 base，layered.py 会自己拼 /v1/chat/completions 或 /layout-parsing
    if [ "${VL_BACKEND:-paddle}" = "vllm" ]; then
      VL_BASE="http://127.0.0.1:$PORT_OCR_VL"
    else
      VL_BASE="http://127.0.0.1:$PORT_OCR_PADDLE"
    fi
    OUT="/tmp/smoke_$(date +%s)"; mkdir -p "$OUT"
    medrec_log "跑 layered.py（backend=${VL_BACKEND:-paddle}, vl-url=$VL_BASE）—— 真实识别，可能要几分钟"
    if "$PY_OCR" "$MEDREC_SRC/layered.py" "$SAMPLE" --out-dir "$OUT" \
         --vl-backend "${VL_BACKEND:-paddle}" \
         --vl-url "$VL_BASE" \
         --no-vl-autostart >"$OUT/layered.log" 2>&1; then
      NJ="$(ls "$OUT"/*.json 2>/dev/null | head -1)"
      if [ -n "$NJ" ]; then
        ok "识别产物：$NJ"
        "$PY_LLM" - "$NJ" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
blocks = d[0]["parsing_res_list"] if isinstance(d, list) else d.get("parsing_res_list", [])
print(f"      块数 {len(blocks)}")
tr = [b for b in blocks if b.get("truncated")]
print(f"      截断块 {len(tr)}  {'✓' if not tr else '✗ 表格被静默截断，尾部行已丢'}")
empty = [b for b in blocks if not (b.get('block_content') or '').strip() and b.get('route') != 'placeholder']
print(f"      空块 {len(empty)}  {'✓' if not empty else '✗ 空块必须留痕'}")
PY
        [ "$(grep -c truncated "$NJ" 2>/dev/null || echo 0)" -eq 0 ] && ok "无截断标记" || bad "有截断标记"
      fi
    else
      bad "layered.py 失败，看 $OUT/layered.log"
    fi
  else
    bad "样例文件不存在：$SAMPLE"
  fi
else
  warn "未给样例文件，跳过真实识别（给个路径可跑全链：bash 20-smoke.sh /path/a.pdf）"
fi

# ---------------------------------------------------------------- 3 Qwen
hdr "3. Qwen3.8-27B 后端（数据处理 + 对话）"
if listen "$PORT_QWEN"; then
  http_has "http://127.0.0.1:$PORT_QWEN/v1/models" "$MODEL_QWEN_NAME" \
    && ok "模型已注册：$MODEL_QWEN_NAME" || bad "端口在但模型名不对（期望 $MODEL_QWEN_NAME）"
  T0=$(date +%s%3N)
  RESP="$(curl -s -m 600 "http://127.0.0.1:$PORT_QWEN/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL_QWEN_NAME\",\"messages\":[{\"role\":\"user\",
        \"content\":\"患者男，78岁，突发右侧肢体无力2小时，查体：神志清，右上肢肌力3级。请用一句话给出 NIHSS 上肢项定档。\"}],
        \"max_tokens\":128,\"temperature\":0,\"chat_template_kwargs\":{\"enable_thinking\":false}}")"
  T1=$(date +%s%3N)
  if echo "$RESP" | grep -q '"content"'; then
    ok "对话可用，耗时 $((T1-T0)) ms（首次含 kernel 编译，可能 1~3 分钟）"
    echo "$RESP" | "$PY_LLM" -c "import sys,json;d=json.load(sys.stdin);print('      回答：'+d['choices'][0]['message']['content'][:160].replace(chr(10),' '))" 2>/dev/null || true
  else
    bad "对话调用失败：$(echo "$RESP" | head -c 200)"
  fi
  # 结构化输出（这一条决定 F3 抽取能不能用）
  SRESP="$(curl -s -m 600 "http://127.0.0.1:$PORT_QWEN/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL_QWEN_NAME\",\"messages\":[{\"role\":\"user\",
        \"content\":\"把这句话抽成 JSON：性别男，年龄78岁。字段 gender(string) 与 age(integer)。只输出 JSON。\"}],
        \"max_tokens\":128,\"temperature\":0,
        \"response_format\":{\"type\":\"json_schema\",\"json_schema\":{\"name\":\"p\",\"strict\":true,
        \"schema\":{\"type\":\"object\",\"properties\":{\"gender\":{\"type\":\"string\"},\"age\":{\"type\":\"integer\"}},
        \"required\":[\"gender\",\"age\"],\"additionalProperties\":false}}},
        \"chat_template_kwargs\":{\"enable_thinking\":false}}")"
  if echo "$SRESP" | grep -q '"gender"'; then
    ok "JSON Schema 约束输出可用（F3 抽取的关键能力）"
  else
    warn "response_format 未生效 —— 用 vllm 的 guided_json 或客户端侧 schema 兜底"
  fi
else
  bad "Qwen 未监听 :$PORT_QWEN（先 bash 11-start-qwen.sh）"
fi

# ---------------------------------------------------------------- 4 Laya
hdr "4. Laya 打分前端（NIHSS + 病历快判）"
if listen "$PORT_LAYAD"; then
  H=$(curl -s -m 5 "http://127.0.0.1:$PORT_LAYAD/health" 2>/dev/null)
  printf '%s\n' "$H" | grep -q '"status":"ok"' \
    && ok "Laya 服务活着（:$PORT_LAYAD）" || bad "Laya /health 异常"
  SC="$(curl -s -m 300 "http://127.0.0.1:$PORT_LAYAD/score" -H 'Content-Type: application/json' -d '{
     "text":"查体：神志清，精神可，对答切题。双侧瞳孔等大等圆，对光反射灵敏。额纹对称，伸舌居中，鼻唇沟对称。四肢肌力5级，肌张力无增高减低。双侧Babinski征未引出。NIHSS评分0分。",
     "scale":"nihss_15"}')"
  echo "$SC" | grep -q '"items"' && ok "量表返回结构正常" || bad "量表返回异常：$(echo "$SC" | head -c 200)"
  echo "$SC" | "$PY_LLM" - <<'PY' 2>/dev/null || true
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(f"      Laya 总分 {d.get('total_expected')} / 满分 {d.get('max_possible')}   耗时 {d.get('elapsed_ms')} ms")
print(f"      低置信需升级 {d.get('need_escalate_count')} 项：{d.get('need_escalate')}")
its = d.get("items") or {}
k0 = next(iter(its), None)
if k0:
    print(f"      示例项 {k0}: {json.dumps({x: its[k0].get(x) for x in ('expected','level','confidence','confidence_source')}, ensure_ascii=False)}")
PY
  # 中文长文本必须用 multilingual
  CL="$(curl -s -m 300 "http://127.0.0.1:$PORT_LAYAD/classify" -H 'Content-Type: application/json' \
        -d '{"text":"入院记录。患者因突发右侧肢体无力2小时入院，头颅CT示左侧基底节区低密度灶，诊断：急性脑梗死。予阿替普酶静脉溶栓治疗。"}')"
  echo "$CL" | grep -q '病案类型' && ok "病历快判可用" || warn "病历快判返回异常"
else
  bad "Laya 未监听 :$PORT_LAYAD（先 bash 12-start-laya.sh）"
fi

# ---------------------------------------------------------------- 5 编排
hdr "5. 打分编排（Laya → 低置信升级 Qwen → 合并）"
if listen "$PORT_LAYAD"; then
  "$PY_LLM" "$HERE/22-score-pipeline.py" --selftest && ok "量表自检通过" || bad "量表自检未通过"
  TMP="/tmp/_smoke_score.txt"
  printf '查体：神志清，精神可，对答切题，双侧瞳孔等大等圆，对光反射灵敏。额纹对称，鼻唇沟对称，伸舌居中。右上肢肌力3级，右下肢肌力4级。双侧Babinski征未引出。NIHSS评分4分。\n' > "$TMP"
  if "$PY_LLM" "$HERE/22-score-pipeline.py" --file "$TMP" --biz-id SMOKE --out /tmp/_smoke_nihss.json 2>&1 | tail -25; then
    ok "编排跑通，产物 /tmp/_smoke_nihss.json"
  else
    bad "编排失败"
  fi
fi

# ---------------------------------------------------------------- 汇总
echo
echo "=============================================================="
echo " 通过 $PASS 项 · 失败 $FAIL 项"
if [ "$FAIL" -eq 0 ]; then
  echo " 可以进入业务验证（F3 抽取 / F4 仲裁 / Web 前端）"
else
  echo " 先修失败的项，别往下走"
fi
echo "=============================================================="
exit "$([ "$FAIL" -eq 0 ] && echo 0 || echo 1)"
