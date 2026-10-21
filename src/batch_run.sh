#!/usr/bin/env bash
# ============================================================
# 批量流水线：PDF 目录 -> 拆页 -> 识别 -> 适配 -> 编译 -> F3 变量级路由
# 串行、逐文件、可断点续跑。**不需要 webui**
#
# 为什么分阶段跑而不是逐文件跑完整链路：
#   识别贵（GPU，几秒~几十秒/页），LLM 便宜（零点几秒/段）。
#   分阶段落盘后，改提示词/schema 只需重跑阶段 5，不用重烧识别。
#
# 目录布局（都在 $WORK 下，互不覆盖）：
#   pages/<名字>/page_0001.png        阶段1 拆页产物
#   raw/<名字>/*_layered.json         阶段2 识别产物（+ .done 标记）
#   json/<名字>/pages.json            阶段3 适配后的页数组
#   compiled/<名字>/                  阶段4 编译产物（compiled.md/json、sections/tables.json）
#   structured/<名字>.json            阶段5 F3 产物（存在 = 该文件已完成）
#   structured/登记表.xlsx             阶段5 拼好的 498 列宽表
#
# ★ v2：阶段 5 从「逐文件调 structure.py」改成「收完所有病例后**批量跑**」。
#   原因是实测出来的：规则层每份只要 0.25 s（纯 CPU），真正贵的是升级调用，
#   而**一批一起发才吃得满 GPU** —— 单份串行 74.8 s/份，8 并发批跑 27.7 s/份。
#   逐文件跑等于把这条杠杆扔掉。
#
# ★ v2.1：「收完一起跑」≠「一次全装进内存」。实测单份在内存留 ~351 KB，
#   37 万份就是 ~125 GB。所以按 F3_CHUNK（默认 200）分块调用 —— 每块约 480 个
#   请求，仍远超 8 个 worker，吞吐不变，内存从 125 GB 降到 ~70 MB。
#   哪一块断了就重跑哪一块（--skip-done 保证结果是幂等的）。
#
# ★ v2.1 修正：本脚本原先 LLM_URL 默认 8101（= qwen-small，8B），把 F3 主档
#   静默发给了 8B；而 medrec CLI 默认是 8102（27B）。两个入口不一致 → 已统一到 8102。
#
# 用法：
#   bash batch_run.sh /path/to/pdfs          # 平铺：一个 PDF = 一例
#   bash batch_run.sh /path/to/root          # **树形**：一个目录 = 一例（见下）
#   WORK=/home/ubuntu/medrec/jobs/b1 bash batch_run.sh /path/to/pdfs
#   SKIP_STAGE1=1 bash batch_run.sh /path/to/imgs    # 输入已是图片、无需拆页
#
# ★ 输入支持**任意深度**，规则只有一条：**一个 PDF（或图片）= 一份病历**。
#   真实数据的形状（实测 106 份 / 5 家医院，深度 3）：
#       测试/<来源>/<病例号>/<文件>.pdf
#   → 病例名 = 相对 $SRC 的路径（去扩展名，`/` → `__`），如 `奉化__20230728_008`
#     或 `玉环第一__205639__img20240314_16190007`。
#   ★ **不做合并**：同一目录下有几个 PDF 就是几例（`玉环第一/210926/` 里的
#     6 个 PDF → 6 例）。名字带上目录是为了**跨医院不撞**、并且能追溯来源。
#   同名不同路径 → **报错退出（exit 2）**，不会静默互相覆盖。
#   平铺输入（$SRC 下直接是 PDF）行为不变 —— 名字取 stem。
#   扫描结果会落一份 $WORK/input_manifest.tsv，宽表用它加
#   「来源目录 / 输入文件」两列（名字里的 `__` 反解有歧义，所以走对照表）。
#
# 解释器：默认全部用 vl-env 的绝对路径（这台机器 PATH 里 FSL 会抢 python/python3，
#   而 pypdfium2 / requests 都装在 vl-env）。要换：PY=/path/to/python bash batch_run.sh ...
# ============================================================
set -uo pipefail

