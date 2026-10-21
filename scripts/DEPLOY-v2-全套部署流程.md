# 病历智能平台 · 部署手册 v2.0

> **架构**：OCR 前端识别 → Laya 打分/快判前端 → Qwen3.8-27B 后端（数据处理 + 对话）
> **目标机**：Ubuntu 22.04 + 4× RTX 4090（96 GB，Ada **sm_89**）
> **前置**：接管现有服务器 —— **旧的 v14 四卡服务全部停掉，不沿用旧环境**，从 conda 开始重建
> **交付物**：本手册 + `deploy-v2/` 下 8 个可直接执行的脚本

---

## 0. 先看这里：三个必须知道的事实（写错就是白干几小时）

这三条是本次部署里**最容易出错、且错了会浪费整晚**的地方。先读完再动手。

### 0.1 Qwen3.8-27B 在 4090 上只有一条量化路线

| 量化格式 | 4090（Ada sm_89）能不能用 | 说明 |
|---|---|---|
| **NVFP4 / MXFP4** | ❌ **不能用** | 需要 Blackwell（sm_120）。官方 recipe 里 "1× RTX 5090 需 `--enforce-eager`" 那一节对你**不适用**，别照抄 |
| **FP8** | ⚠️ 不推荐 | 4090 没有原生 FP8 张量核，会落回 Marlin 反量化，省不了显存还慢 |
| **W4A16（AWQ / GPTQ-Int4）** | ✅ **唯一正确选择** | 权重约 13.5 GiB，24 GB 卡装得下还有余量做 KV cache |

所以配置里写死的是 `cyankiwi/Qwen3.8-27B-AWQ-INT4`。可用替代（都在 HF，魔搭有同名镜像时可换）：

- `abhishekchohan/Qwen3.8-27B-AWQ-INT4`
- `pearsonkyle/Qwen3.8-27B-GPTQ-W4A16`
- `mattbucci` / `Avesed` 的同名 W4A16 版本

> ⚠️ 搜索 "Qwen3.8-27B-AWQ-4bit" 时见过 **"VRAM 27.7GB"** 的条目 —— 那是**未量化的名字骗人**或统计口径含 KV，别据此规划显存。以 `02-download-models.sh` 打印的 `safetensors 合计 GiB` 为准。

### 0.2 张量并行卡数**必须整除 `num_attention_heads`**

Qwen3.8-27B 是 **64 层稠密 + 混合注意力**（仅 16 层全注意力，48 层线性注意力），且**带视觉塔**。

> ⚠️ **别看官方 recipe 里"视觉编码器 16 个注意力头"就拿 16 当除数** —— 那**不是**文本模型的值。
> **实测**（`cyankiwi/Qwen3.8-27B-AWQ-INT4`）：文本模型 `num_attention_heads = 24`、`num_key_value_heads = 8`。
> 所以有效 `--tensor-parallel-size` 是 **1 / 2 / 3 / 4 / 6 / 8** —— **TP=16 对这个模型是非法的**，而 TP=3 反而是合法的。

`11-start-qwen.sh` 启动前会读 `config.json` 的 `num_attention_heads` 与 `num_key_value_heads`，**穷举出可用 TP 集**并校验你设的值，不整除就在起服务之前报错退出。

### 0.3 三个环境**不能合并**，这不是洁癖

| 冲突 | 后果 |
|---|---|
| Qwen3.8 的 `config.json` 要 **transformers ≥ 5.8.0**；PaddleX 3.x 对 transformers 另有一套约束 | 同环境 → 有一方解析不了 config |
| **Laya 要 torch 2.14**；vLLM 对 torch 版本钉得很死 | 把 laya 装进 qwen-env → 极可能顶掉 vLLM 的 torch，**vLLM 会以 kernel 不匹配挂掉，且报错完全看不出是 torch 被换了** |

→ 三套环境：`ocr-env` / `qwen-env` / `laya-env`，靠 **HTTP 通信**，不共享 Python 包。

### 0.4 还有两个已知的官方 issue（部署前先知道，省得当成自己配错）

1. **MTP（多 token 预测）在本模型上会崩**：`[Bug]: Qwen3.5/3.8 multimodal + native MTP crashes in profile_run`（vllm#58203）。**本手册全程不加 `--speculative-config`。**
2. **vLLM 把 `Qwen/Qwen3.8-27B-FP8` 当多模态但缺多模态 processor**，会打印 "running in text-only mode"（HF discussion #146）。本手册默认走 **text-only** 路线（`QWEN_TEXT_ONLY=1`），OCR 已在上游做完，本来就该如此；要用 Qwen 回看原图，读完 §9 再开。

---

## 1. 架构总览

```
                      ① OCR 前端识别                 ② 打分/快判前端            ③ 后端大脑
 ┌──────────┐    ┌──────────────────────────┐   ┌────────────────────┐   ┌────────────────────┐
 │ PDF/图片 │───►│ A 路 PaddleX 产线         │──►│ Laya 决策引擎       │──►│ Qwen3.8-27B (Q4)   │
 │ 病历扫描 │    │   PP-DocLayoutV3 版面     │   │  421M/322M 编码器   │   │  27B dense/混合注意 │
 └──────────┘    │   + PP-OCRv5 快路径       │   │  一次前向出全部题    │   │  256K 上下文        │
                 │ B 路 PaddleOCR-VL-1.6     │   │  33ms / 校准概率    │   │  结构化 JSON 抽取   │
                 │   (vLLM, 难块升级)        │   │  · NIHSS 15 项打分  │   │  · 医学对话         │
                 └──────────────────────────┘   │  · 病历类型/科室快判 │   │  · 低置信项仲裁      │
                            │                   └────────────────────┘   └────────────────────┘
                            │                            │                        ▲
                            ▼                            │ 低置信 (conf<0.70)     │
                    blocks.json / compiled.md ───────────┴────────────────────────┘
                                                                                  │
                          产物：nihss.json（逐项分值 + 依据 + 来源 + 分歧标记）
```

**为什么是"Laya 在前、Qwen 在后"而不是让 Qwen 一把梭：**

| | Laya | Qwen3.8-27B |
|---|---|---|
| 速度 | 33 ms / 问，7.2 ms/问（批） | 秒级 |
| 任务形态 | **定档**（choice / score / noul），不生成文本 | 生成 + 结构化抽取 + 长上下文推理 |
| 输出 | 概率**经校准**（RLCD 训练），可直接做阈值门控 | 需要 schema 约束才可靠 |
| 短板 | 长文本、>20 选项、需要跨段推理 | 慢、贵、全量跑不可复现 |
| 定位 | **System 1**：全量快筛 | **System 2**：疑难仲裁 + 对话 + 抽取 |

