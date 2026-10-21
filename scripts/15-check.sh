#!/usr/bin/env bash
# =====================================================================
#  15-check.sh — 分阶段检查器：每个环节随时可以单独查看
#
#  设计原则：
#    · 全部只读 —— 不改任何状态，可以在任何时刻跑，跑多少次都行
#    · 阶段号与部署步骤一一对应（s1 对应 01-setup-envs.sh，依此类推）
#    · 每一项都给「怎么查 / 期望值 / 常见不符」三样，而不是只说 OK/NG
#
#  用法：
#    bash 15-check.sh              # 从头到尾检查所有阶段（默认）
#    bash 15-check.sh s1           # 只查环境
#    bash 15-check.sh s2 s3        # 查指定的几个
#    bash 15-check.sh --list       # 列出所有阶段
#    bash 15-check.sh --log qwen   # 看某路服务的日志（tail）
# =====================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/config.env"
medrec_clean_path

PASS=0; WARN=0; FAIL=0
ok()   { echo "    ✓ $*"; PASS=$((PASS+1)); }
warn() { echo "    ! $*"; WARN=$((WARN+1)); }
bad()  { echo "    ✗ $*"; FAIL=$((FAIL+1)); }
info() { echo "      $*"; }
hdr()  { printf '\n\033[1m── %s ──\033[0m\n' "$*" 2>/dev/null || printf '\n── %s ──\n' "$*"; }

has()   { command -v "$1" >/dev/null 2>&1; }
listen(){ local o; o=$(ss -ltn 2>/dev/null); printf '%s\\n' "$o" | grep -q ":$1 "; }
# 在文件里找某个包（不打印内容，只回退出码）
pkg_of() { "$1/bin/python" -m pip show "$2" >/dev/null 2>&1; }
ver_of() { "$1/bin/python" -c "import $2,sys;print(getattr($2,'__version__','?'))" 2>/dev/null; }

STAGES=(s0 s1 s2 s3 s4 s5 s6 s7 s8)
NAME_s0="代理与 conda 源（clear-proxy.sh）"
NAME_s1="环境与门禁（01-setup-envs.sh）"
NAME_s2="模型权重（02-download-models.sh）"
NAME_s3="OCR 前端识别（10-start-ocr.sh）"
NAME_s4="Qwen3.8-27B 后端（11-start-qwen.sh）"
NAME_s5="Laya 打分前端（12-start-laya.sh）"
NAME_s6="打分编排与量表（22-score-pipeline.py）"
NAME_s7="机器级风险（显存/Xorg/oomd）"
NAME_s8="常驻与运维（systemd / 日志 / fd）"

# ------------------------------------------------------------------ 参数
[[ "${1:-}" == "--list" ]] && { for s in "${STAGES[@]}"; do printf "  %-4s %s\n" "$s" "${NAME_$s}"; done; exit 0; }

if [[ "${1:-}" == "--log" ]]; then
  case "${2:-}" in
    qwen)  f="$MEDREC_LOGS/qwen.log" ;;
    ocr)   f="$MEDREC_LOGS/ocr_paddle.log" ;;
    ocrvl) f="$MEDREC_LOGS/ocr_vllm.log" ;;
    laya)  f="$MEDREC_LOGS/laya.log" ;;
    *)     echo "用法：bash 15-check.sh --log {qwen|ocr|ocrvl|laya}"; exit 2 ;;
  esac
  [ -f "$f" ] || { echo "日志不存在：$f（服务还没起过？）"; exit 1; }
  echo "===== $f （最后 60 行，只读）====="
  tail -60 "$f"
  echo
  echo "----- 常见真因速查（在日志里搜这些关键词）-----"
  for k in 'EngineCore' 'Traceback' 'ninja' 'nvcc' 'out of memory' 'not supported' 'flashinfer'; do
    n=$(grep -ci -- "$k" "$f" 2>/dev/null || echo 0)
    printf "    %-16s %s 次\n" "$k" "$n"
  done
  exit 0
fi

RUN=("$@")
[ "${#RUN[@]}" -eq 0 ] && RUN=("${STAGES[@]}")

