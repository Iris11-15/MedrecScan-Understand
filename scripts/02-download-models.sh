#!/usr/bin/env bash
# =====================================================================
#  02-download-models.sh — 拉三份权重（魔搭优先）
#
#  产物：
#    $MODEL_OCR_VL_PATH    PaddleOCR-VL-1.6（HF 格式，vLLM 可读）  ~1.8 GB
#    $MODEL_QWEN_PATH      Qwen3.8-27B 的 W4A16 量化版            ~15  GB
#    Laya 三个检查点        由 laya 包自己按需下载                 ~1.5 GB
#
#  用法： bash 02-download-models.sh             （全部）
#         bash 02-download-models.sh ocr qwen      （只下指定的）
#         FORCE=1 bash 02-download-models.sh qwen  （重下）
# =====================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/config.env"
medrec_clean_path
medrec_mkdirs

WANT=("$@"); [ "${#WANT[@]}" -eq 0 ] && WANT=(ocr qwen laya)
FORCE="${FORCE:-0}"
[ -x "$PY_LLM" ] || medrec_die "qwen-env 不存在，先跑 01-setup-envs.sh"

want() { local w; for w in "${WANT[@]}"; do [ "$w" = "$1" ] && return 0; done; return 1; }

# 通用：下载到本地目录（魔搭优先，失败退 HF 镜像）
#
# ★ 这里曾经有个真 bug：snapshot_download() 返回的是**缓存目录**（blobs/ + snapshots/
#   的符号链接结构），旧代码只 glob 了一层且不跟随链接 → 大权重分片全丢，只剩
#   config.json/README.md 几个小文件（实测 ~184K，0 个 safetensors）。
#   正确做法：让下载库**直接落到目标目录**（local_dir=），不要自己拷贝。
ms_download() {   # ms_download <repo_id> <本地目录>
  local rid="$1" dst="$2"
  if [ "$FORCE" != "1" ] && [ -d "$dst" ] && ls "$dst"/*.safetensors >/dev/null 2>&1; then
    medrec_log "已存在且有权重文件，跳过：$dst"; return 0
  fi
  [ -d "$dst" ] && [ "$FORCE" = "1" ] && { medrec_log "FORCE=1，清空重建 $dst"; rm -rf "$dst"; }
  medrec_log "下载 $rid → $dst"
  mkdir -p "$dst"
  MODELSCOPE_CACHE="$MEDREC_MODELS/_ms_cache" \
  HF_HOME="$MEDREC_MODELS/_hf_cache" \
  "$PY_LLM" - "$rid" "$dst" <<'PY' || return 1
import os, sys
rid, dst = sys.argv[1], sys.argv[2]

def via_modelscope():
    from modelscope import snapshot_download
    # local_dir 让文件直接落在 dst（新版 modelscope 支持；旧版会忽略并只返回缓存路径）
    return snapshot_download(rid, local_dir=dst,
                             cache_dir=os.environ["MODELSCOPE_CACHE"])

def via_hf():
    from huggingface_hub import snapshot_download
    return snapshot_download(rid, local_dir=dst,
                             cache_dir=os.environ["HF_HOME"],
                             endpoint=os.environ.get("HF_ENDPOINT", "https://hf-mirror.com"))

for name, fn in (("modelscope", via_modelscope), ("hf-mirror", via_hf)):
    try:
        p = fn()
        print(f"  ✓ {name} 完成，返回路径 {p}")
        break
    except Exception as e:
        print(f"  {name} 失败（{type(e).__name__}: {e}）", file=sys.stderr)
else:
    sys.exit(1)

# 自检：目标目录里到底有没有权重
import glob
st = glob.glob(os.path.join(dst, "**", "*.safetensors"), recursive=True)
if not st:
    print(f"  ✗ {dst} 下没有任何 .safetensors —— 下载没落到位（别继续，先看上面的报错）", file=sys.stderr)
    if p and os.path.realpath(p) != os.path.realpath(dst):
        print(f"    提示：文件可能在 {p}，可手动 mv 过来", file=sys.stderr)
    sys.exit(1)
sz = sum(os.path.getsize(f) for f in st)
print(f"  ✓ 权重 {len(st)} 个分片，合计 {sz/2**30:.2f} GiB")
PY
}

report_size() {  # report_size <目录> [期望的最小 GiB]
  local d="$1" mn="${2:-0}" n sz
  n=$(find "$d" -name '*.safetensors' 2>/dev/null | wc -l)
  sz=$(du -sm "$d" 2>/dev/null | cut -f1)
  printf '    %s：%s 个 safetensors · %s MB\n' "$d" "$n" "${sz:-0}"
  if [ "$n" -eq 0 ]; then
    medrec_log "  ✗ 没有任何 .safetensors —— 目录是空的/下错了仓库，别继续"
    return 1
  fi
  if [ "$mn" -gt 0 ]; then
    local gb=$(( ${sz:-0} / 1024 ))
    [ "$gb" -ge "$mn" ] && medrec_log "  ✓ 体积 ${gb} GB（期望 ≥${mn} GB）" \
                        || medrec_log "  ⚠ 只有 ${gb} GB，期望 ≥${mn} GB —— 可能下载不完整"
  fi
  return 0
}

# 判一个目录是不是「像样的模型目录」：有权重 + 有 config.json
looks_like_model() { [ -f "$1/config.json" ] && find "$1" -name '*.safetensors' 2>/dev/null | grep -q . ; }

# ------------------------------------------------------------ OCR 权重
if want ocr; then
  echo "── PaddleOCR-VL-1.6 ─────────────────────────────────────────"
  if ms_download "$MODEL_OCR_VL_ID" "$MODEL_OCR_VL_PATH"; then
    if report_size "$MODEL_OCR_VL_PATH" 1; then
      [ -f "$MODEL_OCR_VL_PATH/config.json" ] \
        && medrec_log "✓ config.json 在位，vLLM 可直接 serve" \
        || medrec_log "⚠ 没有 config.json —— 检查是不是下到了非 HF 格式的分支"
    else
      medrec_log "⚠ OCR 权重不完整 → 也可让 PaddleX 产线首次启动时自己拉（约 1.8GB）"
    fi
  else
    medrec_log "✗ OCR 权重要么改用 PaddleX 自动拉取（产线启动时会自己下 ~1.8GB），要么手动重下"
  fi
fi

# ------------------------------------------------------------ Qwen 权重
if want qwen; then
  echo "── Qwen3.8-27B · W4A16 (Q4) ─────────────────────────────────"
  echo "    4090 是 Ada（sm_89）：NVFP4 / MXFP4 都用不了，只走 W4A16 的 AWQ/GPTQ。"
  if ms_download "$MODEL_QWEN_ID" "$MODEL_QWEN_PATH" && report_size "$MODEL_QWEN_PATH" 12; then
    "$PY_LLM" - "$MODEL_QWEN_PATH" <<'PY'
import json, os, sys, glob
p = sys.argv[1]
cfg_p = os.path.join(p, "config.json")
if not os.path.exists(cfg_p):
    print("    ✗ 缺 config.json，这份权重不能用"); sys.exit(1)
cfg = json.load(open(cfg_p, encoding="utf-8"))
q = cfg.get("quantization_config") or cfg.get("quant_config") or {}
tc = cfg.get("text_config") or {}
n_heads = tc.get("num_attention_heads") or cfg.get("num_attention_heads")
print(f"    architectures    : {cfg.get('architectures')}")
print(f"    model_type       : {cfg.get('model_type')}")
print(f"    quantization     : {q.get('quant_method') or q.get('quantization_method') or '(未声明)'}"
      f"  bits={q.get('bits','-')}  group={q.get('group_size','-')}")
print(f"    text 注意力头     : {n_heads}  (kv={tc.get('num_key_value_heads')})")
print(f"    max_position     : {tc.get('max_position_embeddings') or cfg.get('max_position_embeddings')}")
print(f"    vision_config    : {'有（多模态塔在位）' if cfg.get('vision_config') or tc.get('vision_config') else '无'}")
# 递归查权重：local_dir 的结构可能带子目录，别只看一层
st = sorted(glob.glob(os.path.join(p, "**", "*.safetensors"), recursive=True))
sz = sum(os.path.getsize(f) for f in st)
print(f"    权重分片         : {len(st)} 个，合计 {sz/2**30:.2f} GiB")
if n_heads:
    ok_tp = [t for t in (1, 2, 4, 8, 16) if n_heads % t == 0]
    bad_tp = [t for t in (1, 2, 3, 4, 8) if n_heads % t]
    print(f"    可用的 TP        : {ok_tp}   ← {n_heads} 必须被 TP 整除")
    if bad_tp:
        print(f"    ✗ 不能用 TP={bad_tp}（会 AssertionError: {n_heads} is not divisible by N）")
PY
  fi
fi

# ------------------------------------------------------------ Laya 检查点
if want laya; then
  echo "── Laya 检查点 ──────────────────────────────────────────────"
  [ -x "$PY_LAYAD" ] || { medrec_log "laya-env 不存在，跳过"; exit 0; }
  "$PY_LAYAD" - <<'PY' || medrec_log "⚠ Laya 检查点下载失败：离线时先把 HF 缓存拷进来再跑"
import os, sys
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
try:
    from huggingface_hub import snapshot_download
except Exception as e:
    print(f"  缺 huggingface_hub: {e}"); sys.exit(1)
for sub in ("multilingual", "typed-decisions"):
    try:
        p = snapshot_download("convaiinnovations/laya", allow_patterns=[f"{sub}/*"])
        print(f"  ✓ {sub} → {p}")
    except Exception as e:
        print(f"  ✗ {sub}: {type(e).__name__}: {e}")
PY
fi

echo
echo "── 磁盘占用 ─────────────────────────────────────────────────"
du -sh "$MEDREC_MODELS" 2>/dev/null || true
df -h "$MEDREC_MODELS" | tail -1
echo "完成。"