→ NIHSS 15 项每题 3~5 档、病历类型 9 选项 —— 都落在 Laya 的能力区间，**全量交给它**；只有 `confidence < 0.70` 的项升级给 Qwen，两项都归档，**分歧项标人工复核**。这就是 `tech-stack.md` 里"按变量路由"的具体落地。

> Laya 的高基数短板有实测数字：Banking77 的 77 个标签上掉到 0.425（对比 Jev 0.870），原因是选项共享固定 token 预算（英文 192 / 多语 256），77 个标签摊到每标签只剩 3~4 token。**所以它只适合 <=20 选项的定档任务**，别拿它替代 F3 的 498 列抽取。

---

## 2. 资源规划（4×4090 的新分配）

### 2.1 显存预算（实测基线口径）

> ⚠️ **每卡有约 480 MiB 是驱动/OS 基线占用**（实测：`total 24564` − `used 18` − `free 24064` = 482 MiB/卡）。
> **vLLM 拿不到这部分，但它算 `--gpu-memory-utilization` 用的是「总量」24564** —— 所以按总量算出来的预申请值要额外扣掉这 480 MiB 再比较。

| 服务 | 卡 | 预申请 | 说明 |
|---|---|---|---|
| PaddleX 产线 | GPU0 | ~1.5 GB | 版面 + PP-OCRv5 快路径 |
| PaddleOCR-VL-1.6（vLLM，`VL_GMU=0.20`） | GPU1 | 0.20 × 24564 ≈ **4.9 GB** | 与 Qwen 共卡，只能给小值 |
| Qwen3.8-27B（TP=2，`QWEN_GMU=0.44`） | GPU1–2 | 0.44 × 24564 ≈ **10.8 GB/卡** | 权重约 13.6 GiB 走 TP 摊到两卡 |
| Laya 打分服务 | GPU2 | ~1.5 GB | 可改 `LAYA_DEVICE=cpu` 完全不占卡 |
| **每卡固定损耗** | 全部 | **~480 MiB** | 驱动/OS 基线 |

**取值的硬边界**：`QWEN_GMU` 建议落在 **0.40 ~ 0.80**。**别顶到 0.90 以上** —— 0.90 时每卡只剩约 2.4 GB，扣掉 480 MiB 基线与 CUDA context / CUDA graph 的分配（这部分**不在 `gpu-memory-utilization` 预算内**），很容易在启动的图捕获阶段 OOM。要更大的 KV cache，优先调 `QWEN_MAXLEN` 与 `--max-num-seqs`，**不是把 GMU 顶满**。
（官方 recipe 里 "1× RTX 5090 需要 `--enforce-eager`，且调 GMU 到 0.80/0.93 都只剩 47 MiB 空闲" 就是因为图捕获在预算外分配 —— 同一类坑。）

### 2.2 图形会话会不会碍事（实测结论）

- **Xorg 会占显存**，且**经常不出现在 `nvidia-smi --query-compute-apps` 里**，这就是"显存被占但没有 PID"的经典现象。
- **判据**：`nvidia-smi | grep -iE 'xorg|gnome-shell'` 看占用 —— **≤64 MiB = 无头 X 的帧缓冲，等于没占**；200~600 MiB = 有活跃桌面会话；>1 GB = 桌面里跑着浏览器/IDE。
- **处置**：有会话时**不要**去停 `gdm`（会连带断掉远程桌面），只需给 `QWEN_GMU` 留出这部分余量。`00-stop-all-and-verify.ps1` §9.3 会自动判定并给结论。

### 2.3 systemd-oomd（Ubuntu 22.04 默认启用，会连整个 user session 一起杀）

- 触发条件（默认）：**PSI `some avg60` > 60% 持续 30 秒**，或 **swap 使用率 > 90%**。
- **判据**：`swapon --show` + `free -g`。swap 用量接近 0 就基本不会触发（加载权重是 mmap 读盘，内存压力很低）。
- **无 swap 或 swap 未用** → 该风险不成立；`00-` 脚本 §9.4 会自动判定。
- 保守做法：加载 27B 权重期间别在服务器上跑浏览器等大内存程序；生产用 systemd unit（属 system slice）比在交互式终端里 `setsid` 起更稳。

### 2.4 GPU 分配

| GPU | 角色 | 显存预算（实测基线口径） |
|---|---|---|
| **GPU0** | PaddleX 产线（版面 + PP-OCRv5 快路径，`GPU_OCR_PADDLE=0`） | ~1.5 GB |
| **GPU1** | PaddleOCR-VL-1.6（vLLM，~4.9 GB）+ Qwen3.8 TP 第 1 片（~10.8 GB） | ≈ 15.7 GB |
| **GPU2** | Qwen3.8 TP 第 2 片（~10.8 GB）+ Laya 打分服务（~1.5 GB） | ≈ 12.3 GB |
| **GPU3** | 空出来给影像管线 / 第二实例 / 实验 | 24 GB |

### 2.5 端口分配

| 端口 | 服务 | 备注 |
|---|---|---|
| 8100 | PaddleX 产线 `/layout-parsing` | Swagger 在 `/docs`，根路径 404 是正常的 |
| 8110 | PaddleOCR-VL-1.6（vLLM，OpenAI 兼容） | 难块走 `/v1/chat/completions` |
| 8102 | **Qwen3.8-27B**（vLLM，OpenAI 兼容） | 数据处理 + 对话 + 仲裁 |
| 8111 | **Laya 打分服务**（本仓库 `laya_service.py`） | `/score` `/classify` |

> `--gpu-memory-utilization` 是**预申请，不是上限**。卡上剩 2 GB 时给小值没用，**会直接 OOM 起不来而不是排队**。起服务前先看 `nvidia-smi`。

---

## 3. 交付物清单