for s in "${RUN[@]}"; do
  if ! printf '%s\n' "${STAGES[@]}" | grep -qx "$s"; then
    info "未知阶段 $s（用 --list 看可选项）"; continue
  fi
  case "$s" in

# ==================================================================== s0
s0)
  hdr "$s · ${NAME_s0}"
  # 代理：直接复用 clear-proxy.sh 的诊断，避免两套口径
  if [ -f "$HERE/clear-proxy.sh" ]; then
    out=$(bash "$HERE/clear-proxy.sh" --check 2>&1); rc=$?
    printf '%s\n' "$out" | grep -E '^  (✓|!|✗)|^ 结论' | sed 's/^/    /'
    case "$rc" in
      0) ok "代理环境干净" ;;
      1) warn "有代理配置（下载正常则无妨；卡住就 source clear-proxy.sh --clear）" ;;
      *) bad "no_proxy 有问题 → 修好再跑（否则连本机 8100/8102 都会被塞进代理）" ;;
    esac
  else
    warn "同目录缺 clear-proxy.sh，跳过代理检查"
  fi
  # conda 源与 solver：这三个是「conda create 卡死」的全部原因
  echo "    --- conda 源与 solver ---"
  local_d=$(curl -s -o /dev/null -w '%{http_code}' -m 8 https://repo.anaconda.com/pkgs/main/noarch/repodata.json 2>/dev/null || echo 000)
  local_m=$(curl -s -o /dev/null -w '%{http_code}' -m 8 https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/noarch/repodata.json 2>/dev/null || echo 000)
  [ "$local_m" = "200" ] && ok "tuna 镜像可达（$local_m）" || bad "tuna 镜像不可达（$local_m）→ conda 会卡"
  [ "$local_d" = "200" ] && info "默认源 repo.anaconda.com 可达（$local_d），但它慢；建议 default_channels 也换镜像" \
                         || warn "默认源不可达（$local_d）→ 必须把 default_channels 换镜像"
  if command -v conda >/dev/null 2>&1; then
    sv=$(conda config --show solver 2>/dev/null | awk '{print $2}')
    [ "$sv" = "libmamba" ] && ok "solver=libmamba" \
                           || bad "solver=$sv（经典 solver 解 python=3.12 可慢几十倍）→ conda install -n base -y conda-libmamba-solver && conda config --set solver libmamba"
    if grep -qE 'restore_free_channel|pkgs/free' "$HOME/.condarc" 2>/dev/null; then
      bad "~/.condarc 里有 free 频道（restore_free_channel 或 pkgs/free）—— 这是 conda create 卡死头号原因"
      grep -inE 'restore_free_channel|pkgs/free' "$HOME/.condarc" | sed 's/^/      /'
    else
      ok "无 free 频道配置"
    fi
    # 频道列表重复声明是配置被追加过的痕迹
    n=$(grep -c '^channels:' "$HOME/.condarc" 2>/dev/null || echo 0)
    [ "${n:-0}" -le 1 ] && ok "channels 未被重复声明" || warn "~/.condarc 里出现 $n 次 'channels:'（配置被追加过，列表翻倍）"
  fi
  ;;

# ==================================================================== s1
s1)
  hdr "$s · ${NAME_s1}"
  for pair in "ocr-env:$PY_OCR:paddle" "qwen-env:$PY_LLM:vllm" "laya-env:$PY_LAYAD:laya"; do
    IFS=':' read -r label py mod <<< "$pair"
    if [ ! -x "$py" ]; then bad "$label 不存在（$py）"; continue; fi
    v=$(ver_of "$(dirname "$(dirname "$py")")" "$mod")
    if [ -n "$v" ]; then ok "$label  python=$("$py" -V 2>&1 | awk '{print $2}')  $mod=$v"
    else bad "$label 存在但 import $mod 失败"; fi
  done
  # 三道门禁
  if [ -x "$PY_OCR" ]; then
    n=$("$PY_OCR" -c "import paddle;print(paddle.device.cuda.device_count())" 2>/dev/null || echo 0)
    [ "${n:-0}" -ge 1 ] && ok "paddle 看到 ${n} 张卡" || bad "paddle 看不到卡（装成 CPU 版？别用 --no-deps 重装）"
    "$PY_OCR" -m pip show nvidia-cublas-cu12 >/dev/null 2>&1 && ok "nvidia-*-cu12 轮子在位" \
      || warn "没查到 nvidia-cublas-cu12 —— 若 GPU 失效就是这个原因"
    # ★ 服务化插件：paddlex 的服务化能力是单独装的（paddlex --install serving），不是 pip extra
    if "$PY_OCR" -c "from paddlex.utils.deps import require_serving_plugin; require_serving_plugin()" >/dev/null 2>&1; then
      ok "PaddleX serving 插件在位（A 路能起）"
    else
      bad "PaddleX serving 插件缺失 → 'paddlex --serve' 会抛 DependencyError"
      info "修复： $PY_OCR -m paddlex --install serving"
    fi
  fi
  if [ -x "$PY_LLM" ]; then
    "$PY_LLM" - <<'PY'
