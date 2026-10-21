# AGENTS.md — 病历取数项目 · 给 Codex 的工作交接

> 主模型：本机 **Strata**（Qwen3.8-Flash-Next IQ3_S，OpenAI 兼容 `http://127.0.0.1:8080/v1`，Responses / Chat / Anthropic 三种协议都支持）。
> 工作目录 `/home/ubuntu/medrec`。你的角色是**助手**：产出草案与报告，**由人验收后才落地**。

## 一、红线（违反即视为任务失败）

1. **可写区只有两个**：`~/codex-scratch/`（草稿 + 记忆库）与 `~/medrec/workspace/`（**工地**：随便建文件、随便跑脚本、随便做实验）。
   `~/medrec` 下**其它**目录，以及 `~/vital_rules.json`、`~/vital_extract.py`、`tables.json`、`登记表.xlsx`、`scripts/*.sh`、模型权重 —— **一律只读**。
   - **唯一例外**：任务书里**逐条写明目录**时，只允许写那些目录。例外**不得自行推断**，任务书没写就是没有。
2. **不得** kill / pkill / 重启任何服务；**不得** sudo / su；**不得**联网 —— 只允许访问 `127.0.0.1` 上的本机端口。
   （唯一例外：任务书明确授权时，可重启 GPU 0 上的 OCR 服务，且必须先给回滚命令。
   回滚命令（2026-10-10 受控复测**可用**：`ocr-env` 下 ~6–30 s 就绪、`/health` 返回 `Healthy`，
   真实整页 `/layout-parsing` **24.5–33.0 s**；线上 8100 就是这条命令起的
   （`/proc/616288/cmdline` 与之逐字节相同，日志 `workspace/vllm_ocr/paddlex_ocrenv.log`）：

   ```bash
   setsid env CUDA_VISIBLE_DEVICES=0 /home/ubuntu/anaconda3/envs/ocr-env/bin/python \
     -m paddlex --serve --pipeline PaddleOCR-VL-1.6 \
     --host 127.0.0.1 --port 8100 --device gpu:0
   ```

   **线上用 `ocr-env`**（`scripts/config.env:22,25` 的 `PY_OCR=$ENV_OCR/bin/python`）。
   旧记录「`vl-env` 会让 serving 退化成单线程 CPU 推理」—— **本轮受控复测不成立**：
   `vl-env` 起同一条命令，真实整页 **28.8 s**、GPU0 sm 24%（`workspace/repro5/vlenv_page.out`）。
   旧记录「回滚前必须停旧实例，同卡第二个 PaddleX 必退化」—— **本轮复测也不成立**：
   8100 + 8120 同卡两个 `--serve` 实例，整页 28.0 s / 24.5 s、sm 92% / 52%
   （`workspace/repro5/two_serves_8120page.out`、`two_serves_second8100.out`）。

   ⚠ **旧记录「这条 `--serve` 命令已不可用 / 整页 600 s、1200 s 不返回」2026-10-10 复测不复现**
   （同日、独占 GPU0、起前 35 MiB、卡上单实例、`ocr-env`）：`workspace/repro5/real_serve_a.out`
   整页 28.4 / 25.9 s（sm 22%/32%）；round 4 那个失败过的客户端原路径
   `workspace/repro5/rollback_clean_now.out` 26.2 s（sm 36%）；逐行照抄
   `paddlex_cli.py:502-522 serve()` 的进程内复刻 `workspace/repro5/serve_default.out`
   27.6 / 25.3 s。三次正文 sha 相同（`0bd88a69ea79c9c3`）。
   → **serving 这条路径本身没坏**，不需要替代回滚路径；round 4 的归因
   （`paddlex/inference/pipelines/paddleocr_vl/pipeline.py:1124 _worker_vlm`
   在工作线程里把 VLM 解码退化到 CPU）**作废** —— 那两份证据是测量工具造的：
   在已加载 CUDA 的进程里 `subprocess.run(["nvidia-smi"])` 会 fork VIRT≈47 GB 的进程，
   把正在跑的推理拖死（离线整页 32.8 s → >150 s、sm 0%）。**GPU 采样必须在客户端进程做。**
   退化的**真实触发器仍 unknown**（本轮三种条件都造不出退化）。
   所以防线不是「记住哪种环境会坏」，而是**起服务后必须跑退化自检**：
   `bash scripts/10-start-ocr.sh check`（已接进启动路径；判据 = 请求期间 GPU sm，
   不是小图耗时；rc≠0 直接 die）。见 §二 末。停 GPU0 服务用
   `bash workspace/conc/stop_gpu0_hard.sh`（SIGTERM→20 s→SIGKILL）。