| 文件 | 干什么 | 在哪跑 |
|---|---|---|
| `config.env` | **唯一配置文件**（路径/卡/端口/模型/口径） | 服务器，被其他脚本 source |
| `00-stop-all-and-verify.ps1` | 停旧服务 + 收显存 + 全量核查机器现状 + **§9 显存/Xorg/oomd 体检** | **Windows**（SSH 过去） |
| `01-setup-envs.sh` | 建三套 conda 环境，含版本门禁 | 服务器 |
| `02-download-models.sh` | 拉三份权重（魔搭优先，自动校验量化与头数） | 服务器 |
| `10-start-ocr.sh` | 起 OCR（A 路 PaddleX / B 路 vLLM） | 服务器 |
| `11-start-qwen.sh` | 起 Qwen3.8-27B（含 TP 整除预检、可选参数探测） | 服务器 |
| `12-start-laya.sh` | 起 Laya 打分微服务 | 服务器 |
| `clear-proxy.sh` | 代理体检（只读）+ `source ... --clear` 清理；含「直连 200 却报 ProxyError」口诀 | 服务器 |
| `15-check.sh` | **分阶段检查器**：任意时刻只读体检任一环节（`s1`~`s8`），含日志真因速查 | 服务器 |
| `laya_forms.json` | **NIHSS 量表（13 行 / 15 计分项）+ 病历快判题面**（唯一真相源） | 服务器 |
| `laya_service.py` | Laya 的 HTTP 封装（含置信度多路兜底） | 服务器 |
| `22-score-pipeline.py` | 打分编排：Laya → 低置信升级 Qwen → 合并落证据 | 服务器 |
| `20-smoke.sh` | 端到端冒烟验收（6 组、逐项 ✓/✗） | 服务器 |

部署顺序：`00 → 01 → 02 → 10/11/12 → 20 → 22`。**每一步的验收不过就别往下走。**

### 3.1 怎么在每一步"看一眼"：`15-check.sh`

这是**唯一一个只读、可以任意时刻跑、跑多少次都行的脚本** —— 它不改状态、不重启服务、不杀进程，只是为了让你在任何一个环节都能当场看清"到哪了、对不对"。

```bash
bash 15-check.sh --list        # 列出 8 个阶段的编号与名字
bash 15-check.sh               # 从头到尾检查一遍（默认）
bash 15-check.sh s1            # 只查环境
bash 15-check.sh s2 s4         # 查权重 + Qwen 后端
bash 15-check.sh --log qwen    # 看某路日志的尾部 + 常见真因关键词命中次数
```

**阶段号与部署步骤的对应关系**（编号不是随便定的，`s1` 就对应 `01-`）：

| 阶段 | 对应做什么 | 检查内容（只读） |
|---|---|---|
| `s0` | `clear-proxy.sh` / conda 源 | **代理变量与持久配置、`no_proxy` 是否含本机地址**、tuna 镜像可达性、**solver 是不是 libmamba**、**`~/.condarc` 里有没有 free 频道**（conda create 卡死三主因） |
| `s1` | `01-setup-envs.sh` | 三套环境是否都在、`import` 是否通、**paddle 能否看到卡**、`nvidia-*-cu12` 轮子在不在、`transformers>=5.8`、`ninja` 是否在位、PATH 有没有被 fsl 抢 |
| `s2` | `02-download-models.sh` | 两份权重在不在、**量化是不是 W4A16**、**`QWEN_TP` 是否整除注意力头数**、权重体积与分片数、Laya 缓存 |
| `s3` | `10-start-ocr.sh` | A/B 两路端口与接口是否通、**最近一次识别的 `truncated` 是否为 0、空块是否留痕** |
| `s4` | `11-start-qwen.sh` | `/v1/models` 模型名、对话是否通（含耗时）、**JSON Schema 是否生效**、**回答里有没有混进 `<think>` 思考块** |
| `s5` | `12-start-laya.sh` | `/health`、检查点是否 preload、**量表返回的 `confidence_source` 是什么**（这一条决定升级逻辑可不可信）、**路由到的是不是 multilingual** |
| `s6` | `22-score-pipeline.py` | 量表自检（34/42/3 三个数）、已产出多少份 `nihss.json` |
| `s7` | 机器级风险 | 每卡 `总/已用/空闲/驱动基线`、**图形侧占卡**、**僵尸占卡 pid**、**oomd + swap 判定** |
| `s8` | systemd / 运维 | 三个 unit 的 `enabled`/`active`、`ulimit -n`、日志目录清单 |

输出用三种标记区分严重度：**`✓` 通过 · `!` 提醒（不是错误）· `✗` 需要处理**，末尾给 `通过/警告/失败` 三个计数，并且**只有出现 `✗` 才返回非 0**（方便接进 CI 或 `&&` 链）。

### 3.2 出问题时怎么定位（比翻整份日志快得多）

```bash
bash 15-check.sh --log qwen      # 尾部 60 行 + 自动统计真因关键词命中次数
```

它会打印这些关键词在日志里的出现次数：`EngineCore` / `Traceback` / `ninja` / `nvcc` / `out of memory` / `not supported` / `flashinfer`。
**哪个非 0 就先看哪个** —— 比从头 `less` 快得多，也避免被 `Engine core initialization failed` 这个外壳日志带偏（见 §11.3）。

落盘日志位置：

| 服务 | 日志 |
|---|---|
| Qwen3.8-27B | `~/medrec/logs/qwen.log` |
| OCR A 路（PaddleX） | `~/medrec/logs/ocr_paddle.log` |
| OCR B 路（vLLM） | `~/medrec/logs/ocr_vllm.log` |
| Laya 打分 | `~/medrec/logs/laya.log` |

---

## 4. 阶段一 · 停旧服务并核查（机器接管）

> 🔍 **随时查看本环节**：`bash 15-check.sh s7`（每卡基线占用、图形侧占卡、僵尸占卡 pid、oomd+swap 判定）

在 **Windows 本机**执行：

```powershell
cd D:\visonalbe\deploy-v2
.\00-stop-all-and-verify.ps1 -Ssh ubuntu@<服务器IP>
```

它会做 8 件事并留一份报告到 `deploy-v2\_reports\inventory-<时间戳>.txt`：