import sys
from packaging.version import Version
import torch, transformers
print(f"      torch={torch.__version__} cuda={torch.cuda.is_available()}")
print(f"      transformers={transformers.__version__}")
if not torch.cuda.is_available(): print("      ✗ torch 看不到 CUDA"); sys.exit(1)
if Version(transformers.__version__) < Version("5.8.0"): print("      ✗ transformers < 5.8.0"); sys.exit(1)
PY
    [ $? -eq 0 ] && ok "qwen-env 版本门禁通过" || bad "qwen-env 版本门禁未过（见上面两行）"
    [ -x "$ENV_LLM/bin/ninja" ] && ok "ninja 在位（JIT 子进程找得到）" || warn "env/bin 里没有 ninja → pip install ninja"
  fi
  echo "$PATH" | tr ':' '\n' | grep -qE 'fsl|freesurfer' \
    && warn "PATH 里仍有 fsl/freesurfer（服务用绝对路径不受影响，但子进程可能被抢）" \
    || ok "PATH 未被 fsl 抢占"
  ;;

# ==================================================================== s2
s2)
  hdr "$s · ${NAME_s2}"
  # OCR 权重
  if [ -f "$MODEL_OCR_VL_PATH/config.json" ]; then
    sz=$(du -sm "$MODEL_OCR_VL_PATH" 2>/dev/null | cut -f1)
    ok "PaddleOCR-VL-1.6 在位（${sz} MB，有 config.json → vLLM 可读）"
  else
    warn "缺 $MODEL_OCR_VL_PATH（A 路 PaddleX 也能自己拉，不影响）"
  fi
  # Qwen 权重
  if [ -f "$MODEL_QWEN_PATH/config.json" ]; then
    "$PY_LLM" - "$MODEL_QWEN_PATH" <<'PY'
