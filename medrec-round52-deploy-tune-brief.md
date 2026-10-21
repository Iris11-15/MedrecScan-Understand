# 任务书：medrec 全栈部署包 + 自调参（迁移到 2×L40）

> **这份文档是自包含的。** 接手的人或模型不需要读过之前的任何报告，也不需要能访问源机器 ——
> 所有必要的背景、路径、数值、判据都写在下面。凡写「需现场确认」的地方，就是必须去目标机查的东西。

**版本**：round52 详细版　**目标机**：2× NVIDIA L40　**源机器**：4× RTX 4090

---

## 0. 怎么读这份文档

1. **先读 §1–§3**（背景与已知事实）—— 不读这三节，后面每个决定都看不懂为什么。
2. **§4 和 §5 是两个独立交付物**，可以分给两个人/两个模型并行做，**但接口必须严格按 §6 走**。
3. **§7 红线是硬约束**，违反即返工。
4. **§8 是验收清单** —— 交付前逐条勾，任何一条没勾都要在交付说明里写为什么。
5. **§9 定义了「遇到不知道怎么办」** —— 不要靠猜，按这一节处理。

---

## 1. 背景：这是个什么系统

把**扫描病历（PDF 或散页图片）**变成**一行一例的结构化宽表**，用于科研分析。

### 1.1 六阶段流水线

```
输入（PDF / 散页图片 zip）
   │
   ├─ 输入归一：散页图片 → 单个 PDF（原字节嵌入，不重编码）
   │            多患者守卫：抽首页/中间/末页各 1 页，比对住院号+姓名，不一致则禁止合并
   ▼
⓪ locate       在几十页里找「病案首页」和「首次病程记录」
① s1_ocr       对命中的页做整页 OCR + 难块分带二次识别
② s2_preprocess 文本预处理
③ s3_extract    调大模型按 24 列 schema 抽字段
④ s4_normalize  日期转 ISO、年龄去「岁」、来源目录 → 标准化就诊医院
⑤ s5_qc         质控标记
⑥ s6_table      汇总成宽表、去重、删列
   ▼
输出 run/wide/<batch>/06_table/wide.csv（一行一例，23 列）
```

阶段之间用**文件交接**，每一步可单独重跑，`.done` 文件是完成哨兵。

### 1.2 规模与性能基线（源机器 4×4090 实测）

| 指标 | 值 |
|---|---|
| 单批规模 | 3,831 例 |
| 批墙钟 | 21,576 s（约 6 小时） |
| 单例耗时 | 5.62 s（目标 ≤3.0 s） |
| OCR 请求总数 | 158,946（41.49 次/例） |
| 服务端吞吐 | 7.37 req/s |

**逐步优化后当前稳态**：240 例 bench 上 3.06–3.62 s/例（取决于 `max-num-batched-tokens` 取值，见 §5.3）。

### 1.3 输出格式

`config/columns.yaml` 定义 23 列（原本 24 列，已删掉全空的「就诊医院」）。

- **`主诉` / `查体` 必须原文照抄**，不许改写、不许摘要
- `入院时间（住院时间）` 照抄首页原文（中文格式，可到分钟）
- `标准化入院时间` ×2 列输出 ISO 格式
- `年龄` 输出纯数字（不含「岁」）
- 去重键：四元组 `(标准化就诊医院, 病案号, 住院号, 入院日期)`，任一分量为空则该行不参与合并

---

## 2. 这次要做什么

**把整套系统迁移到一台只有 2 张 L40 的新服务器，并产出一个「点击即可安装」的部署包 + 一个独立的自动调参工具。**

### 2.1 硬件变化（决定所有设计）

| | 源机器 4×4090 | 目标机 2×L40 | 影响 |
|---|---|---|---|
| 总显存 | 96 GB | 96 GB | 持平 |
| 单卡显存 | 24 GB | **48 GB** | 翻倍 |
| 卡数 | 4 | **2** | **减半** |
| 显存带宽 | 1008 GB/s | **864 GB/s** | **降 14.3%** |
| FP16 算力 | ~165 TFLOPS | ~90 TFLOPS | 对本场景影响可忽略（见下） |