1. 基本信息 / GPU 现状（每卡 driver、总/已用/空闲显存）
2. **谁在占卡** + 端口监听 + 相关进程（这就是旧服务清单）
3. 已有资产体积（`~/medrec/models`、`jobs`、`src`）+ 磁盘余量
4. **工具链**：`gcc/g++/nvcc/ninja/ptxas` 是否在位（决定 vLLM 能否落地）
5. conda 环境列表 + **PATH 里有没有被 fsl/freesurfer 抢占**
6. `nofile` 上限 + pip 镜像/魔搭可达性
7. **停服务**：先 `systemctl stop+disable`，再按进程名 kill，最后按端口兜底清残留
8. 迁移清单：扫 `~/medrec/src` 里的 `wsl.exe` / `/mnt/c` / `C:\` 残留

**验收（报告里必须看到）**

| 检查项 | 通过标准 | 不通过怎么办 |
|---|---|---|
| 4 张卡空闲 | 每卡 `memory.used` < 500 MiB | 报告第 7 节的端口兜底会清；还占着就按 `nvidia-smi --query-compute-apps=pid` 列出的**确切 pid** 清（别用 `pkill -f python` 这种宽匹配，会打到桌面组件） |
| 无残留计算进程 | `query-compute-apps` 为空**或**没有 `[Not Found]` 条目 | §9.2 会标出查不到进程名的占卡 pid，按 pid `kill -9` |
| 驱动基线 | 每卡 `total − used − free` ≈ 480 MiB | 这是正常的 OS/驱动占位，不是泄漏；记下来用于算 GMU 余量 |
| 图形会话 | §9.3 输出 `≤64 MiB → 无活跃图形会话` | 有会话（数百 MiB~GB）**不要停 gdm**，只需给 `QWEN_GMU` 留余量；桌面里有浏览器就先关掉 |
| systemd-oomd | §9.4 判定 swap 用量低 / 无 swap | swap 已用 >50%：加载前先腾内存，否则 oomd 可能连带杀掉会话与正在加载的 vLLM |
| `ptxas --help \| grep -c -- --compress-mode` | 输出 **0 是正常的**（老工具链） | 见 §11.3：0 就要带 `VLLM_USE_FLASHINFER_SAMPLER=0`，本手册脚本已默认带上 |
| 磁盘 | `/home` 剩余 > 60 GB | 27B-Q4 约 15 GB + OCR 1.8 GB + Laya 1.5 GB + 缓存 |

> ⚠️ **不要删 `~/medrec/models` 和 `~/medrec/jobs`** —— 里面是已下好的 PaddleOCR-VL-1.6 权重和历史病历产物。"不借助之前的环境"指的是 **conda 环境与 Python 包**，不是数据。真要清理，先 `du -sh` 看清楚再动手。

---

## 5. 阶段二 · 三套 conda 环境

> 🔍 **随时查看本环节**：`bash 15-check.sh s1`（环境、版本门禁、paddle 能否看到卡、ninja、PATH）

```bash
cd ~/medrec/scripts        # 或 deploy-v2 落地的任意目录
bash 01-setup-envs.sh
# 想彻底重建： bash 01-setup-envs.sh --recreate
```

**为什么三套（硬冲突，不是偏好）**：见 §0.3。

| 环境 | Python | 关键包 | 用途 |
|---|---|---|---|
| `ocr-env` | 3.12 | `paddlepaddle-gpu==3.2.1`（cu126 源）+ `paddlex` + `paddleocr` | A 路 PaddleX 产线、快路径 |
| `qwen-env` | 3.12 | `vllm` + `openai` + `transformers>=5.8` + `ninja` | Qwen3.8-27B、PaddleOCR-VL 的 vLLM 后端 |
| `laya-env` | 3.12 | `torch`（cu126）+ `laya[serve]` | Laya 打分服务 |

**脚本内置的三道门禁（不过就停）**

1. `paddlepaddle-gpu` **绝不允许 `--no-deps`**（会跳过 7 个 `nvidia-*-cu12` 轮子 → GPU 静默失效跑 CPU，这个坑项目里踩过）。
2. `qwen-env` 装完立刻断言 `torch.cuda.is_available()` 且 `transformers >= 5.8.0`。
3. `laya-env` 装完立刻 `import laya` + 打印 torch/CUDA。

**验收**

```bash
$ENV_OCR/bin/python  -c "import paddle;print(paddle.__version__, paddle.device.cuda.device_count())"
#   期望：3.2.1  4
$ENV_LLM/bin/python  -c "import torch,vllm,transformers;print(torch.__version__,vllm.__version__,transformers.__version__)"
#   期望：torch 2.x  vllm 0.30.x  transformers >= 5.8.0
$ENV_LAYAD/bin/python -c "import laya,torch;print(laya.__version__, torch.cuda.is_available())"
#   期望：0.3.20  True
```

> 如果 `paddle.device.cuda.device_count()` 是 0 —— **停在这里别往下走**，一定是 paddle 装成了 CPU 版或 `--no-deps` 了。

---

## 6. 阶段三 · 下模型（魔搭优先）

> 🔍 **随时查看本环节**：`bash 15-check.sh s2`（权重大小/分片、量化是否 W4A16、TP 是否整除注意力头数）

```bash
bash 02-download-models.sh            # 全部
bash 02-download-models.sh ocr qwen   # 只下指定
FORCE=1 bash 02-download-models.sh qwen
```

**下载策略**：优先 ModelScope（`snapshot_download`），失败自动退到 `HF_ENDPOINT=https://hf-mirror.com`。两路都是 `$MEDREC_MODELS` 下的缓存，落盘后打印实际体积。

**脚本会替你校验三件事（这三件错了后面全是白跑）**

1. `PaddleOCR-VL-1.6` 目录里有 `config.json` → 说明是 vLLM 能读的 HF 格式，不是只有 paddle 权重。
2. Qwen 权重的 `quantization_config`：打印 `quant_method / bits / group_size` → **确认真的是 W4A16**，不是名字带 AWQ 实际是别的。
3. Qwen 的 `num_attention_heads` → **打印它，并据此判断你的 TP 该取几**（必须整除，见 §0.2）。

**期望输出形态**

```
quantization     : awq  bits=4  group=128
text 注意力头     : 16  (kv=8)   → TP 必须整除它
vision_config    : 有（多模态塔在位）
分片 5 个，合计 13.6 GiB
```

**Laya 的检查点**由 `laya` 包按需下载（HF，约 1.5 GB），脚本会预拉 `multilingual` 与 `typed-decisions`。离线环境就把 HF 缓存整目录拷进来再跑。

**验收**

```bash
ls $MODEL_QWEN_PATH/*.safetensors | wc -l    # >0
du -sh $MODEL_QWEN_PATH                       # 期望 ~14-16 GB
```

---

## 7. 阶段四 · 起三路服务

顺序：**OCR → Qwen → Laya**（Qwen 最慢，先把 OCR 起来可以让识别先跑起来）。

> 🔍 **随时查看本环节**：`bash 15-check.sh s3`（A/B 两路端口与接口、最近产物的截断与空块）

### 7.1 OCR 前端