3. **不许猜**：每条结论附证据（`文件:行号` / `页号+原样片段` / 命令输出）；拿不到就写 `unknown` 并说明缺什么。
4. **大文件禁止整读**：>20000 字节先用 `docmap.py` 拿行号地图，再 `sed -n 'A,Bp'`（单次 ≤150 行）；整篇理解用 `docsum.py`；统计一律交给脚本。
5. **不自行落库**：规则与取值只出草案（含 evidence）；写入正式配置**必须由人执行**。
6. **缺失就是缺失，填 `null`。** 不许用「未提及」「无」「否」之类占位词冒充有值，也不许推断。
7. **不许为了让指标或耗时好看而牺牲字段完整性。** 少 OCR 页、跳过验证、放宽守卫、少跑变体都算。
   **每一条提速改动都必须附带字段级回归**（列命中数 + `主诉`/`查体` 是否非空）—— 「变快了」和「少抽了」长得一模一样。
8. **不许 `pip install` / `conda install`。** 缺库就报告包名、版本、完整安装命令，等确认。
9. **PII**：身份证号、本人电话、联系人电话、地址 —— 不许进 stdout / 日志，日志里打码（身份证留前 3 后 2）；输出文件权限 `600`。
10. 抽取温度设 `0`。报告里所有数字都要附**原始命令与原始输出**。

## 二、环境（2026-10-10 核对）