import json,sys,glob,os
p=sys.argv[1]; cfg=json.load(open(p+"/config.json",encoding="utf-8"))
q=cfg.get("quantization_config") or {}
tc=cfg.get("text_config") or cfg
sz=sum(os.path.getsize(f) for f in glob.glob(p+"/*.safetensors"))
print(f"      quant={q.get('quant_method','?')} bits={q.get('bits','?')} group={q.get('group_size','?')}")
print(f"      num_attention_heads={tc.get('num_attention_heads')}  (TP 必须整除它)")
print(f"      weights={sz/2**30:.1f} GiB  分片={len(glob.glob(p+'/*.safetensors'))}")
print(f"      vision_config={'有' if cfg.get('vision_config') or tc.get('vision_config') else '无'}")
PY
    [ $? -eq 0 ] && ok "Qwen3.8-27B 权重在位" || bad "config.json 读不了 → 权重目录不完整"
    # TP 整除性 —— 这是最容易白等一次的坑
    nh=$("$PY_LLM" -c "
import json,sys
tc=json.load(open('$MODEL_QWEN_PATH/config.json',encoding='utf-8')).get('text_config') or {}
print(tc.get('num_attention_heads') or 0)" 2>/dev/null || echo 0)
    if [ "${nh:-0}" -gt 0 ]; then
      [ $((nh % QWEN_TP)) -eq 0 ] && ok "QWEN_TP=$QWEN_TP 整除 ${nh} 个头" \
        || bad "QWEN_TP=$QWEN_TP 不整除 ${nh} 个头 → 起服务必然断言失败（改 1/2/4/8）"
    fi
  else
    bad "缺 $MODEL_QWEN_PATH/config.json → 先跑 02-download-models.sh qwen"
  fi
  # Laya 缓存
  lc=$(du -sm "$MEDREC_MODELS" 2>/dev/null | cut -f1)
  find "$MEDREC_MODELS" -maxdepth 4 -type d -name '*laya*' 2>/dev/null | head -3 | sed 's/^/      /'
  has du && info "MEDREC_MODELS 合计 ${lc} MB"
  ;;

# ==================================================================== s3
s3)
  hdr "$s · ${NAME_s3}"
  if listen "$PORT_OCR_PADDLE"; then
    R=$(curl -s -m 5 -X POST "http://127.0.0.1:$PORT_OCR_PADDLE/layout-parsing" \
        -H 'Content-Type: application/json' -d '{}' 2>/dev/null)
    echo "$R" | grep -q errorCode && ok "A 路 PaddleX 在 $PORT_OCR_PADDLE（/layout-parsing 回 errorCode）" \
                                 || bad "A 路端口在监听，但 /layout-parsing 未回 errorCode（服务还在加载权重？）"
  else
    info "A 路 PaddleX 未监听 $PORT_OCR_PADDLE"
  fi
  if listen "$PORT_OCR_VL"; then
    M=$(curl -s -m 5 "http://127.0.0.1:$PORT_OCR_VL/v1/models" 2>/dev/null)
    echo "$M" | grep -q "$MODEL_OCR_VL_NAME" && ok "B 路 PaddleOCR-VL 在 $PORT_OCR_VL" \
                                            || warn "B 路端口在，但模型名不是 $MODEL_OCR_VL_NAME：$(echo "$M" | head -c 120)"
  else
    info "B 路 vLLM 未监听 $PORT_OCR_VL"
  fi
  # 最近一次识别产物的质量
  j=$(ls -t "$MEDREC_JOBS"/*/raw/blocks.json 2>/dev/null | head -1)
  if [ -n "$j" ]; then
    info "最近产物：$j"
    "$PY_LLM" - "$j" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
b=d if isinstance(d,list) else d.get("parsing_res_list",[])
tot=len(b); tr=[x for x in b if x.get("truncated")]
empty=[x for x in b if not (x.get("block_content") or "").strip() and x.get("route")!="placeholder"]
print(f"      块数={tot} 截断={len(tr)} 空块={len(empty)}")
print(f"      {'✓' if not tr else '✗'} 截断数必须为 0（表格被静默截断会丢尾部行）")
print(f"      {'✓' if not empty else '✗'} 空块必须留痕（不该静默丢弃）")
PY
  else
    info "还没有 raw/blocks.json —— 跑一次 layered.py 才会产生"
  fi
  ;;

# ==================================================================== s4
s4)
  hdr "$s · ${NAME_s4}"
  if listen "$PORT_QWEN"; then
    M=$(curl -s -m 5 "http://127.0.0.1:$PORT_QWEN/v1/models" 2>/dev/null)
    echo "$M" | grep -q "$MODEL_QWEN_NAME" && ok "模型已注册：$MODEL_QWEN_NAME" \
                                          || bad "端口在但模型名不符（期望 $MODEL_QWEN_NAME）"
    T0=$(date +%s%3N)
    R=$(curl -s -m 600 "http://127.0.0.1:$PORT_QWEN/v1/chat/completions" -H 'Content-Type: application/json' \
      -d "{\"model\":\"$MODEL_QWEN_NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"回复：就绪\"}],
           \"max_tokens\":16,\"temperature\":0,\"chat_template_kwargs\":{\"enable_thinking\":false}}" 2>/dev/null)
    T1=$(date +%s%3N)
    echo "$R" | grep -q '"content"' && ok "对话可用（$((T1-T0)) ms；首次含 kernel 编译，1~3 分钟属正常）" \
                                   || bad "对话失败：$(echo "$R" | head -c 160)"
    # schema 约束（决定 F3 抽取能否用）
    S=$(curl -s -m 600 "http://127.0.0.1:$PORT_QWEN/v1/chat/completions" -H 'Content-Type: application/json' \
      -d "{\"model\":\"$MODEL_QWEN_NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"抽成 JSON：性别男，78岁。字段 gender(string)/age(integer)。只输出 JSON。\"}],
           \"max_tokens\":128,\"temperature\":0,
           \"response_format\":{\"type\":\"json_schema\",\"json_schema\":{\"name\":\"p\",\"strict\":true,
           \"schema\":{\"type\":\"object\",\"properties\":{\"gender\":{\"type\":\"string\"},\"age\":{\"type\":\"integer\"}},
           \"required\":[\"gender\",\"age\"],\"additionalProperties\":false}}},
           \"chat_template_kwargs\":{\"enable_thinking\":false}}" 2>/dev/null)
    echo "$S" | grep -q '"gender"' && ok "JSON Schema 约束生效" \
                                   || warn "response_format 未生效 → 退用 guided_json 或客户端侧 schema"
    # 回答里混进思考块是最典型的坑
    echo "$S" | grep -qi '<think' && bad "回答里出现思考块 → 没关 enable_thinking / 没配 --reasoning-parser qwen3" \
                                  || ok "回答里没有思考块"
  else
    info "Qwen 未监听 $PORT_QWEN"
  fi
  [ -f "$MEDREC_LOGS/qwen.log" ] && info "日志：bash 15-check.sh --log qwen"
  ;;

# ==================================================================== s5
s5)
  hdr "$s · ${NAME_s5}"
  if listen "$PORT_LAYAD"; then
    H=$(curl -s -m 5 "http://127.0.0.1:$PORT_LAYAD/health" 2>/dev/null)
    echo "$H" | grep -q '"status":"ok"' && ok "Laya 服务在 $PORT_LAYAD" || bad "/health 异常：$(echo "$H" | head -c 160)"
    echo "$H" | grep -q '"loaded":true' && ok "检查点已加载（preload 生效）" \
                                       || warn "检查点未加载（首请求会卡 7~10 秒）"
    R=$(curl -s -m 300 "http://127.0.0.1:$PORT_LAYAD/score" -H 'Content-Type: application/json' \
      -d '{"text":"查体：神志清，对答切题，双侧瞳孔等大等圆，四肢肌力5级。NIHSS评分0分。","scale":"nihss_15"}' 2>/dev/null)
    if echo "$R" | grep -q '"items"'; then
      ok "量表接口可用"
      echo "$R" | "$PY_LLM" - <<'PY' 2>/dev/null || true
import sys,json
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
its=d.get("items") or {}
print(f"      行数={len(its)} 合计={d.get('total_expected')} 上界={d.get('max_possible')} 耗时={d.get('elapsed_ms')}ms")
print(f"      路由检查点={d.get('model')}（中文必须是 multilingual；english 在中文上会错得很自信）")
k=next(iter(its),None)
if k:
    a=its[k]
    print(f"      置信度来源={a.get('confidence_source')}  ← 若显示「无置信度字段」说明字段名对不上，全部项都会被升级给 Qwen")
src=set()
for v in its.values(): src.add(str(v.get("confidence_source")))
print(f"      全项置信度来源集合：{src}")
PY
    else
      bad "量表接口异常：$(echo "$R" | head -c 200)"
    fi
    C=$(curl -s -m 300 "http://127.0.0.1:$PORT_LAYAD/classify" -H 'Content-Type: application/json' \
      -d '{"text":"入院记录。诊断：急性脑梗死。予阿替普酶静脉溶栓。"}' 2>/dev/null)
    echo "$C" | grep -q '病案类型' && ok "病历快判接口可用" || warn "快判返回异常"
  else
    info "Laya 未监听 $PORT_LAYAD"
  fi
  ;;

# ==================================================================== s6
s6)
  hdr "$s · ${NAME_s6}"
  [ -f "$MEDREC_SRC/laya_forms.json" ] && ok "laya_forms.json 已装入 $MEDREC_SRC" \
                                       || warn "未装入 $MEDREC_SRC（12-start-laya.sh 会复制）"
  [ -f "$MEDREC_SRC/laya_service.py" ] && ok "laya_service.py 已装入" || warn "未装入 $MEDREC_SRC"
  if [ -x "$PY_LLM" ]; then
    out=$("$PY_LLM" "$HERE/22-score-pipeline.py" --selftest 2>&1)
    if echo "$out" | grep -q '✓ 通过'; then
      ok "量表自检通过"
      echo "$out" | grep -E '逐项满配|昏迷特例|满分口径' | head -4 | sed 's/^/      /'
    else
      bad "量表自检未过"; echo "$out" | tail -8 | sed 's/^/      /'
    fi
  fi
  n=$(ls -t "$MEDREC_JOBS"/*/scoring/nihss.json 2>/dev/null | wc -l)
  [ "$n" -gt 0 ] && info "已有 $n 份打分产物；最新：$(ls -t "$MEDREC_JOBS"/*/scoring/nihss.json 2>/dev/null | head -1)" \
                 || info "还没有打分产物（跑一次 22-score-pipeline.py）"
  ;;