```bash
bash 10-start-ocr.sh paddle     # A 路：PaddleX 产线服务化部署（推荐，接口契约最稳）
bash 10-start-ocr.sh vllm       # B 路：PaddleOCR-VL 权重走 vLLM（快，可作难块升级）
bash 10-start-ocr.sh status
bash 10-start-ocr.sh stop
```

- A 路启动命令：`python -m paddlex --serve --pipeline PaddleOCR-VL-1.6 --host 127.0.0.1 --port 8100 --device gpu:0`
- **产线注册名必须写 `PaddleOCR-VL-1.6`**，写成 `PaddleOCR-VL` 会指向旧组合（PP-DocLayoutV2 + PaddleOCR-VL），多下约 2 GB 且不是 1.6。
- 脚本把**端口预检放在加载 1.8 GB 权重之前** —— 这样端口冲突会秒级失败，而不是等 3 分钟。
- B 路的调用形态（难块）：`POST :8110/v1/chat/completions`，body 里 `content` 走 `image_url` + `text:"OCR:"`。

> 🔍 **随时查看本环节**：`bash 15-check.sh s4`（模型注册名、对话耗时、JSON Schema 是否生效、回答里有没有思考块）

### 7.2 Qwen3.8-27B 后端

```bash
bash 11-start-qwen.sh            # 起
bash 11-start-qwen.sh warm       # 预热（首次要按真实输入编译 kernel，1~3 分钟）
bash 11-start-qwen.sh status
bash 11-start-qwen.sh stop
```

脚本里已经替你处理了四件容易翻车的事：

1. **TP 整除预检** —— 读 `config.json` 的 `num_attention_heads`，不整除就**在起服务之前**报错退出（省掉一次 5 分钟的白等）。
2. **`--reasoning-parser qwen3` 是必备不是可选** —— 该模型的 chat template 每轮都以 `<think>` 开头，不配它会把整段思考塞进 `message.content`，2048 的预算还没开始回答就用完了。脚本先探测本机 vllm 的取值列表，支持才加。
3. **`env/bin` 前置到 PATH** —— 这台机 PATH 被 fsl 抢，vLLM 子进程会看不见 `ninja`/`nvcc`。
4. **失败判读**：进程挂了会直接 `grep` 出 `EngineCore_0 | ERROR | Traceback | ninja | nvcc | out of memory` 附近 15 行 —— 因为 `Engine core initialization failed. See root cause above.` **只是外壳**，真因永远在它上面。

**等价的手敲命令**（脚本就是把它加上预检与环境变量）：

```bash
PATH=$ENV_LLM/bin:$PATH \
VLLM_USE_FLASHINFER_SAMPLER=0 CUDA_VISIBLE_DEVICES=1,2 \
$ENV_LLM/bin/vllm serve $MODEL_QWEN_PATH \
  --served-model-name qwen-27b \
  --host 127.0.0.1 --port 8102 \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.44 \
  --max-model-len 32768 \
  --enable-prefix-caching \
  --guided-decoding-backend xgrammar \
  --reasoning-parser qwen3
```

> **首启 1~3 分钟是正常的**（torch.compile + kernel 编译）。**别中途 kill**，否则缓存全废重来。

> ⚠️ **唯一一个我没法在写文档时替你验证的环节**：本机 vLLM 版本是否认 `Qwen3.8-27B` 的架构（`Qwen3_5ForConditionalGeneration` / `model_type=qwen3_5_text`）。官方 recipe 标注需要 **vLLM ≥ 0.17.0**（NVFP4 路径在 0.26.1rc1 + TP2 上验证过），所以 **pip 装最新稳定版最稳**。
> **验证方法**（30 秒，别省）：启动日志里看到 `Loading model weights took ...` 与 `Maximum concurrency` 就是认了；如果报 `Model architectures [...] are not supported` —— **升 vLLM**（`pip install -U vllm`），不要试图改 config.json 里的 `architectures` 去骗过加载器。

> 🔍 **随时查看本环节**：`bash 15-check.sh s5`（检查点是否 preload、置信度来源、路由到的是不是 multilingual）

### 7.3 Laya 打分前端

```bash
bash 12-start-laya.sh            # 起（首次会下/加载检查点）
bash 12-start-laya.sh selftest   # 只验量表与代码，不连服务
bash 12-start-laya.sh status
```

**Laya 的关键参数（都在脚本里，理由是实测的）**

| 参数 | 取值 | 理由 |
|---|---|---|
| `LAYA_ROUTER_MODEL` | `multilingual` | 中文病历必须用 mmBERT 检查点。英文检查点对**非拉丁文字会归零**（高棉语实测 0.000 准确率却有 0.952 置信度）——**错得很自信，靠置信度门控救不回来** |
| `LAYA_MAX_LEN` | `8192` | 检查点默认 1024 会把长查体直接截断。实测约 4000 token 内稳定（20 题对 16~18） |
| `LAYAD_CONF_MIN` | `0.70` | 低于此升级 Qwen。**先按这个跑一批，用分歧率回调**：分歧率高 → 阈值定低了；升级太多 → 阈值定高了 |
| 串行 + 锁 | 固定 | Laya 33 ms，串行足够；并发会盖掉 CUDA graph 缓冲 |

---

## 8. 阶段五 · 把现有流水线接到 Qwen 上

`server-manual.md` 里的 F 系列脚本原本指向 `--url http://127.0.0.1:8101`（旧的小模型）。**新架构只有一个后端**：

```bash
# F3 结构化抽取。⚠ 注意 structure.py 的接口是「位置参数 compiled_dir」，
#    没有 --work（我早先写错过，实测报 unrecognized arguments: --work）
#    compiled_dir = 含 compiled.json / sections_text.json 的那一层
$ENV_LLM/bin/python ~/medrec/src/structure.py \
  "$MEDREC_JOBS/批次目录/compiled/病历号姓名" \
  --url http://127.0.0.1:8102 --model qwen-27b \
  --schema ~/medrec/src/f3_schema.json \
  --system ~/medrec/src/f3_system.txt

# 先看它打算怎么切块（不联网，最适合第一次跑）
$ENV_LLM/bin/python ~/medrec/src/structure.py "<compiled_dir>" \
  --url http://127.0.0.1:8102 --model qwen-27b \
  --schema ~/medrec/src/f3_schema.json --system ~/medrec/src/f3_system.txt --dry-run

# 断了续跑（每完成一块就落盘）
$ENV_LLM/bin/python ~/medrec/src/structure.py "<compiled_dir>" \
  --url http://127.0.0.1:8102 --model qwen-27b \
  --schema ~/medrec/src/f3_schema.json --system ~/medrec/src/f3_system.txt --resume
```