- 卡位（**2026-10-10 round 7 之后**）：**GPU0 = vLLM OCR 单副本(8110)**｜**GPU1/2/3** = Strata(8080)。
  `config/pipeline.yaml` 的 `ocr.backend` 已是 `vllm`、`ocr.vllm_url=http://127.0.0.1:8110`
  —— 单副本正好对上，**本轮没有也不需要改 `config/pipeline.yaml`**（md5 未变）。
  PaddleX(8100) 已停、不再常驻；第二副本 8112 已停。
  旧记录的 `Laya(8111)` / `agent-strong(8102)` 仍不在监听，勿再引用。
  交付配置 = **单副本**（必须逐字带上那两个确定性参数，
  闸门 `s1_ocr.vllm_single_seq_replicas()` 读 `/proc/<pid]/cmdline` 校验，≥1 个端点即通过）：

  ```bash
  # 唯一副本 —— GPU0（gmu 0.7 → GPU0 实测占用 17376 MiB）
  setsid /home/ubuntu/anaconda3/envs/qwen-env/bin/vllm serve \
    /home/ubuntu/medrec/models/models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master \
    --served-model-name PaddleOCR-VL --trust-remote-code \
    --host 127.0.0.1 --port 8110 --max-model-len 8192 \
    --gpu-memory-utilization 0.7 --attention-backend FLASH_ATTN \
    --compilation-config '{"mode":0,"cudagraph_mode":"full"}' \
    --max-num-seqs 1 --no-enable-prefix-caching
  ```

  **不要加 `--kv-cache-memory-bytes`**：那是 round 6 为「同卡两副本 KV 画像不对称」打的补丁，
  单副本下不需要。不带它：`Available KV cache memory: 14.23 GiB` /
  `GPU KV cache size: 828,960 tokens` / `Maximum concurrency 101.19x`，
  两次独立重启逐字相同（`workspace/round6/f0p_vllm_8110.log:77,79`、
  `workspace/round7/t3_vllm_8110.log:77,79`）。

  ⚠ 退回两副本：`bash workspace/round6/item2_kv_symmetric.sh`
  （8110+8112 各 gmu 0.35 + `--kv-cache-memory-bytes 3221225472`）。两副本必须**同时起
  （间隔 ≤2 s）**；错开 5 s 起第二个 → 第二个做内存画像时第一个已预留 8.1 GiB →
  `Available KV cache memory: -1.45 GiB` → `Engine core initialization failed`
  （`workspace/repro5/vllm_rep2_final.log`）。`--gpu-memory-utilization` 按**卡的总量**算、
  不感知同卡其它副本，两副本必须摊薄到 0.35。

  `--max-num-seqs 1 --no-enable-prefix-caching` 是**确定性开关**，不是性能参数：
  没有它们，同一页两次跑出不同正文（`c4c` vs `c4c2` = 238 同 / 2 不同；页级 30 同 / 18 不同）；
  带上它们，两趟端到端 240/240 逐列 sha 相同、页级 48/48 正文逐字节相同
  （本轮 `c5a` vs `c5a2`：`workspace/repro5/colsha_c5a_c5a2.out`、`workspace/repro5/audit_repeat_c5a.out`；
  旧轮 `workspace/conc/single_sq_conc.out`）。代价：并发只赚 5–8%。

  **round 6 更正（2026-10-10 round 7 写死，勿再引用旧数字）**：
  1. **探针 TPOT `1.98 ms/token` 只适用于 256×256 探针**，不能拿来算真实页。
     真实页 TPOT = **2.767 ms/token**（探针低估 40%）。带宽屋顶 1.79 ms/token
     → 真实页只达峰值 **65%**（不是 90%）。
     证据：`reports/round6-batch-verdict-token-20261010.md`、`workspace/round6/`。
  2. **「`VLLM_BATCH_INVARIANT` 会禁用 CUDA graph」这个假设已被证伪**（判决 B）。
     BI 的真实作用是 `vllm/v1/attention/backends/flash_attn.py:1715` 的 **`num_splits=1`**
     （关掉 decode 的 split-KV 并行），**不碰 CUDA graph**。
     所以「4.2× 惩罚是可绕开的实现副作用」不成立 —— 提 batch（BI + 高 `--max-num-seqs`）已裁决不做。
  3. **单副本 vs 两副本（同轮同口径、各两趟）**：单副本 `--gpu-memory-utilization 0.7`
     = **18.37 s/例**（两趟 18.49 / 18.25）；两副本各 `0.35` = **19.82 s/例**（两趟 19.85 / 19.78）。
     **单副本快 7.3%**，两个臂各自 240/240 列 sha 相同、48/48 页正文相同、命中页 10/10。
     → 交付配置因此改为**单副本**（见上面的启动块）。
- OCR 服务：PaddleX，pipeline **必须写全 `PaddleOCR-VL-1.6`**；端点 `/layout-parsing`、`/health`、`/restructure-pages`。
  线上用 `ocr-env`（`scripts/config.env:22,25` 的 `PY_OCR=$ENV_OCR/bin/python`）。
  两个环境都是 paddle 3.2.1 GPU 版，差别在 CUDA：`vl-env` = cuda 12.9 / cudnn 9.9.0，
  `ocr-env` = cuda 12.6 / cudnn 9.5.1（`paddle.version.cuda()` 实测）。
  ⚠ **旧记录「vl-env 起服务会退化到单线程 CPU、整页不可用」本轮不复现**：vl-env 起同一条
  命令，真实整页 28.8 s、GPU0 sm 24%（`workspace/repro5/vlenv_page.out`）。
  **环境名不是退化判据，自检才是**（见本节末）。
  启动命令见 §一 红线 2 的回滚块。**「GPU0 只能留一个 OCR 服务」本轮也不成立**：
  8100 + 8120 同卡两个 `--serve` 实例，整页 28.0 s / 24.5 s、sm 92% / 52%
  （`workspace/repro5/two_serves_base8100.out`、`two_serves_8120page.out`、`two_serves_second8100.out`）。
  真正的约束是**显存**：每个 PaddleX ~8.85 GiB，24 GiB 卡最多两个；
  PaddleX 与两副本 vLLM(0.35×2) 同卡必超 24 GiB。
  （旧测：PaddleX 与 vLLM 同卡时 vLLM 整页从 ~2.0 s 涨到 ~2.7–3.4 s —— 本轮未复测。）