SRC="${1:-}"
[ -n "$SRC" ] && [ -d "$SRC" ] || { echo "用法: bash batch_run.sh <含 PDF/图片的目录>"; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$HOME/medrec/jobs/batch_$(date +%m%d_%H%M)}"
PY="${PY:-/home/ubuntu/anaconda3/envs/vl-env/bin/python}"
VLLM_URL="${VLLM_URL:-http://127.0.0.1:8110}"
# 2026-10-07 换代：vLLM :8102(qwen-27b) 已停 → Strata :8080/v1
LLM_URL="${LLM_URL:-http://127.0.0.1:8080/v1}"
LLM_MODEL="${LLM_MODEL:-qwen3.8-flash-next-iq3_s}"
VL_LIMIT="${VL_LIMIT:-80}"
SMALL_URL="${SMALL_URL:-http://127.0.0.1:8101}"
SMALL_MODEL="${SMALL_MODEL:-qwen-small}"
# OCR：PaddleX 产线在 :8100（GPU0），pipeline 名必须写全
VL_URL="${VL_URL:-http://127.0.0.1:8100}"
# 分层开关，默认 off（实测开分层会换掉约 2.8% 的答案，见 medrec 里的注释）
F3_SPLIT="${F3_SPLIT:-off}"
F3_WORKERS="${F3_WORKERS:-1}"   # Strata 默认单请求；8 是旧 vLLM 的结论，未重测前按 1
SKIP_STAGE1="${SKIP_STAGE1:-0}"
LOG="$WORK/batch.log"

[ -x "$PY" ] || { echo "[x] 找不到解释器：$PY（用 PY=... 指定）"; exit 2; }

mkdir -p "$WORK"/{pages,raw,json,compiled,structured}
echo "== 批次目录：$WORK" | tee -a "$LOG"
echo "== 输入：$SRC" | tee -a "$LOG"
echo "== 解释器：$PY" | tee -a "$LOG"

# 识别进程要带的显存上限（防止 Paddle 按整卡预申请，把同卡的 vLLM 挤掉）
export FLAGS_initial_gpu_memory_in_mb="${FLAGS_initial_gpu_memory_in_mb:-4096}"
export FLAGS_fraction_of_gpu_memory_to_use="${FLAGS_fraction_of_gpu_memory_to_use:-0.45}"

shopt -s nullglob
# ============================================================
# ★ 输入不再要求「平铺」：**递归找，一个 PDF = 一份病历**。
#
# 真实数据的形状是 `测试/<来源>/<病例号>/<文件>.pdf`，深度 3。
# 一个 PDF 就是一例 —— **不做任何合并**（`玉环第一/210926/` 里 6 个 PDF
# 就是 6 例，它们恰好同属一个目录是数据本身的事，不是我们的语义）。
#
# 名字 = 相对 $SRC 的路径（去掉扩展名），`/` → `__`，例如：
#     奉化/20230728_008/20230728_008.pdf  →  奉化__20230728_008
#           （最后一级目录名与文件名相同 → 折叠掉重复的那段）
#     玉环第一/205639/img20240314_16190007.pdf
#           →  玉环第一__205639__img20240314_16190007
#     flat.pdf（在根下）                    →  flat
# 为什么带上目录：只用文件名会撞（病例号跨医院重复是常态，这套数据恰好
# 不重复是运气），而且**来源是你后面要用的**（见下面的 manifest 与宽表列）。
#
# 我们自己的产物目录要排除掉，否则把上一轮的 pages/ 又当输入吃进来。
EXCLUDE_DIRS="pages raw json compiled structured logs _failed"
_find_expr=()
for d in $EXCLUDE_DIRS; do
  _find_expr+=(-not -path "*/$d/*")
done
mapfile -t _allfiles < <(
  find "$SRC" -type f \( -iname '*.pdf' -o -iname '*.png' \
       -o -iname '*.jpg' -o -iname '*.jpeg' \) "${_find_expr[@]}" 2>/dev/null | LC_ALL=C sort
)
[ "${#_allfiles[@]}" -gt 0 ] || { echo "递归扫描 $SRC 没找到 PDF/图片"; exit 1; }
# nullglob 关掉：后面还有别的 glob，别改变它们「不匹配就保持字面量」的行为
shopt -u nullglob