**实测有效的关键取值**（`--help` 里的默认值）：`--max-chars 5000`、`--max-tokens 2048`、`--timeout 600`、`--temperature 0.0`、默认输出 `<compiled_dir>/structured.json`。整篇一次送要显式 `--whole-file`（须自己确认不超上下文）；`--no-json-schema` 表示不带 `response_format`（本次后端已验证支持 schema，**别加这个**）。

**必须配的三件事：**

1. **关思考**：每个请求带 `"chat_template_kwargs": {"enable_thinking": false}`，否则 JSON 前会多出思考块，解析器会吃到垃圾。
2. **温度 0**：科研要可复现（`temperature: 0`）。
3. **schema 约束**：优先 `response_format={"type":"json_schema",...}`；本机 vLLM 版本不支持时退到 `--guided-decoding-backend xgrammar` 的 `guided_json`（服务启动时已开）。`20-smoke.sh` 第 3 节会实测这一条。

**抽取产物落点不变**：`jobs/<biz_id>/extracted/structured.json` + `provenance.jsonl`（逐变量带 `block_ids` 与模型哈希）。

---

## 9. 阶段六 · NIHSS 打分与病历自动识别

> 🔍 **随时查看本环节**：`bash 15-check.sh s6`（量表自检 34/42/3、已产出多少份 nihss.json）

### 9.1 量表配置

`laya_forms.json` 是**唯一真相源**：13 行（1a / 1b / 1c + 2~11，其中 1a/1b/1c 算 3 个计分项 → 共 **15 个计分项**），每行的 `criteria` 数组顺序 = 分值顺序。

**关于满分口径（我在这上面连错两次，所以固化成断言了）**

| 数字 | 含义 | 怎么来的 |
|---|---|---|
| **34** | 本配置逐项满配合计 | 13 行档位最大分相加；`total_expected` / `total_level` 的上界 |
| **42** | 文献通行的「NIHSS 满分」 | 标准版对**左右肢体分别计分**（左臂/右臂/左腿/右腿 各 0-4），比合并计分多 `4×2=8` 分 → `34 + 8 = 42` |
| **3** | 昏迷特例下的可达上限 | 1a=3 时 1b/1c 记 0 且其余项不再计分 |

所以：**`total_expected` 的上界是 34，不是 42**，两个数字并不矛盾（差在肢体是否拆侧）。要落到标准 42 分制：拆左右肢体后求和，且 `1a>=1` 时把 1b/1c 置 0。

`bash 12-start-laya.sh selftest` 会把这三个数和口径一起打印，并在数字被改动时报错 —— 避免半年后没人记得为什么是 34。

### 9.2 打分编排

```bash
# 自检（不连服务）
python 22-score-pipeline.py --selftest

# 单份文本
python 22-score-pipeline.py --text "查体：神志清，…" --biz-id T001

# 从流水线产物打分（compiled.md 就是给模型的形态）
python 22-score-pipeline.py --file ~/medrec/jobs/T001/compiled/compiled.md --biz-id T001

# 只跑 Laya 建基线（不升级，用来调阈值）
python 22-score-pipeline.py --file x.md --no-escalate
```

**它做的事**：Laya 全量出 15 项 → 挑出 `confidence < 0.70` 或**取不到置信度**的项 → 只把这些项交 Qwen 定档（带 JSON schema + 要求引用原文 ≤30 字）→ 合并 → **分歧项标 `needs_human_review`** → 落 `jobs/<biz-id>/scoring/nihss.json`。

**屏幕摘要长这样**：

```
  Laya 总分     2.4
  最终总分      3     (满分 34)
  升级 Qwen     4 项   分歧 1 项   待人工复核 2 项
  耗时          1820 ms
 ! 5_上肢运动   laya=2 conf=0.58  -> final=3 [qwen(分歧)]
   1a_意识水平  laya=0 conf=0.94  -> final=0 [laya]
```

**判断置信度这一列（本服务唯一需要你核对一次的地方）**

`laya_service.py` 的 `_confidence()` 做多路兜底并把**来源**一起返回（`confidence_source`）：优先 payload 里的 `confidence` 字段 → 次选由 `probs` 分布算归一化熵（score 型）/ `max(probs)`（choice 型）/ `|2p-1|`（noul 型）→ **都拿不到就返回 `None` 并强制升级**。

所以第一次跑完，拿几份真实病历看一眼 `confidence_source` 打印的是什么：
- 如果是 `payload.confidence` → 直接用，阈值随便调都有意义。
- 如果是 `1-归一化熵(probs)` → 说明字段名变了，去 `laya_service.py` 的 `find_key` 名字元组里补上真实字段名，**别让熵兜底长期顶着**（熵是相对量，跨量表不可比）。

### 9.3 病历自动识别（Laya 快判）

同一份服务里的 `/classify`：病案类型、科室、卒中相关、溶栓相关、含否定表述、文书完整度 —— 都是 `<=9` 选项的低基数定档。

```bash
curl -s localhost:8111/classify -H 'Content-Type: application/json' \
  -d '{"text":"入院记录。患者因突发右侧肢体无力2小时入院，头颅CT示左侧基底节区低密度灶，诊断：急性脑梗死。予阿替普酶静脉溶栓。"}' \
  | python3 -m json.tool
```

**红线**：**不要用 Laya 做 498 列抽取**（选项数远超 20，token 预算摊薄，实测会掉到 0.4 级别）。抽取永远走 Qwen + schema。

---

## 10. 阶段七 · 冒烟验收

```bash
bash 20-smoke.sh                    # 只验服务健康
bash 20-smoke.sh /path/样例.pdf     # 连真实识别一起验
```

6 组检查，**任何 ✗ 都别往下走**：

| 组 | 验什么 | 通过标准 |
|---|---|---|
| 0 | 环境 | 三个 env 的 python 版本打印出来；PATH 未被 fsl 抢占 |
| 1 | GPU | 每卡空闲 >20 GB；**无残留计算进程** |
| 2 | OCR | A 路 `/layout-parsing` 回 `errorCode`；给样例时查 **`truncated` = 0** 与空块留痕 |
| 3 | Qwen | `/v1/models` 有 `qwen-27b`；对话可用；**`response_format` 的 JSON Schema 实测生效** |
| 4 | Laya | `/score` 出 15 项；**中文走 `multilingual`**；`/classify` 可用 |
| 5 | 编排 | 量表自检 + Laya→Qwen 升级链跑通 |