- 抽取服务：Strata `http://127.0.0.1:8080/v1/chat/completions`，模型名 `qwen3.8-flash-next-iq3_s`，API key 任意（`sk-local`）。
  **它有思维链**：`choices[0].message` 里多一个 `reasoning_content`，解析必须只取 `content`。
- Python 一律 `/home/ubuntu/anaconda3/envs/vl-env/bin/python`（PATH 里 fsl 会抢 python）。
  **例外：OCR 服务（PaddleX，8100）不在 vl-env，在 `ocr-env`** —— 见上一条。
  产线脚本（`wide.*`）仍用 vl-env，它们只是 HTTP 客户端，不加载 paddle。
- 判活用 `curl -s http://127.0.0.1:<端口>/v1/models`；**不要用 pgrep**（PID 命名空间里会误报）。
  **但 `/health` 200 / `/v1/models` 200 都不代表服务在 GPU 上干活。**
  退化自检**已接进启动路径** `scripts/10-start-ocr.sh`：`paddle` / `vllm` 模式起完自动检
  （vLLM 每个副本各检一次），任一 rc≠0 → `medrec_die` 脚本非零退出；
  新增 `check` 模式对在跑的 paddlex + 每个 vLLM 副本各检一次后汇总退出。

  ```bash
  cd /home/ubuntu/medrec && bash scripts/10-start-ocr.sh check
  # 单独检一个端点：
  cd /home/ubuntu/medrec/src && /home/ubuntu/anaconda3/envs/vl-env/bin/python \
    -u -m wide.tools.ocr_healthcheck --backend vllm --url http://127.0.0.1:8110
  ```

  判据（2026-10-10 校准）：**请求期间 GPU sm 峰值 < `--min-sm`(默认 5%) → 退化（rc=3）**；
  耗时阈值 paddlex **120 s** / vllm 10 s。旧的 paddlex 30 s 阈值会把**健康**服务判成退化
  （健康 paddlex 的 256×256 小图本来就要 **73.9–81.7 s**，
  `workspace/repro5/hc_paddlex_before.out` 误判样本 / `selfchk_pass.out` 通过样本）。
  退出码：0 正常 / 2 无响应 / 3 退化 / 4 空响应。
  ⚠ 采样必须在**客户端**做 —— 在被测 CUDA 进程内 fork `nvidia-smi` 会自己造退化。

  `rc != 0` 就别往产线上跑。停 GPU0 服务用 `bash workspace/conc/stop_gpu0_hard.sh`
  —— PaddleX 收 SIGTERM 后**不会退出**（uvicorn 等那个已被客户端放弃的请求），
  显存永不释放，后续 vLLM 全部 `Engine core initialization failed`；该脚本 SIGTERM→20 s→SIGKILL。
  **别用 `pkill -f '<pattern>'`** —— 会匹配到自己那条 `bash -c`，自杀（exit 143）。
- 本机 `127.0.0.1:7897` 是**无效代理**：命令前加 `env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY`。
- MCP 工具在 `approval_policy=never` 下**一律被拒**：记忆与脚本操作只走 shell 命令行。
- **干活姿势**：`cxw "任务"`（可写工地）或 `cxwf 任务书.txt`（长任务书 + 可写）；`cx` / `cxf` 是**只读**，只适合查问题，写不了东西。

### 已裁决关闭的提速路线（round 9/10 实测，勿再重问）