# 三个平行数组：cases[] 名字 / case_file[] 输入文件 / case_dir[] 相对目录
declare -a cases=() case_file=() case_dir=()
declare -A _seen_name=()
for f in "${_allfiles[@]}"; do
  rel="${f#"$SRC"}"; rel="${rel#/}"
  case "$rel" in
    */*)
      d="${rel%/*}"; s="${rel##*/}"; s="${s%.*}"
      # 最后一级目录名与文件名相同 → 折叠，免得出现 `奉化__20230728_008__20230728_008`
      [ "${d##*/}" = "$s" ] && d="${d%/*}"
      if [ -n "$d" ]; then nm="${d//\//__}__${s}"; else nm="$s"; fi
      ;;
    *)  nm="${rel%.*}" ;;                       # 平铺在 $SRC 下：取 stem
  esac
  reldir="${rel%/*}"; [ "$reldir" = "$rel" ] && reldir=""

  if [ -n "${_seen_name[$nm]+x}" ]; then
    # ★ 两个**不同路径**映射到同一个名字 —— 绝不能静默互相覆盖
    echo "[x] 病例名冲突：'$nm' 同时来自"
    echo "      ${_seen_name[$nm]}"
    echo "      $rel"
    echo "    两条输入会被当成同一例互相覆盖。请改掉其中一个的目录/文件名后重跑。"
    exit 2
  fi
  _seen_name[$nm]="$rel"
  cases+=("$nm"); case_file+=("$f"); case_dir+=("$reldir")
done

ncase="${#cases[@]}"
echo "== 扫描到 $ncase 例（${#_allfiles[@]} 个文件；一个 PDF = 一例，不合并）" | tee -a "$LOG"

# ★ manifest：把「名字 ← 相对目录 / 输入文件」记下来给宽表用。
#   宽表本身只有 住院号+姓名+498 列，看不出每行来自哪家医院 ——
#   而名字里的 `__` 是有歧义的（真名里本来就可能含 `__`），
#   所以不靠反解名字，直接把对照表落盘。
MANIFEST="$WORK/input_manifest.tsv"
{
  printf 'name\tdir\tfile\n'
  for i in "${!cases[@]}"; do
    printf '%s\t%s\t%s\n' "${cases[$i]}" "${case_dir[$i]}" "${case_file[$i]#"$SRC"/}"
  done
} > "$MANIFEST"
echo "   对照表：$MANIFEST（宽表会用它加「来源目录 / 输入文件」两列）" | tee -a "$LOG"