**关于 FP16 算力**：本系统的 decode 是**显存带宽 bound**，不是算力 bound。
单个 token 需要读 1.8 GB 权重（0.9B 参数 × 2 字节），在 864 GB/s 上耗时 2.08 ms；
而计算只需 1.8 GFLOP，在 90 TFLOPS 上耗时 0.02 ms。**带宽慢 104 倍。**
所以 L40 的算力劣势不用管，**唯一要接受的是带宽降 14.3% ⇒ 所有 OCR 请求慢约 16%。**

### 2.2 已确定的拓扑（不要再讨论）

**一张卡跑 OCR，一张卡跑 Strata。**

```
卡 0: PaddleOCR-VL（vLLM 8110）  —— 约 4 GB
卡 1: Strata（Qwen 125B MoE）    —— 约 45 GB
```

**已评估并否掉的方案**：让 OCR 和 Strata 共享一张卡（能把 Strata 显存从 45 GB 提到 87 GB，但 OCR 会被抢带宽，收益不确定，实测数据不足）。

### 2.3 两个独立交付物

| # | 交付物 | 职责 | 失败后果 |
|---|---|---|---|
| **A** | `install.sh` 部署包 | 只装：代码 + 模型 + 环境 + 服务 | 什么都没有，需要重装 |
| **B** | `tune.sh` 自调参 | 只测：探测硬件与服务，产出建议配置 | **用默认值照样能跑**，只是不是最优 |

**这两个必须分开，原因见 §6。**

---

## 3. 已知环境事实（照抄，不要重新推导）

### 3.1 服务清单

| 服务 | 端口 | 模型 | 备注 |
|---|---|---|---|
| Strata | 8080 | Qwen3.8-Flash-Next IQ3_S（125B MoE） | 抽取用，`serving=1` |
| vLLM | 8110 | PaddleOCR-VL-1.6（约 0.9B） | 识别用，现役 |
| PaddleX | 8100 | 同 OCR 模型的 PaddleX 格式 | 旧路线，当前批次不用 |
| medrec 控制台 | 8090 | — | 网页，传 zip 入库、看跑批 |
| codex-webui | 8787 | — | 已弃用，**不需要迁移** |

### 3.2 模型清单

| 模型 | 用途 | 格式 | 备注 |
|---|---|---|---|
| PaddleOCR-VL-1.6 | OCR | **HuggingFace 格式**（vLLM 读） | 需 `--trust-remote-code` |
| PaddleOCR-VL-1.6 | OCR | **PaddleX 格式**（PaddleX 读） | 与上面是两份不同的文件 |
| PP-DocLayoutV3 | 版面分析 | PaddleX | |
| PP-OCRv5_mobile_rec | 文字识别 | PaddleX | |
| Qwen3.8-Flash-Next IQ3_S | 抽取 | Strata 自有格式 | 约 54 GB 权重，需 expert offload |