# ==================================================================== s7
s7)
  hdr "$s · ${NAME_s7}"
  nvidia-smi --query-gpu=index,memory.total,memory.used,memory.free --format=csv,noheader,nounits \
    | tr -d ' ' | awk -F',' '{b=$2-$3-$4; printf "      GPU%-3s 总 %d 已用 %d 空闲 %d 基线 %d MiB\n",$1,$2,$3,$4,(b<0?0:b)}'
  # 看不见的占卡者
  x=$(nvidia-smi 2>/dev/null | grep -iE 'xorg|gnome-shell|wayland' | awk '{for(i=1;i<=NF;i++) if($i ~ /MiB$/){gsub(/MiB/,"",$i); s+=$i}} END{print s+0}')
  if [ "${x:-0}" -le 64 ]; then ok "图形侧占卡 ${x} MiB → 无活跃图形会话（不用停 gdm）"
  elif [ "${x:-0}" -le 1024 ]; then warn "图形侧占卡 ${x} MiB → 有会话，给 GMU 留余量，别停显示管理器"
  else bad "图形侧占卡 ${x} MiB → 先关桌面里的浏览器/IDE 等重负载程序"; fi
  orph=$(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>/dev/null | grep -c 'Not Found' || true)
  [ "${orph:-0}" -eq 0 ] && ok "无「查不到进程名」的占卡 pid" || bad "有 ${orph} 个僵尸占卡 pid → 查出来按 pid kill -9"
  # oomd
  if systemctl is-active --quiet systemd-oomd 2>/dev/null; then
    st=$(free -m | awk '/^Swap:/{print $3" "$2}')
    su=${st%% *}; stt=${st##* }
    if [ "${stt:-0}" -eq 0 ]; then ok "systemd-oomd active 但无 swap → 默认规则无从触发"
    else
      pct=$(( su * 100 / stt ))
      [ "$pct" -lt 50 ] && ok "oomd active，swap 仅用 ${pct}%（触发线 90%）" || bad "swap 已用 ${pct}% → 有被 oomd 连带杀掉会话的风险"
    fi
  else ok "systemd-oomd 未启用"; fi
  info "内存：$(free -g | awk '/^Mem:/{printf "总 %sG 可用 %sG", $2, $7}')"
  ;;

# ==================================================================== s8
s8)
  hdr "$s · ${NAME_s8}"
  for u in medrec-ocr medrec-qwen medrec-laya; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^$u.service"; then
      printf '      %-14s enabled=%s active=%s\n' "$u" \
        "$(systemctl is-enabled $u 2>/dev/null || echo no)" "$(systemctl is-active $u 2>/dev/null || echo no)"
    else
      info "$u.service 未安装（还在用脚本起？脚本模式也能跑）"
    fi
  done
  printf '      ulimit -n      %s (软) / %s (硬)\n' "$(ulimit -Sn)" "$(ulimit -Hn)"
  info "日志目录：$MEDREC_LOGS"
  ls -1 "$MEDREC_LOGS" 2>/dev/null | sed 's/^/        /' | head -8
  info "各服务日志查看：bash 15-check.sh --log {qwen|ocr|ocrvl|laya}"
  ;;

  esac
done

echo
echo "──────────────────────────────────────────────"
printf ' 通过 %d · 警告 %d · 失败 %d\n' "$PASS" "$WARN" "$FAIL"
echo " （'!' 是提醒不是错误；'✗' 需要处理）"
echo "──────────────────────────────────────────────"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