n=0; okc=0; failc=0; incompletec=0
for i in "${!cases[@]}"; do
  n=$((n + 1))
  name="${cases[$i]}"
  _cf=("${case_file[$i]}")
  pages="$WORK/pages/$name"; raw="$WORK/raw/$name"
  jsond="$WORK/json/$name"; comp="$WORK/compiled/$name"
  {
    echo ""
    echo "========== [$n/$ncase] $name =========="
  } | tee -a "$LOG"

  if [ -f "$WORK/structured/$name.json" ]; then
    echo "  已完成，跳过" | tee -a "$LOG"; okc=$((okc + 1)); continue
  fi
  mkdir -p "$raw" "$jsond" "$comp"

  # ---- 阶段 1：拆页（图片输入可跳过）----
  if [ "$SKIP_STAGE1" = "1" ]; then
    # 输入已经是图片：那叠图就在它自己那个目录里，直接喂 layered.py
    pages="$(dirname "${_cf[0]}")"
  elif [ ! -f "$pages/.pages_done" ]; then
    # ★ 判据是 **.pages_done 标记**，不是「目录非空」。
    #   「非空就跳过」有个洞：渲染到一半失败时目录里已经有页，重跑会被当成
    #   已完成，然后拿**残缺的页**一路跑出结论 —— 而且不报错。
    #   现在 pdf2png 是「全收齐才落盘」（失败时一页都不写），所以失败后
    #   重跑必然从头来；这里只负责记住「这一例的页确实齐了」。
    echo "  [1/5] 拆页…" | tee -a "$LOG"
    if "$PY" "$HERE/pdf2png.py" "${_cf[0]}" --out "$WORK/pages" --as-name "$name" \
         --quiet >>"$LOG" 2>&1; then
      mkdir -p "$pages"; touch "$pages/.pages_done"
    else
      echo "  [x] 拆页失败（看 $LOG）" | tee -a "$LOG"; failc=$((failc + 1)); continue
    fi
  fi

  # ---- 阶段 2：识别（有 .done 就跳过；模型只加载一次）----
  if [ ! -f "$raw/.done" ]; then
    echo "  [2/5] 识别（串行，模型只加载一次）…" | tee -a "$LOG"
    # 2026-10-07：写死的 vllm/:8110 已停 → PaddleX 产线 :8100（GPU0）。
    "$PY" "$HERE/layered.py" "$pages" --vl-backend paddle --vl-url "$VL_URL" \
         --no-vl-autostart --vl-limit "$VL_LIMIT" --out-dir "$raw" --quiet >>"$LOG" 2>&1
    rc_layered=$?
    # ★ 2026-10-07 修：只有「产物页数 == 源页数」才允许 touch .done。
    #   以前是「layered.py 返回 0 就 touch」，而 layered.py 单页抛异常被吞掉后
    #   仍然返回 0 —— 于是 118 页只出 84 个产物也被判为完成，缺页一路往下传。
    n_src=$(find "$pages" -maxdepth 1 -type f -iname '*.png' | wc -l)
    n_out=$(find "$raw" -maxdepth 1 -type f -name '*_layered.json' | wc -l)
    if [ "$rc_layered" -eq 0 ] && [ "$n_src" -gt 0 ] && [ "$n_out" -eq "$n_src" ]; then
      [ -f "$raw/.incomplete" ] && rm -f "$raw/.incomplete"
      touch "$raw/.done"
      echo "  [2/5] 识别完成：产物 $n_out / 源 $n_src 页" | tee -a "$LOG"
    else
      # 缺页清单落盘，让批次能发现、能定向重跑
      {
        printf 'rc_layered=%s\nsrc_pages=%s\nout_pages=%s\nmissing_pages:\n' \
               "$rc_layered" "$n_src" "$n_out"
        for p in "$pages"/*.png; do
          b="$(basename "$p" .png)"
          [ -f "$raw/${b}_layered.json" ] || printf '  %s\n' "$b"
        done
      } > "$raw/.incomplete"
      echo "  [x] 识别不完整：产物 $n_out / 源 $n_src 页（rc=$rc_layered）" \
           "→ 缺页清单：$raw/.incomplete" | tee -a "$LOG"
      failc=$((failc + 1)); incompletec=$((incompletec + 1)); continue
    fi
  fi

  # ---- 阶段 3：适配（多页合并成一份页数组）----
  echo "  [3/5] 适配…" | tee -a "$LOG"
  "$PY" "$HERE/to_pages.py" "$raw" --out "$jsond" --merge --quiet >>"$LOG" 2>&1 \
    || { echo "  [x] 适配失败（看 $LOG）" | tee -a "$LOG"; failc=$((failc + 1)); continue; }

  # ---- 阶段 4：编译 ----
  echo "  [4/5] 编译…" | tee -a "$LOG"
  "$PY" "$HERE/compile.py" "$jsond/pages.json" --out "$comp" --quiet >>"$LOG" 2>&1 \
    || { echo "  [x] 编译失败（看 $LOG）" | tee -a "$LOG"; failc=$((failc + 1)); continue; }

  okc=$((okc + 1))
  echo "  [ok] $name（编译完成，等阶段 5 批量跑 F3）" | tee -a "$LOG"
done

# ---- 阶段 5：F3 变量级路由（**收完所有病例，批量跑一次**）----------------
# 为什么挪到循环外：升级调用是成本瓶颈，而并发才是它的杠杆。
# 逐文件跑 = 每个病例只发 2 个请求，GPU 大半时间在空转。
echo "" | tee -a "$LOG"
echo "== 阶段 5：F3 变量级路由（批量，$F3_WORKERS 并发）==" | tee -a "$LOG"
# ★ 分块：一次只把 F3_CHUNK 例装进内存。
#   实测单份要在内存里留 ~351 KB（段落 265 KB + 请求体 86 KB）——
#   **373,233 份 ≈ 125 GB**，一次装下必然 OOM。
#   但并发要的是「同时有足够多请求在飞」，不是「一次装下全部病例」：
#   200 份≈480 个请求，8 个 worker 根本吃不完 —— **分块不掉吞吐**。
#   --skip-done 让每一块都可重入：中断了原样重跑，已完成的不重算。
F3_CHUNK="${F3_CHUNK:-200}"
export QWEN_URL="$LLM_URL" QWEN_MODEL="$LLM_MODEL" \
       QWEN_SMALL_URL="${SMALL_URL:-http://127.0.0.1:8101}" \
       QWEN_SMALL_MODEL="${SMALL_MODEL:-qwen-small}" \
       F3_SPLIT="${F3_SPLIT:-off}"
n_case=$(find "$WORK/compiled" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
f3_ok=1
off=0
while [ "$off" -lt "$n_case" ]; do
  end=$((off + F3_CHUNK)); [ "$end" -gt "$n_case" ] && end="$n_case"
  echo "-- F3 第 $((off + 1))..$end 例 / 共 $n_case" | tee -a "$LOG"
  "$PY" "$HERE/f3_batch.py" --cases "$WORK/compiled" \
    --outdir "$WORK/structured" --workers "$F3_WORKERS" \
    --offset "$off" --limit "$F3_CHUNK" --skip-done 2>&1 | tee -a "$LOG" || f3_ok=0
  off=$((off + F3_CHUNK))
done
if [ "$f3_ok" = 1 ]; then
  echo "  [ok] F3 完成 -> $WORK/structured" | tee -a "$LOG"
else
  echo "  [!] F3 未全部完成（编译产物已留好，重跑本阶段即可；看 $LOG）" | tee -a "$LOG"
fi

# ---- 阶段 6：拼宽表（登记表本体）----------------------------------------
echo "== 阶段 6：拼 498 列宽表 ==" | tee -a "$LOG"
# ★ NIHSS 评分列（F6 的产物）。约定：F6 跑完把结果放 $WORK/nihss/，这里自动认；
#   也可以用 NIHSS_DIR=... 指定别处。**没有也照样出这一列（空）** ——
#   列数不随「有没有 F6 结果」变，下游按列号读表的代码才不会静默错位。
NIHSS_DIR="${NIHSS_DIR:-}"
if [ -z "$NIHSS_DIR" ] && [ -d "$WORK/nihss" ]; then
  NIHSS_DIR="$WORK/nihss"
fi
NIHSS_ARGS=()
if [ -n "$NIHSS_DIR" ]; then
  NIHSS_ARGS=(--nihss "$NIHSS_DIR")
  echo "   NIHSS 评分来自 $NIHSS_DIR" | tee -a "$LOG"
else
  echo "   （没有 $WORK/nihss/ 也没给 NIHSS_DIR —— NIHSS评分 列会是空的）" | tee -a "$LOG"
fi
"$PY" "$HERE/f3_to_wide.py" --indir "$WORK/structured" \
  --out "$WORK/structured/登记表.xlsx" "${NIHSS_ARGS[@]}" 2>&1 | tee -a "$LOG" \
  || echo "  [!] 宽表没拼成（structured/ 里的逐份 JSON 仍在）" | tee -a "$LOG"

echo ""
echo "== 结束：处理 $n 个，完成 $okc，失败/待重跑 $failc（其中识别缺页 $incompletec 例）"
if [ "$incompletec" -gt 0 ]; then
  echo "   ★ 有 $incompletec 例识别产物页数 < 源页数，标记在 raw/<名字>/.incomplete"
  echo "     列出所有缺页例：find $WORK/raw -name .incomplete"
fi
echo "   批次目录：$WORK   日志：$LOG"
echo "   登记表：  $WORK/structured/登记表.xlsx"
echo "   逐份结果：$WORK/structured/<名字>.json"
echo "   重跑 F3（只补没跑过的）："
echo "     $PY $HERE/f3_batch.py --cases $WORK/compiled --outdir $WORK/structured \\"
echo "       --workers $F3_WORKERS --skip-done"
echo "   重拼宽表："
echo "     $PY $HERE/f3_to_wide.py --indir $WORK/structured --out $WORK/structured/登记表.xlsx"

# 有失败/缺页就以非零码退出，批次级别才能发现（以前这里恒为 0）。
[ "$failc" -gt 0 ] && exit 1
exit 0