1. **量化这条路整体关闭**（两条都实测证伪，出处 `reports/round9-awq-20261011.md`）：
   - vLLM `--quantization fp8`（在线动态）：TPOT **1.778 vs bf16 1.672 ms → 慢 6.3%**，
     KV 只多 0.16 GiB（权重并没有驻留变小）。
   - **AWQ W4A16**（compressed-tensors / Marlin kernel）：TPOT 只快 **5.7%**，但生成 token
     **+53.4%**、截断页 **6→18**、非空 **230/240→225/240**，端到端反而**慢 9.6~11.9%**。
     失败模式是「**不收尾**」（不吐 EOS）。
   - `llmcompressor` 自己量化：已证死（模型自定义代码与 transformers 5.x 不兼容，
     而 llmcompressor 0.14 要求 transformers>=5.15）。
   → 交付态维持 **bf16 单副本 8110**。
2. **TPOT 测量噪声 ±25% —— 强制「同会话配对」口径**：同一份 bf16 权重三处实测
   TPOT = **1.6161 / 1.7317 / 2.0202 ms**（跨度 ±25%），来源是 Strata 在 GPU1/2/3 上的争用。
   **凡要比较 20% 以内的差异，必须同会话、同管道、配对跑**；跨臂单独测出来的小差异不算证据。
3. **`vllm serve` 的模型路径是位置参数，不是 `--model` 旗标**（round 9 §3.6 踩过）。
   自检服务端参数时用 `grep -q '<模型路径片段>' /proc/<pid>/cmdline`，不要 `grep -- '--model '`。
4. **方案 D（让 VLM 少吐字）整条路关闭**（round 10 第2件实测，勿再重问）：
   - D 的「直出 24 列」形态：2026-10-10 四组 prompt 实验全部复读死循环 / 撞 4096
     （`workspace/vllm_ocr/probe_*/prompt_probe_raw.json`、`reports/ocr-vllm-switch-20261010.md:144-157`）。
   - **D2「只喂目标区域」**：主诉终点页高占比 p95=**0.529**、查体终点 p95=**0.817**（max **0.924**）；
     裁到前 55% 只保住 **29/38 例**（丢 23.7%）。每院区「保住全部」所需裁剪线跨度
     玉环 38.7% → 奉化 44.9% → 桐乡 49.4% → 长兴 71.5% → **安吉 92.4%**；
     安吉同一例跨批次位置 spread 高达 **0.505/0.645**。
     token 前提成立（裁到 55% 正文量平均只剩 48.8%）但代价不成比例 → **NO-GO**。
     证据 `workspace/round10/d2_geometry.out`、`d2_crop_curve.out`、`d2_extra.out`。
   - 附带口径：**现网 vLLM 路径每页只有 1 个块 `text`、`block_bbox=None`** —— 现网产物没有坐标，
     任何「按区域喂图」都得先让 s1 输出 layout，这本身是新一轮的成本。

### 来源守卫（round 10 第1件 · 已落地，会改变字段命中数）

`s4_normalize.py` 对 **frontpage 列**加了 `guard_provenance`（G3）：`source_label` 缺失 /
标签不属于本列 / 值在进过 prompt 的首页正文里找不到 → **判缺失不给值**（红线①⑥）。
实测代价：`adm_no` 10/10 → 7/10（历史 400 例次里判坏 109 格，全是 `adm_no`）。
`s5_qc.py` 现在会把「有值但无 `source_label`」写进 `05_qc/_summary/qc_flags.csv`
（旧版 s5 全文不读 `source_label` → 对 QC 完全静默）。
对照/回滚开关：`normalize.provenance_guard`（`config/pipeline.yaml`，**缺键=开**）
或 `s4 --no-provenance-guard`（只用于对照，正式跑批不许关）。详见 `reports/round10-admno-d2-20261011.md`。

## 三、流水线（六阶段）

```
⓪ locate  →  ① s1_ocr  →  ② s2_preprocess  →  ③ s3_extract
→  ④ s4_normalize  →  ⑤ s5_qc  →  ⑥ s6_table
```