**⚠ 关键**：HF 格式那份的路径必须指到 `.../snapshots/master` 这一层。
`modelscope download --cache_dir ~/medrec/models` 会**再建一层 models/**，真实路径是
`~/medrec/models/models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master`。
指到上一级会报 `HFValidationError: Repo id must be in the form...` —— **这个报错等于「目录不存在」**。

### 3.3 Python 环境

| 环境名 | 用途 | 关键包 |
|---|---|---|
| `vl-env` | 产线主脚本 | 无 torch、无 vllm，只是通用脚本环境 |
| `ocr-env` | PaddleX | paddlepaddle（**无 torch**） |
| `qwen-env` | vLLM | vllm、torch |

**⚠ 这台机器上裸 `python` / `pip` 会被 `/home/ubuntu/fsl/bin` 抢走**（FSL 自带 Python 3.11）。
**所有命令必须用绝对路径**：`/home/ubuntu/anaconda3/envs/vl-env/bin/python`。

### 3.4 目录约定

```
~/medrec/
  src/wide/          六阶段代码
  src/wide/tools/    run_steps.py（编排器）、locate_pages.py
  bin/               medrec CLI 入口
  scripts/           启动脚本、config.env
  config/            columns.yaml、pipeline.yaml、locate_prior.yaml
  webui/             8090 网页（server.py + static/ + 若干 .py 模块）
  docs/              文档
  run/wide/<batch>/  产物（**绝不能打包**，含患者姓名）
  workspace/         工地（**绝不能打包**，含患者姓名）
```

产物目录结构：

```
run/wide/<batch>/
  00_index/      枚举 + 页索引
  01_ocr/<case>/ 每页 OCR 原文
  02_text/<case>/ 预处理正文 + .done
  03_extract/    抽出的字段
  04_norm/       标准化后字段
  05_qc/         质控标记
  06_table/      wide.csv（最终宽表）
  _medrec/       RUN_META.json / stages.json / summary.json
```

### 3.5 已确认的配置陷阱（迁移时一定会踩）

| # | 陷阱 | 现状 | 迁移时要做什么 |
|---|---|---|---|
| 1 | `scripts/config.env:44` 写着 `GPU_VL_TP="0"`，但线上 8110 实际在 **GPU1** | 启动必须显式带 `OCR_VLLM_GPU=1`，否则会落到 GPU0 与 PaddleX 共卡，起不来 | **重排卡号后必须重新确认这个值** |
| 2 | 8110 的 `--gpu-memory-utilization 0.7` | 在 24 GB 卡上是 16.8 GB；**在 48 GB 卡上会变成 33.6 GB** | **必须改成 0.2–0.25**，否则白占 30 GB |
| 3 | `strata-iq3_s.json` 的 `"gpu": [2,3]` | 依赖 4 卡编号 | 改成 `[1]`（新拓扑卡 1 给 Strata） |
| 4 | `--max-num-seqs 64`，但 AGENTS.md 文档写 1 | 文档过时 | 以线上 argv 为准，同步改文档 |
| 5 | `config/locate_prior.yaml` 的院区键是中文短名（「安吉」），而批次目录键是拼音（`jixi_rmyy`） | `prior_row()` 用精确匹配 ⇒ **2,689 例一次都没匹配上** | 迁移不修这个，但要知道它失效 |
| 6 | `--no-locate-cache` 只挡**读**缓存，不挡**写** | 测速批次会污染共享缓存目录 | bench 要用独立批次名 |
| 7 | 日志只增不轮转 | 源机器上已到 99 MB / 101 万行 | 安装时加轮转（注意：执行器有 `O_APPEND`，**只能 copytruncate**，不能 rename+create） |

---

## 4. Part A —— 部署包

### 4.1 分包策略

**必须分两个包，因为更新频率差三个数量级：**

| 包名 | 内容 | 体积 | 更新频率 |
|---|---|---|---|
| `medrec-code` | `src/` `bin/` `scripts/` `config/` `webui/` `docs/` | 几十 MB | 每周 |
| `medrec-assets` | 全部模型权重 + Strata 引擎 | 几十 GB | 装一次 |

安装顺序：**先 assets、再 code**。日常更新只替换 `medrec-code`。

**打包时必须排除**：`run/`、`workspace/`、`logs/`、`__pycache__`、`*.bak*`、任何 `.csv`/`.json` 产物。
**理由：`run/` 和 `workspace/` 里有几万个含患者姓名的 case_id，一旦发出去收不回来。**

### 4.2 `install.sh` 只做四件事，顺序不许变

```
1. preflight           查环境，任一不过就停
2. unpack + verify     解包 + 逐文件校验 sha256
3. env setup           建/激活 conda 环境，装 requirements（不联网则从本地 wheel 装）
4. start + smoke       起三个服务 + 跑 2 例端到端冒烟
```

**★ 不许在 `install.sh` 里内联任何压测、扫参、调参逻辑。** 见 §6。

### 4.3 preflight 必查项

| 检查 | 命令 | 不过怎么办 |
|---|---|---|
| OS 版本 | `lsb_release -a` | 只支持 Ubuntu 20.04+ |
| 驱动 | `nvidia-smi --query-gpu=driver_version --format=csv,noheader` | 报需要的版本，停 |
| 卡数与型号 | `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader` | **必须恰好 2 张，型号含 L40** |
| 磁盘余量 | `df -h` 目标分区 | 必须 ≥ 包大小 × 2 |
| 端口占用 | `ss -lntp \| grep -E '8080\|8110\|8090'` | 被占就停，报是哪个进程 |
| 外网 | `curl -sI --max-time 5 https://www.modelscope.cn` | 通 → online 模式；不通 → offline 模式 |
| conda | `command -v conda` | 没有就报「需要预装 miniconda3」 |

**每一个检查项都要打印 `[OK]` 或 `[FAIL] 原因 + 怎么补`，不允许静默跳过。**

### 4.4 服务启停脚本

必须提供三个脚本（不许把启停命令散在文档里）：

- `start-all.sh` —— 按顺序起 Strata(8080) → vLLM(8110) → webui(8090)，每起一个等健康检查通过
- `stop-all.sh` —— 反向顺序停
- `health.sh` —— 一次打印三个服务的状态，**人一眼能看出谁没起来**

`health.sh` 输出样例：

```
8080 Strata   [OK]  /v1/models → qwen3.8-flash-next-iq3_s
8110 vLLM     [OK]  /v1/models → PaddleOCR-VL
8090 webui    [OK]  HTTP 200
```

### 4.5 网页（8090）

`webui/` 整个目录进 `medrec-code`（含 `static/`、`table_formats/`、`README.md` 和同目录的 `.py` 模块）。

- 装完自动启动，默认绑 `127.0.0.1:8090`（**不许绑 0.0.0.0**）
- 启动方式：`cd <webui 目录> && setsid nohup <vl-env python> server.py > run.log 2>&1 < /dev/null &`
- **必须 `cd` 进去再起** —— `server.py` 用相对路径找 `static/`，不 cd 会页面样式全丢

### 4.6 幂等与回滚

- **幂等**：已解包的包、已装的 conda 环境、已下的模型，全部跳过；用 `.installed` 标记文件 + sha256 判断
- **断点续装**：记录每一步状态到 `install.state`，重跑时从断点继续
- **回滚**：`install.sh --rollback` 能撤回到安装前（停服务、删新建的目录、恢复被改的配置）；**不许删用户原有的任何东西**

---

## 5. Part B —— 自调参

### 5.1 目的与边界

**目的**：让同一套代码在不同机器上自动定出接近最优的参数。

**边界（重要）**：
- `tune.sh` **只读不写** —— 不许改 `config.env`、不许改服务启动参数、不许重启服务
- 唯一输出：一个 `tuning.json`（写在 `workspace/` 下），**落地由人执行**

**为什么这样设计**：调参要改参数就要重启服务，而一个「探测脚本」手里握着重启服务的能力，风险太大。
它跑砸了必须不影响任何东西 —— 这是它能被放心跑的前提。

### 5.2 四层探测，逐层加成本

| 层 | 测什么 | 怎么测 | 耗时 | 产出 |
|---|---|---|---|---|
| **L0 指纹** | 卡型号/显存/卡数/CPU 核数/磁盘 | `nvidia-smi`、`nproc`、`df -h` | <1 s | `fingerprint` |
| **L1 服务自检** | 8080/8110 活着、模型名对 | `GET /v1/models` | <1 s | 通过/不通过 |
| **L2 单点基准** | 单流 TPOT、单请求耗时 | 串行跑 10 个标准请求 | ~30 s | `measured` |
| **L3 吞吐扫描** | `R23_OCR_CONCURRENCY` / `R15_CASE_PARALLEL` 最优点 | 每组跑 100 例，扫 3–4 个值 | 20–60 min | 最优值 |

**L0–L2 每次部署都跑；L3 只在「卡型号不在查表里」或「人工指定」时跑。**

### 5.3 L2 的判据（这一层最容易被跳过，但最有用）

**L2 不只是测参数，它首先回答「这台机器正常吗」。**

```
L40 上单流 TPOT 的合理值 = 1.8 GB 权重 ÷ 864 GB/s = 2.08 ms
```

- 实测 2.0–2.2 ms ⇒ 正常
- 实测 5 ms 以上 ⇒ **机器有问题（驱动/时钟/带宽没吃满），先修机器，别调参**
- 实测 1.5 ms 以下 ⇒ 要么模型比预期小，要么测错了

**已有实测可对照（源机器）**：

| N（并发） | TPOT | 说明 |
|---|---|---|
| 1 | 1.74 ms | ≈ 1.8 GB / 1008 GB/s，证明单流是带宽 bound |
| 24 | 3.09 ms | 多出来的部分是 CPU 记账，约 0.05 ms/序列 |
| 96 | 5.83 ms | 边际收益已经过了拐点 |

### 5.4 卡型号查找表

`tune.sh` 里预置一张表，命中就不用跑 L3：

| 指纹 | `R15_CASE_PARALLEL` | `R23_OCR_CONCURRENCY` | 来源 |
|---|---|---|---|
| 4090 × 4 | 24 | 32 | 源机器实测 |
| L40 × 2 | 待测 | 待测 | **首次部署时需跑 L3 填进来** |
| 其他 | 24 | 32 | 保守默认 |

### 5.5 `tuning.json` 的格式

```json
{
  "fingerprint": { "gpu": "NVIDIA L40", "count": 2, "vram_mib": 46068,
                   "bandwidth_gbps": 864, "nproc": 96, "disk_free_gb": 1200 },
  "profile": "l40-2x",
  "measured": { "tpot_ms": 2.11, "single_req_s": 2.4, "ocr_req_s": 7.4 },
  "ocr":      { "max_num_batched_tokens": 16384, "gpu_mem_util": 0.22,
                "max_num_seqs": 64 },
  "strata":   { "gpu": [1], "kv_resident": 32768 },
  "pipeline": { "R15_CASE_PARALLEL": 20, "R23_OCR_CONCURRENCY": 24,
                "R23_PROBE_CONCURRENCY": 16 },
  "source":   "table:l40-2x"
}
```

**`source` 字段必须写清数值来源**：`table:<profile>` / `sweep:<日期>` / `default`。
出问题时要能立刻分辨「这个值是实测的还是猜的」。

### 5.6 参考：`max-num-batched-tokens` 的实测（源机器，240 例）

| 取值 | 墙钟 | s/例 | running 均值 | 等待占比 | 结论 |
|---|---|---|---|---|---|
| 8192 | 869.2 s | 3.622 | 24.20 | 7.4% | 保守 |
| 16384 | 794.8 s（−8.6%） | 3.312 | 34.82 | 23.2% | 推荐 |
| 32768 | 734.4 s（−15.5%） | 3.060 | 38.33 | 27.9% | 更快但排队明显 |

**收益来源是更大的 prefill / encoder 批次（Encoder cache 预算 8,192 → 32,768），不是「解锁并发」。**

---

## 6. 两个交付物的边界（这一节是核心约束）

| | Part A `install.sh` | Part B `tune.sh` |
|---|---|---|
| 装东西 | ✅ 全责 | ❌ 一个字节都不装 |
| 起服务 | ✅ | ❌ 不起不停不重启 |
| 跑 OCR 请求 | ✅ 只跑 2 例冒烟 | ✅ 会跑基准请求 |
| 写文件 | 装到目标路径 | **只写 `tuning.json` 一个文件** |
| 改配置 | 替换包里的默认值 | **不改任何配置** |
| 失败后果 | 什么都没有 | 用默认值照样能跑 |

**唯一耦合点**：`install.sh` 装完打印一行「安装完成，可运行 `tune.sh` 生成建议配置」；`tune.sh` 只依赖装好的服务在跑。

**为什么必须分开**：如果把调参塞进 install，**一次 30 分钟的压测失败会把整个安装判成失败，而实际上系统是好的。**

---

## 7. 红线

1. **不许把 `run/` 或 `workspace/` 打进任何包** —— 里面有患者姓名
2. 不许改源机器的任何配置、不许重启源机器的服务、不许删源机器的文件
3. 不许 `pip install` / `conda install`（清单外的包）—— 缺包报包名+版本+完整命令
4. 不许 `heredoc`
5. 不许在 `install.sh` 里内联调参；不许 `tune.sh` 改配置或重启服务
6. 所有产物权限：文件 600、目录 700
7. 网页**只能绑 `127.0.0.1`**，不许 `0.0.0.0`，不许做端口转发
8. 报告的粘贴值必须打码；交付文档里不许出现具体患者身份信息

---

## 8. 验收清单

### Part A 交付时必须逐条勾

- [ ] `medrec-code` 与 `medrec-assets` 两个包都生成，各带 `manifest.json`（文件清单 + sha256）
- [ ] 包里**没有** `run/`、`workspace/`、`logs/`、`__pycache__`（用 `tar -tzf | grep` 证明）
- [ ] `install.sh --help` 能打印全部子命令：`preflight` / `online` / `offline` / `--rollback` / `--dry-run`
- [ ] `preflight` 的 7 个检查项都有 `[OK]` / `[FAIL+原因]` 输出
- [ ] `--dry-run` 能跑通且零副作用（证明它不会误伤目标机）
- [ ] 三个启停脚本存在且 `health.sh` 输出格式与 §4.4 一致
- [ ] `webui/` 启动脚本里有 `cd`（证明不会丢静态资源）
- [ ] 重跑 `install.sh` 显示全跳过（幂等）
- [ ] 冒烟测试能产出 `06_table/wide.csv`，行数=2、列数=23

### Part B 交付时必须逐条勾

- [ ] `tune.sh` 全程不写 `config/`、不改 `config.env`、不重启任何服务（附证明：跑完 `md5sum` 前后一致）
- [ ] L0/L1/L2 三层都能单独调用（`tune.sh --level L0`）
- [ ] L2 输出里有「TPOT 是否落在预期区间」的明确判断，而不只是一个数
- [ ] `tuning.json` 的 `source` 字段非空，且三种取值都出现过至少一次
- [ ] L3 默认不跑，需要 `--sweep` 显式开启
- [ ] 卡型号表里有 4090×4 一行，且数值与源机器实测一致

---

## 9. 遇到未知怎么办

**这份文档覆盖不到的，按下面三条处理，不要猜：**

1. **能查的** —— 查。`nvidia-smi`、`pip show`、`strings`、`ps`、`grep`。查完把原始命令和原始输出贴进交付说明。
2. **查不到的** —— 写成 `unknown`，并写清「缺什么信息才能确定」。**不许用「应该」「大概」「估计」填空。**
3. **需要人决定的** —— 写成「待裁决」条目，说明两个选项各自的代价。**不许自己选一个然后当成事实往下做。**

**已经明确标注为「需现场确认」的项（§3.5 的七个陷阱、§2.1 的卡型号 L40 vs L40S、§5.4 的 L40 参数）必须全部有结论或标 unknown，不能留空。**

---

## 10. 交付格式

1. 两个包（`medrec-code-<version>.tar.gz`、`medrec-assets-<version>.tar.gz`）+ 各自的 `manifest.json`
2. `install.sh`、`tune.sh`、`start-all.sh`、`stop-all.sh`、`health.sh`
3. `DELIVERY.md`：
   - §1 两个包的大小与内容摘要
   - §2 `install.sh` 每个子命令怎么用，给三条实际执行记录
   - §3 `tune.sh` 的 L0/L1/L2 实际输出（L3 若无则说明未跑及原因）
   - §4 §3.5 那七个陷阱各自的处置结果
   - §5 未知项清单
   - §6 待裁决清单
4. 所有脚本带 `set -euo pipefail`，所有 `curl` 带 `--max-time`