**特别留意第 2 组的两行**：`truncated_blocks` 必须为 0（表格被静默截断会把尾部行全丢掉，而日志看起来完全正常）；`missing_blocks` 可以 >0 但**必须可见**。这是 `functional-spec.md` F1.2/F1.3 的验收点。

---

## 11. 常驻与排障

> 🔍 **随时查看本环节**：`bash 15-check.sh s8`（三个 unit 的 enabled/active、ulimit、日志清单）；机器级风险看 `s7`；日志真因速查 `--log qwen`

### 11.1 systemd（三个服务各一个 unit）

```ini
# /etc/systemd/system/medrec-qwen.service
[Unit]
Description=MedRec Qwen3.8-27B backend (vLLM)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/medrec
# systemd 不继承 ~/.bashrc —— 中文日志/输出会崩，这几个必须写进 unit
Environment=CUDA_VISIBLE_DEVICES=1,2
Environment=PATH=/home/ubuntu/anaconda3/envs/qwen-env/bin:/usr/local/bin:/usr/bin:/bin
Environment=HF_HOME=/home/ubuntu/medrec/models
Environment=VLLM_USE_FLASHINFER_SAMPLER=0
Environment=PYTHONIOENCODING=utf-8
Environment=LANG=C.UTF-8
Environment=PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
ExecStart=/home/ubuntu/anaconda3/envs/qwen-env/bin/vllm serve \
  /home/ubuntu/medrec/models/Qwen3.8-27B-AWQ-INT4 \
  --served-model-name qwen-27b --host 127.0.0.1 --port 8102 \
  --tensor-parallel-size 2 --gpu-memory-utilization 0.44 \
  --max-model-len 32768 --enable-prefix-caching \
  --guided-decoding-backend xgrammar --reasoning-parser qwen3
Restart=on-failure
RestartSec=15
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
```