产物落 `run/wide/<batch>/0X_*/<case_id>/`。**阶段之间用文件交接**，每步可单独重跑。
列定义唯一来源：`config/columns.yaml`（`2026-10-15.1` = **23 列**，含 2 组重名列 —— 按模板原样保留，**不合并**）。
**宽表输出列数 = yaml 列数 − `normalize.drop_output_columns`（当前 23 − `[]` ⇒ 23 列；
`就诊医院` 已在 `2026-10-15.1` 从 yaml 删除，不再靠删列开关）** ——
「24 列 = 新链」这条新旧链判据已失效（round34 E2），改用 `medrec doctor` 的 `COLUMNS …` 行
与 `medrec check` 的 `WIDE cols=… expect=…`。判重键 = 四元组
`(标准化就诊医院, 病案号, 住院号, 入院日期)`，时分不进键；任一分量空 ⇒ 该行不参与合并
并记 QC `dedup_key_incomplete`；同键跨院区 ⇒ 报警不合并（round34 Part B）。

```bash
cd /home/ubuntu/medrec/src
P=/home/ubuntu/anaconda3/envs/vl-env/bin/python
B=<batch>; CID=<case_id>
$P -u -m wide.tools.locate_pages --batch $B --case "$CID"
$P -u -m wide.s1_ocr          --batch $B --case "$CID" --crop-blocks
$P -u -m wide.s2_preprocess   --batch $B --case "$CID"
$P -u -m wide.s3_extract      --batch $B --case "$CID"
$P -u -m wide.s4_normalize    --batch $B --case "$CID"
$P -u -m wide.s5_qc           --batch $B
$P -u -m wide.s6_table        --batch $B
```

## 四、页面定位先验（实测，**不要再用「首页在第 1 页」那个假设**）

页序**分两组，方向相反**：

| 组 | 院区 | 首页位置 | 首程相对首页 |
|---|---|---|---|
| **A 尾组** | 奉化、长兴人民、安吉 | 记录**后段**（实测第 54–89 页） | 首页 **−10 ~ −13 页**（安吉 −5） |
| **B 头组** | 桐乡二院、玉环第一 | **第 0–1 页** | 首页 **+5 ~ +8 页** |

**统一规律：首程 = 首页 ± 4~13 页。** 所以定位策略是：
1. **两端各试 1–2 页**找到首页（A 组试尾部、B 组试头部），命中即停
2. 在**首页 ±13 页的窗口**内试 1–3 页找首程
3. 目标 **2–4 次 OCR 请求**（现状平均 11.7 次）

每一例都要记录：**扫了哪几页、在第几页命中、扩了几次**，否则漏页时查不出来。

## 五、命令约定（都是踩过的坑）

1. **后台任务必须 `setsid`**：`nohup ... &` 不够，父 shell 退出会回收。用
   `setsid nohup <cmd> > <log> 2>&1 < /dev/null &`
2. **不许 `rm -rf`，也不要试图绕过。** 想「干净重跑」就**另起目录**（`wide10c` → `wide10d`），
   上轮产物是证据，不该为新一轮让路。
3. **不要把「清理」和「启动」写进同一条复合命令**：清理被拦会连带拦掉启动。
4. **`ls` 会带 ANSI 转义码**（即使在 `$( )` 里）。拿路径用
   `find ... -printf '%s %p\n' | sort -rn | head -1 | cut -d' ' -f2-`，不要用 `ls -S`。
5. **长命令别用 `| tail -N`**（会缓冲住）；用 `setsid nohup ... > log &` 再轮询。

## 六、记忆（**已自动化**，你按需补充即可）

`cx / cxw / cxf` 包装器已经在沙箱外替你做了两件事，**不需要你操心**：
① 开工自动把最近 10 条已确认记忆注入任务开头（会显示为「【长期记忆·自动注入】」）；
② 收工自动从本次记录里抽取 0~5 条草稿写进库（草稿需人确认后才可被检索）。

```bash
P=/home/ubuntu/anaconda3/envs/vl-env/bin/python
$P ~/medrec/scripts/mem.py recall <关键词>      # 默认只返回已确认；加 --drafts 连草稿一起看
$P ~/medrec/scripts/mem.py add --kind fact|lesson --text "…" --tags a,b --source <任务>
```

库：`~/codex-scratch/memory/memory.db`（在你的可写区）。
**注意**：`memory.db` 里有大量旧项目（生命体征取数）的条目，与本产线无关，recall 时注意甄别。