Laya 与 OCR 同理（`ExecStart` 分别指 `laya_service.py` 与 `paddlex --serve`）。

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now medrec-qwen
journalctl -u medrec-qwen -f
```

> **顺序依赖**：`medrec-laya` 与 `medrec-qwen` 都在 OCR 之后起更稳；需要严格顺序就加 `After=medrec-ocr.service` 并在 unit 里配 `Wants=`。
>
> **顺带一个额外好处**：systemd unit 跑在 **system slice**，而 `systemd-oomd` 的"杀 user session"规则不作用于它。所以生产用 systemd 比在交互式终端里 `setsid` 起更抗 oomd（见 §2.3）。

### 11.2 每日检查

```bash
nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu --format=csv,noheader
for p in 8100 8110 8102 8111; do printf "  %-5s " $p; curl -sf -m 2 localhost:$p/v1/models >/dev/null && echo UP || echo down; done
find ~/medrec/jobs -name nihss.json -mtime -1 2>/dev/null | wc -l
```

### 11.3 故障速查

| 症状 | 真因 | 处置 |
|---|---|---|
| `Engine core initialization failed. See root cause above.` | **这是外壳**，V1 引擎在子进程里，真因永远在它上面几行 | `grep -n -B2 -A20 -E "EngineCore_0\|ERROR\|Traceback\|ninja\|nvcc" 日志` |
| **`conda create` 卡住不动**（无输出、无报错） | 三主因：①`~/.condarc` 里有 `restore_free_channel`/`pkgs/free`（求解空间爆炸）②solver 是经典版 ③`default_channels` 指向 repo.anaconda.com（慢但能通） | `bash 15-check.sh s0` 一眼定位；`conda env remove -p <路径>` 后重跑（`01-` 脚本已加预检+300s 超时+venv 兜底）。**别以为网断了** —— `repo.anaconda.com` 返回 200 也可能照样卡 |
| `FileNotFoundError: 'ninja'` | 引擎要现场 JIT，但子进程 PATH 里没有 | `pip install ninja` **且**把 `env/bin` 前置到 PATH（脚本已做） |
| `nvcc fatal: Unknown option '--compress-mode=size'` | FlashInfer 采样器 JIT 撞老 CUDA（`ptxas --help \| grep -c -- --compress-mode` = 0） | `VLLM_USE_FLASHINFER_SAMPLER=0`（脚本已默认），根治靠升 CUDA Toolkit ≥12.4 |
| `Free memory ... less than desired GPU memory utilization` | **显存不够**（不是排队） | 先腾卡，或调小 `--gpu-memory-utilization` |
| `AssertionError: N is not divisible by TP` | TP 不整除 `num_attention_heads`（本模型 **24**） | 取 24 的约数且 ≤ 卡数：**1/2/3/4/6/8**（`11-start-qwen.sh` 已预检并打印可选集） |
| 回答里全是思考、看不到答案 | 没配 `--reasoning-parser qwen3` | 补上，或每请求 `enable_thinking=false` |
| JSON 解析失败 | 没关思考 / 没 schema 约束 | 两者都要（见 §8） |
| 中文打分明显不对 | 路由到了 english 检查点 | `LAYA_ROUTER_MODEL=multilingual`，长文本给 `max_len=8192` |
| **显存被占但没有 PID** | Xorg / 图形组件的占用不出现在 `query-compute-apps` 里 | `nvidia-smi \| grep -iE 'xorg\|gnome'` 看是多少；`00-` 脚本 §9.3 会自动判定。**别去停 gdm**，留余量即可 |
| vLLM 启动时在 CUDA graph 阶段 OOM | 图捕获的分配**不在 `gpu-memory-utilization` 预算内**（调这个值没用） | 降 `--max-num-seqs` / `QWEN_MAXLEN`，或临时 `--enforce-eager`（慢但省） |
| 图形会话/终端突然被杀，vLLM 也一起没了 | `systemd-oomd` 在内存压力下杀整个 user session/cgroup | `journalctl -u systemd-oomd --since "1 day ago"`；生产改用 systemd unit（system slice），并保证 swap 用量低 |
| 所有项都升级给 Qwen | 置信度字段名对不上，走了 `None` 分支 | 看响应里的 `confidence_source`，按 §9.2 补字段名 |
| `[Errno 24] Too many open files` | fd 上限 | `ulimit -n $(ulimit -Hn)`；systemd 用 `LimitNOFILE=1048576` |
| 训练/服务 20 秒就退 | 卡被别人占 | 先 `nvidia-smi` 再 `pip check` |

---

## 12. 降级与备选路径

| 场景 | 方案 |
|---|---|
| `cyankiwi/Qwen3.8-27B-AWQ-INT4` 拉不到 | 换 `abhishekchohan/Qwen3.8-27B-AWQ-INT4` 或 `pearsonkyle/Qwen3.8-27B-GPTQ-W4A16`，只改 `config.env` 的 `MODEL_QWEN_ID` |
| 魔搭/HF 都不通 | 用 `-v` 挂 U 盘/内网盘拷权重；`MODEL_QWEN_PATH` 指向本地目录即可，vLLM 不在乎来源 |
| 显存压不下来 | `QWEN_TEXT_ONLY=1`（丢视觉塔只做文本，脚本会自动探测该开关）；再不行把 `QWEN_MAXLEN` 降到 16384 |
| 要更大的 KV / 并发 | 把 `QWEN_TP` 改成 4（空着 GPU3 时用 1,2,3 会失败，必须 4 卡或 2 卡），`QWEN_GMU` 提到 0.85 |
| 要 Qwen 回看原图仲裁 | 先确认本机 vllm 的 `Qwen3_5ForConditionalGeneration` 多模态 processor 是否注册（HF discussion #146 报告过"被当多模态但无 processor → text-only"）。**先单发一张图测通再加进流水线**，别直接改生产脚本 |
| 只想先要 NIHSS 打分 | Laya 可以在 **CPU** 上跑（193~464 ms/问）：`LAYA_DEVICE=cpu bash 12-start-laya.sh`，完全不占卡 |

---

## 13. 与旧文档的差异（避免两边打架）

| 旧文档说 | 现在以谁为准 |
|---|---|
| `server-manual.md` §10.3/10.4：8101 小模型 Qwen3.5-9B、8102 大模型 Qwen3.5-35B-A3B | **本手册**：新架构只有一个后端 Qwen3.8-27B，占 8102；8101 留空 |
| `tech-stack.md` §1.4：v14 缩到 2 卡、流水线用 2 卡 | **本手册**：v14 全停，不再共存；GPU0-2 归新链路，GPU3 备用 |
| `tech-stack.md` §3：`vl-env` 含 vLLM | **本手册**：vLLM 独立成 `qwen-env`（transformers ≥5.8 与 PaddleX 冲突） |
| `pkg_f5i/UPGRADE-F5.md`：`$PY` 指 `vl-env/bin/python` | **本手册**：区分 `PY_OCR` / `PY_LLM` / `PY_LAYAD`，别再用单一的 `$PY` |
| 历史文档说"yaml 是变量定义表" | 仍然有效；新增的 `laya_forms.json` 只管**量表与快判题面**，两者不重叠 |

---

## 14. 验收总表（照表打勾）

| # | 检查项 | 通过标准 | 命令 |
|---|---|---|---|
| 1 | 旧服务已停 | 4 卡 `memory.used` < 500 MiB，无计算进程 | `nvidia-smi` |
| 2 | 三个环境 | 三行版本号都能打印，且 paddle 看到 4 卡 | §5 验收 |
| 3 | 权重齐备 | Qwen ≥13 GiB 且 `quantization=awq/4bit`；OCR 有 `config.json` | `02-download-models.sh` 输出 |
| 4 | OCR A 路 | `/layout-parsing` 回 `errorCode` | `20-smoke.sh` 组 2 |
| 5 | OCR 质量 | `truncated` = 0，空块留痕 | `20-smoke.sh /path/a.pdf` |
| 6 | Qwen 就绪 | `/v1/models` 有 `qwen-27b`；预热成功 | `11-start-qwen.sh warm` |
| 7 | 结构化输出 | JSON Schema 生效 | `20-smoke.sh` 组 3 |
| 8 | Laya 就绪 | `/score` 出 13 行答案、`/classify` 可用 | `20-smoke.sh` 组 4 |
| 9 | 量表口径 | 自检打印 34 / 42 / 3 三个数且全绿 | `12-start-laya.sh selftest` |
| 10 | 置信度来源 | 真实病历上 `confidence_source` 不是"无置信度字段" | `22-score-pipeline.py --file ...` |
| 11 | 升级率合理 | Qwen 升级项占比 **5%~40%**（>40% 阈值太低，0% 说明门控没生效） | 同上的摘要行 |
| 12 | 端到端 | `20-smoke.sh` 全绿 | `bash 20-smoke.sh` |
| 13 | 常驻 | 三个 unit `enabled` 且重启机器后自起 | `systemctl is-enabled medrec-*` |

---

## 附：本手册的事实来源

- Qwen3.8-27B 架构/量化/TP 约束：[vLLM Recipes · Qwen/Qwen3.8-27B](https://recipes.vllm.ai/Qwen/Qwen3.8-27B)、[vLLM Docs](https://docs.vllm.ai/)
- MTP 崩溃 issue：[vllm-project/vllm#58203](https://github.com/vllm-project/vllm/issues/58203)
- 多模态 processor 缺失：[HF Qwen/Qwen3.8-27B discussion #146](https://huggingface.co/Qwen/Qwen3.8-27B/discussions/146)
- 量化件：`cyankiwi/Qwen3.8-27B-AWQ-INT4`、`abhishekchohan/Qwen3.8-27B-AWQ-INT4`、`pearsonkyle/Qwen3.8-27B-GPTQ-W4A16`
- 本地部署参考（FP8 vs INT4 显存；其 TP 说法有误，见 §0.2）：[Qwen3.8-27B 本地部署全流程指南](https://deepseek.csdn.net/6a82618510ee7a33f29be0f7.html)（注：该文说"视觉编码器 16 头 → TP 只能 1/2/4/8/16"，与本次实测的文本模型 24 头不符，**以实测为准**）
- Laya 引擎与三个检查点、`system_one` 契约、速度/校准数据：[PyPI laya 0.3.20](https://pypi.org/project/laya/)、[API reference](https://nandhakishorm.github.io/laya/reference/agent/)、[GitHub](https://github.com/NandhaKishorM/laya)
- Laya 与 Jev 的对比、>20 选项的短板：[BENCHMARKS](https://github.com/NandhaKishorM/laya/blob/main/BENCHMARKS.md)
- 服务器历史坑位（fsl 抢 PATH、paddle `--no-deps`、vLLM 预申请显存、FlashInfer JIT）：本项目 `server-manual.md` 附录 C 与 `MEMORY.md`