## 七、输出报告

**按 `docs/REPORT_TEMPLATE.md` 的骨架写。** 结论先行；原始输出放附录；未验证项单独一节。

## 八、按需查阅（**不要整读**，先 grep 定位再读那几行）

| 文件 | 内容 |
|---|---|
| `~/medrec/docs/HOWTO.md` | 本产线用法：单例/批量命令、产物落点、参数含义、排错 |
| `~/medrec/config/columns.yaml` | 列定义（唯一来源；`2026-10-15.1` = 23 列，宽表输出 = 23 − drop_output_columns） |
| `~/medrec/docs/interfaces.md` | 端口 / 模型 / 启动命令 / 端点与请求响应形态 |
| `~/medrec/docs/security.md` | 安全边界、隔离跑法、审计方式 |
| `reports/` 下**最新一份** | 当前状态与待办 |

> 仓库里有 **1000+ 个 md**。**除上表列出的，不要主动去读**；需要时先 grep 定位。

## 九、回执（每次任务最后打印）

做了什么 / 产出在哪（绝对路径）/ 缺什么 / 建议下一步

> **关于「唯一例外」的边界（2026-10-10 补充）**：`run/`、`logs/`、`reports/`、`models/` **不是常设可写区** ——
> 它们只在**当次任务书逐条写明**时才可写。**`models/` 里的模型权重任何情况下都不得改动。**
<!-- STRATA-CTL:BEGIN -- 由 `~/strata-ctl.sh docs` 生成，块内不要手改 -->

### Strata 本地模型（Codex 的大脑）启停

引擎 `/home/ubuntu/Strata/engine/strata`，权重 `/raid5/strata-data`，监听 `127.0.0.1:8080/v1`，
模型名 `qwen3.8-flash-next-iq3_s`，API key 任意（`sk-local`）。占 **GPU1/2/3**（GPU0 是 OCR，不交叉）。

**唯一入口：`~/strata-ctl.sh`**

```bash
~/strata-ctl.sh status      # 在不在跑 + /v1/models + 三卡显存 + Codex 侧配置
~/strata-ctl.sh start       # 起，自带 20 分钟就绪轮询
~/strata-ctl.sh stop        # SIGTERM -> 等 24s -> 没停升 SIGKILL
~/strata-ctl.sh restart     # stop + start
~/strata-ctl.sh verify      # 真发一次补全，确认能出字（不只是端口开着）
~/strata-ctl.sh env         # 打印本脚本的环境，并与在跑进程逐段比对
~/strata-ctl.sh log 60      # 日志尾巴
~/strata-ctl.sh docs        # 重新生成本文档块
```

三条硬事实（都实测过）：

1. **`LD_LIBRARY_PATH` 不能漏**。引擎没有指向 CUDA 库的 rpath，`libcublasLt.so.13` 只靠它找到。
   实跑值前三段：`/home/ubuntu/cuda-13.4/bin`、`/home/ubuntu/cuda-13.4/lib64`、
   `/home/ubuntu/anaconda3/envs/qwen-env/lib/python3.12/site-packages/nvidia/cu13/lib`。
   漏了直接起不来 —— 这不是"可能有问题"，是必挂。
2. **冷启动约 556 秒**（46.84 GiB 专家 @3.85 GiB/s，再把 9728 个专家铺进 VRAM）。
   期间端口不响应是正常的，不要以为卡死。判断死活用 `~/strata-ctl.sh log 30`。
3. **停止必须两级**：只发 SIGTERM 会卡在 shutting down 不退出。脚本里是 TERM → 24s → KILL。

Codex 侧（`~/.codex/config.toml`）：

```
model    = "qwen3.8-flash-next-iq3_s"
base_url = "http://127.0.0.1:8080/v1"
env_key  = "LOCAL35B_KEY"        # shell 里需 export LOCAL35B_KEY=sk-local
```

响应带思维链：`choices[0].message` 多一个 `reasoning_content`，**解析只取 `content`**。
Strata 没起来时 Codex 报的是连接被拒，不是配置错。

<!-- STRATA-CTL:END -->
