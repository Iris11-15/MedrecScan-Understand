# MEDREC 运行操作手册（日常运转）

> **本手册管什么**：机器**已经装好之后**，日常怎么开、怎么跑、怎么查、怎么关、坏了怎么办。
>
> **本手册不管什么**（去看对应的文档，别在这里找）：
>
> | 你要做的事 | 去哪 |
> |---|---|
> | 从零装环境、下模型、起服务 | `DEPLOY-v2-全套部署流程.md` |
> | 为什么这么选（显存账、TP 头数、环境为什么不能合并） | 同上 §0–§2，以及 `medrec-v2-架构与部署手册.md` |
> | F6 分流为什么改、改的依据链条 | `F6分流改动说明.md` |
>
> 版本：2026-09-28 · 对应部署包 `deploy-v2-pkg-20260928-r3.zip`

---

## 第 0 步（每次接手先跑一次）：认清这台机器

这份手册里凡是带 ⚠ 的地方，都是**我没法从交付包里确认、只能在你机器上看到**的东西
（某个脚本的实际位置、某个 CLI 到底有没有某个子命令）。先跑这一段，把真实情况打印出来，
再往下走就不会走偏：

```bash
bash ~/medrec/scripts/13-svc-all.sh list
bash ~/medrec/scripts/13-svc-all.sh status
ls -l ~/medrec/scripts/*.sh
for e in ocr-env qwen-env laya-env llmsrv medrec; do printf '%-10s ' "$e"; [ -x ~/anaconda3/envs/$e/bin/python ] && ~/anaconda3/envs/$e/bin/python -V 2>&1 || echo '（不存在）'; done
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader
command -v medrec && medrec help | head -20
ls -l ~/medrec/src/f6.sh ~/medrec/src/route.py ~/medrec/src/arbiter.py 2>&1
```

**这段输出决定下面哪些命令能照抄**，尤其是 `medrec` 的子命令集合 —— 见 §7 待确认清单。

---

## 1. 服务地图

五个常驻服务。**四个归 `config.env` 管，第五个是遗留的**。

| 名字 | 端口 | 干什么 | 卡 | 谁负责启动 | PID 文件 | 日志 |
|---|---|---|---|---|---|---|
| `ocr` | 8100 | PaddleX 版面 + PP-OCRv5 快路径 | GPU0 | `10-start-ocr.sh paddle` | `run/ocr_paddle.pid` | `logs/ocr_paddle.log` |
| `ocrvl` | 8110 | PaddleOCR-VL-1.6（0.9B，vLLM） | GPU1 | `10-start-ocr.sh vllm` | `run/ocr_vllm.pid` | `logs/ocr_vllm.log` |
| `qwen` | 8102 | Qwen3.8-27B Q4，TP=2 | GPU2 + GPU3 | `11-start-qwen.sh start` | `run/qwen.pid` | `logs/qwen.log` |
| `laya` | 8111 | Laya 打分微服务（`laya_service.py`） | GPU1 | `12-start-laya.sh start` | `run/laya.pid` | `logs/laya.log` |
| `qwen-small` | 8101 | **遗留服务，无人管理** | GPU1 | `13-svc-all.sh` 自己管 | `run/qwen_small.pid` | — |

路径前缀都是 `~/medrec/`（`MEDREC_ROOT`）。

**三种「就绪」的探法不一样**，这是最容易误判的地方：

| 服务 | 探法 | 注意 |
|---|---|---|
| 8100 / 8111 | 自建服务 | **根路径 404 是正常的**，别拿 `curl /` 判断死活 |
| 8101 / 8102 / 8110 | vLLM，OpenAI 兼容 | `curl -s <base>/v1/models` |

> ⚠ `qwen-small`（8101）来自第五个 conda 环境 `llmsrv`，原始工作目录是个**已删除**的目录
> （`/home/ubuntu/f3-hc`）。脚本能 start/stop 它，但**先确认还有没有人在用它**——
> 老的 `medrec` CLI 里 `LLM_URL` 默认就指着它（见 §4 的坑）。

---

## 2. 开关服务：`13-svc-all.sh`

一条总控脚本，**不重复实现启动逻辑** —— 谁有专职脚本就调谁。

| 命令 | 作用 |
|---|---|
| `bash 13-svc-all.sh list` | 只列出五个服务、端口、谁在跑（不动任何东西） |
| `bash 13-svc-all.sh status` | 同上 + 每组一行状态判断 |
| `bash 13-svc-all.sh start` | 起全部；**已在跑的不动**（幂等） |
| `bash 13-svc-all.sh start ocr laya` | 只起指定的几个 |
| `bash 13-svc-all.sh stop` | 停全部 |
| `bash 13-svc-all.sh stop ocrvl` | 停单个 |
| `bash 13-svc-all.sh restart qwen` | 重启单个 |
| `bash 13-svc-all.sh wait` | 等就绪，默认 300 秒超时 |
| `bash 13-svc-all.sh refresh` | **接管**：把当前真正在监听的进程写进 PID 记录 |
| `bash 13-svc-all.sh free-gpu0` | 为训练腾出 GPU0：按「谁真的占着 GPU0」来停，不靠猜 |

### 每日两条命令

```bash
bash ~/medrec/scripts/13-svc-all.sh start
bash ~/medrec/scripts/13-svc-all.sh wait
```

收工（要腾卡的时候）：

```bash
bash ~/medrec/scripts/13-svc-all.sh stop
```

### 三条必须记住的脾气

1. **`ocr` 和 `ocrvl` 要分开停。** `10-start-ocr.sh stop` 会**同时**停掉两条 OCR 通道；
   只想停 B 路就用 `13-svc-all.sh stop ocrvl`。这是总控存在的主要理由之一。
2. **手工重启过服务，就一定要跑一次 `refresh`。**
   PID 记录过期后，`status` 会报错、`stop` 会停不掉（它按 PID 文件找进程）。
   `refresh` 会用 `ss` 反查真正的监听进程并改写记录。
3. **`start` 不会重启动已在跑的服务。** 要应用新参数（比如改了 `config.env`），
   必须 `restart <名字>` 或先 `stop` 再 `start`。

另外：`start` / `status` / `restart` / `refresh` 会**先做一次代理体检**（脚本里的 `check_proxy`），
因为代理坏掉时能起服务的假象和真象很难分清。看到它报警先看 §7.3，**别顺手清代理**。

---

## 3. 运行主线：一份病历从 PDF 到带 NIHSS 的大表

七步。**每步的产物就是下一步的输入**，卡住时先确认上一步的产物真的写出来了。

> ⚠ **表里的 `<...>` 是槽位，不要直接粘进终端。** 尖括号会被 bash 当成重定向，
> 报 `bash: F6: No such file or directory` —— 这个坑真实发生过（`nihss_doctor.sh`
> 整个脚本就是为了绕开它才写的）。整条链路要跑，用下面 §3.1 的入口，它没有占位符。

| 步 | 做什么 | 命令形式（槽位版） | 产物 |
|---|---|---|---|
| 1 | PDF → 逐页 PNG | `medrec pdf2png <输入...> --out <页目录>` | `<页目录>/*.png` |
| 2 | 分层识别 | `medrec recognize <图片目录> --out-dir <raw>` | `<raw>/blocks.json` 等 |
| 3 | 适配成 F2 页数组 | `medrec pages <raw> --out <pages>` | `pages.json` |
| 4 | F2 编译 | `medrec compile <pages.json> --out <compiled>` | `compiled.md` 等 |
| 5 | **F3 结构化** | `bash run_f3_fast.sh <compiled>` | `structured/structured.json` |
| 6 | **F6 分流定档** | `bash f6.sh <文本目录> <输出目录>` | `<stem>.route.json` / `.final.json` / `.prov.jsonl` |
| 7 | **拼宽表（带 NIHSS 列）** | `python f3_to_wide.py --indir <F3结果目录> --out 登记表.xlsx --nihss <F6输出目录>` | `登记表.xlsx`，第 5 列 = `NIHSS评分` |

第 1–4 步也可以一条命令跑完（= `batch_run.sh`）：

```bash
medrec run <输入目录>
```

### 3.1 整条链路的现成入口

不想一步步敲，用这两个：

```bash
bash ~/medrec/scripts/nihss_doctor.sh 2>&1 | tee ~/nihss_doctor.log
```

`nihss_doctor.sh` 是**一条命令摸清现状**：解释器、有哪些批次、F6 六件套在不在、F6 跑过没有、
金标准 xlsx 在哪，最后**顺便把 NIHSS 列拼进大表**。只读为主（唯一会写的重拼出来的
`登记表.xlsx`，幂等）。把整份 log 贴回来就能定位问题。

> ⚠ 它是为了「不让用户粘贴一长串命令」而写的 —— 手工粘贴时说明文字会被一起带进终端，
> 或尖括号占位符被 bash 当重定向（`bash: F6: No such file or directory`）。
> **脚本里没有占位符、没有需要手填的路径，全部自己探测。** 这也是本手册所有命令块
> 都不带 `<占位符>` 的原因。

---

## 4. ⚠ 两代 CLI 的坑（最容易白干半天的地方）

**已确认（2026-09-28，读 `medrec-cli-v2` 的 `README-f3-swap.md`）：服务器上是 v2 那代。**

| | 老 CLI（工作区里那份 `medrec`，2026-09-23） | **v2 CLI（服务器上跑的）** |
|---|---|---|
| F3 子命令 | `structure` | **`structure` 已移除**；改为 `route` / `route-one` / `route-dry` |
| 宽表 | 无 | `wide` |
| 一条龙 | `run` | `run`（五步 + 批量 F3 + 出登记表） |
| `batch_run.sh` 的 `LLM_URL` 默认 | `8101`（`qwen-small`） | **`8102`**（v2.1 改的） |

v2 的完整子命令集合：

```
doctor | env | pdf2png | recognize | pages | compile | route | route-one | route-dry | wide | run
```

敲 `medrec structure` 会提示改用 `route` / `route-one`。

> ⚠ 别拿老 CLI 的默认值去判断服务器：老那份写死 `LLM_URL=8101` / `LLM_MODEL=qwen-small` /
> `PY=envs/vl-env`。v2 里 `medrec run` 会 export `LLM_URL` 覆盖 `batch_run.sh` 的默认，
> 所以走 CLI 一直是对的；**只有直接 `bash batch_run.sh` 才会咬人**。
> 跑之前先看生效值：

```bash
medrec env
```

推荐的正路（并发才是成本杠杆，单份串行 74.8 s → 8 并发 27.7 s）：

```bash
medrec route ~/medrec/jobs/<批次>/compiled --workers 8
```

```bash
medrec wide <route 的输出目录> --nihss <F6 结果目录>
```

> ⚠ **`--url` 带不带 `/v1` 是个真陷阱。** `config.env` 里的 `QWEN_URL` 是
> `http://127.0.0.1:8102/v1`（**带** `/v1`），而 `structure.py --url` 要的是**不带**的。
> 直接拿 `QWEN_URL` 传进去会拼出 `.../v1/v1/chat/completions`。这个坑在
> `arbiter.py` 上撞过一次，`run_f3_fast.sh` 里写死了 `--url http://127.0.0.1:8102` 就是为它。

---

## 5. NIHSS 这一段单独说

### 5.1 谁在打分：规则，不是 Laya

**结论先记住：NIHSS 打分不要用 Laya。**

| 方案 | 逐格准确率 | MAE |
|---|---|---|
| **规则（`label_nihss.py`）** | **99.1%** | **0.35** |
| Laya 全量微调 8 轮（322M，数小时 GPU） | 95.6% | 0.96 |
| Laya 基座零样本（只在有证据的格子上） | 15.4% | — |
| 平凡基线（全判 0） | 85.3% | — |

F6 改动把「有证据的格子」从 Laya 交回规则之后，端到端**真跑**的成绩：

| 指标 | 值 |
|---|---|
| 逐格（299 格） | **98.0%** |
| 总分 MAE | **0.26** |
| 完全一致 | 78.3% |
| ±1 / ±2 | 95.7% / **100.0%** |
| Qwen 调用次数 | **0** |

`source` 分布：`rule 150 / rule_no_evidence 148 / laya 1`。整条链路**不调大模型**。

5 个剩余错格里，4 个已确认是**我自己的标签错**（已改，见下），1 个待专家定。
**没有任何已知的规则缺陷。**

### 5.2 F6 分流

```bash
bash f6.sh <文本或目录> [输出目录]
```

产物三件套（每个输入一份）：`<stem>.route.json` / `<stem>.final.json` / `<stem>.prov.jsonl`。

F6 用 **13 个键**（`1a_意识水平` … `11_忽视`，5a/5b 合成 `5_上肢运动`、6a/6b 合成 `6_下肢运动`，
合并后满分 34），而 `label_nihss.py` 输出 **15 个键**。中间的映射在 `route.py` 的
`_our_keys()` 里，合并项取两侧**较差**的那个。

### 5.3 回滚（一条命令）

```bash
cd ~/medrec/src && cp route.py.bak-0927-2120 route.py
```

### 5.4 金标准与可复现性

| 文件 | 说明 |
|---|---|
| `nihss_laya_mine23.orig.jsonl` | 人工金标准**原件**（sha256 `97250a66…`），不要改 |
| `nihss_laya_mine23.jsonl` | **修正版**（sha256 `26c4cdc9…`），4 格复核后改动，带 `reviewed=2026-09-27` 标记 |
| `nihss_laya_rules.jsonl` | 943 条规则标注结果（`label_nihss.py` 产物） |
| `nihss_holdout_ids.txt` ⚠ | 留出集 ID 清单 |

修正的 4 格与依据：

| id | 题 | 原 | 改 | 依据 |
|---|---|---|---|---|
| 306 | 6b 右下肢 | 0 | 2 | 「右侧肢体肌力远端3级，近端4级」→ MRC 3 → NIHSS 2；同一句给 5b 判了 2 却给 6b 判 0，**同一证据两套标准** |
| 492 | 2 凝视 | 0 | 1 | 「**双眼向右侧凝视**」= 凝视偏斜 |
| 492 | 3 视野 | 2 | 0 | 全文只有瞳孔/光反射/复视，**无任何视野描述** —— 把凝视的发现误记到视野项 |
| 368 | 3 视野 | 1 | 0 | 同上，只有「对光反射灵敏」 |

> **待专家确认**：`659 / 1a_意识水平`（原文「意识清楚，**反应迟钝**」，0 与 1 都说得通）。

### 5.5 顺带解掉的一个 50 倍疑点

早先记录说「`3 视野`：抽取找到 205 个缺陷 vs 规则只有 4 个」。逐条读原文后：
**这些查体文本极少记录视野结果**（943 条里只有 4 条阳性；唯一提到视野的 425 写的是
「视力视野无主诉障碍，无明显客观发现障碍」→ 0）。所以「205」几乎肯定是**另一个管线在
过度判阳**，规则的 4 更可能对。**「修视野召回」的计划已取消。**

---

## 6. 自检与验收

**纪律：任何一步 ✗ 都不许往下走。** 这一步的 ✗ 会在后面变成看不懂的错。

| 工具 | 命令 | 管什么 |
|---|---|---|
| 分阶段检查 | `bash 15-check.sh` | s0–s8 全量只读检查，**任何时刻都能跑** |
| 单阶段 | `bash 15-check.sh s3` | 只查某一步（`--list` 看全部阶段） |
| 看日志 | `bash 15-check.sh --log qwen` | tail 某路服务的日志 |
| 端到端冒烟 | `bash 20-smoke.sh [样例PDF]` | 全链路走一遍 |
| 现状体检 | `bash nihss_doctor.sh` | 见 §3 |
| 打分自检 | `python 22-score-pipeline.py --selftest` | 不连服务，只验代码与量表 |
| 规则回归 | `python run_tests.py` | 19 个套件 + 5 个检查 = **24 项** |

`15-check.sh` 的设计原则（用的时候放心）：
- **全部只读** —— 不改任何状态，跑多少次都行；
- 阶段号与部署步骤一一对应（`s1` ↔ `01-setup-envs.sh`，依此类推）；
- 每一项都给「怎么查 / 期望值 / 常见不符」，而不是只丢一个 OK/NG。

**改过任何配置或引擎之后，跑 `python run_tests.py`**（24/24 才算过）。

---

## 7. 故障速查（现象 → 真因 → 修法）

按「你看到什么」查，别看序号。

### 7.1 `qwen` 起不来，日志已经刷出 `GPU KV cache size` 之后才抛错

```
ValueError: max_num_seqs (256) exceeds available Mamba cache blocks (234).
Each decode sequence requires one Mamba cache block, so CUDA graph capture cannot proceed.
```

**真因**：这个模型是 **Mamba/线性注意力 + 全注意力**的混合结构（64 层只有 16 层全注意力）。
每个 decode 序列要**独占**一个 Mamba cache block，能分出多少块由 `--gpu-memory-utilization`
决定；GMU=0.80 时只有 234 块，而 vLLM 的 `--max-num-seqs` **默认 256** → 超了。

**为什么看着像「跑一半莫名退出」**：这个检查在 **CUDA graph 捕获阶段**，即权重已加载完、
日志已经刷出 `GPU KV cache size: ...` 之后。

**修法**：`--max-num-seqs` 必须 ≤ 日志里那个块数。本项目设 **64**
（原来稳定跑了两天的那个 27B 进程 cmdline 里的值）。已固化在 `config.env` 的
`QWEN_MAX_SEQS` 与 `11-start-qwen.sh` 里。

**硬天花板** = 启动日志里那句 `available Mamba cache blocks (N)`。

### 7.2 `laya` 起不来：`IncompleteSnapshotError`

```
cached snapshot for 'convaiinnovations/laya' is incomplete: 1 file(s) missing
(typed-decisions/model.safetensors)
```

**真因**：`Router.preload()` 会把路由里的**每一个**检查点都加载一遍
（`multilingual` + `typed-decisions`），而后者不在 HF 缓存里，HF 又连不上。

**修法**：`config.env` 里 `LAYA_PRELOAD=0`（已设）。启动不加载、按需加载 ——
`multilingual` 是完整的，第一个请求会正常加载。**要恢复 preload，先补下那个缺的文件（需网络）。**

### 7.3 `ValueError: Unknown scheme for proxy URL URL('socks://127.0.0.1:7897/')`

**真因**：`all_proxy` / `ALL_PROXY` 是 socks 协议，httpx **在构造客户端时**就崩。

**修法**：只 unset 这两个：

```bash
unset all_proxy ALL_PROXY
```

> ⚠ **绝对不要跑 `clear-proxy.sh --clear`。** 直连 tuna/pypi/modelscope 都是 `000`，
> 把所有代理都清掉 = 彻底断网。

### 7.4 `FileNotFoundError: 'ninja'` 或 `RuntimeError: Ninja build failed.`

**真因**（两个独立的坑叠在一起）：
1. 用绝对路径调 env 的 python **不等于激活该 env** —— 不会把 `env/bin` 加进 `PATH`，
   而 `ninja` 装在那个 env 里；
2. 这台机器 `PATH` 里 `/home/ubuntu/fsl/bin` 排在 `/usr/bin` 前面，
   `gcc`/`g++` 会解析到 FSL 捆绑的老编译器，而报错是 `Ninja build failed.`
   这种**看不出编译器问题**的外壳。

**修法**（涉及 JIT 编译的启动一律这么写）：

```bash
export PATH="/home/ubuntu/anaconda3/envs/<env>/bin:/usr/bin:/bin:$PATH"
export CC=/usr/bin/gcc CXX=/usr/bin/g++
export VLLM_USE_FLASHINFER_SAMPLER=0
```

第三条是兜底：它让 vLLM 用原生采样器，**完全不需要编译**，FSL gcc 的问题也就不再碍事。
`config.env` 已经导出了 `VLLM_USE_FLASHINFER_SAMPLER=0`。

### 7.5 服务「显示就绪」但其实没在跑

**真因**：拿 `curl` 的返回码判断时写成 `code="$(curl ... || echo 000)"` ——
`curl` 已经输出 `000`，`||` 又补一个，得到 `"000\n000" != "000"`，于是**没在跑被判成就绪**。

**修法**：判就绪**两条都要**：HTTP 探活 **且** `ss` 看到监听。

```bash
ss -ltnp | grep -E ':(8100|8101|8102|8110|8111)\b'
```

### 7.6 端口探活正常，但难块全被丢成 `route=unavailable`

**真因**：`layered.py` 的**探活**和**请求**用的是两套地址配置。请求走 `--vl-url`，
探活走 `vl_service.VLLM_PORT`（仓库默认 **8118**）。两者不一致时探活永远失败，
于是 `--no-vl-autostart` 下**每一页的难块都被静默丢弃**。

**修法**：用 `medrec recognize` 跑（它会自动对齐），或手工跑之前先：

```bash
export VLLM_PORT=8110
```

`medrec doctor` 的第 5b 项专门查这个。

### 7.7 `No available memory for the cache blocks`

**真因**：`--gpu-memory-utilization` 是**占卡总量的比例**，而且**不感知同卡已被别的服务占用的部分**。
同卡跑着 PaddleX / OCR-VL 时，vLLM 照样按 `总量 × GMU` 算自己的预算，权重一装完就没 KV 了。

**修法**：算「权重 + 别的服务占用」之后还剩多少能当 KV，再决定 GMU。
`11-start-qwen.sh` 有显存账预检，改小会当场报出来。

> Qwen 是 TP=2，GMU 会在卡组里**每张卡都按同一比例预申请** → TP 卡组里**不能有别的服务**
> （0.80 + 0.20 = 1.00 零余量，必挂）。这就是 GPU 分配写成 GPU2+GPU3 的原因。

### 7.8 `Engine core initialization failed. See root cause above.`

**这只是外壳**，真因在它**上面几行**的 EngineCore 日志里。别盯着这一行查。

### 7.9 裸 `python` / `pip` 装错地方

这台机器 PATH 里 `/home/ubuntu/fsl/bin` 排在 conda 前面（fsl/freesurfer 条目重复 20+ 次），
裸 `python` / `pip` 一律解析到 FSL 自带的 Python，`sys.prefix=/home/ubuntu/fsl`。
**曾经把 1.89 GB 的 paddlepaddle-gpu 装进 FSL 的 Python。**

**铁律**：装包/起服务一律用绝对路径：

```
/home/ubuntu/anaconda3/envs/<env>/bin/python -m pip
```

`config.env` 里的 `medrec_clean_path()` 会从 `PATH` 剔除 `*fsl*` / `*freesurfer*`。

> ⚠ `/home/ubuntu/fsl` 与 freesurfer 属于**脑影像工作流，不得删除或改动。**

### 7.10 conda 环境不能合并（现在有五个，别按「三个」去数）

| 环境 | 用途 |
|---|---|
| `ocr-env` | PaddleX + 快路径（OCR A 路 8100） |
| `qwen-env` | vLLM 0.30+ / transformers ≥ 5.8（qwen 8102、ocrvl 8110） |
| `laya-env` | 决策模型（torch 2.14 / transformers 5.x）（laya 8111） |
| `llmsrv` | vLLM 专用 —— **遗留服务 `qwen-small`(8101) 在用** |
| `medrec` ⚠ | F3 结构化（`run_f3_fast.sh` 里写死的就是它） |

版本互相冲突，**这不是洁癖**。注意 `DEPLOY-v2-全套部署流程.md` 写的是「三个环境」——
那说的是**需要新建**的三个；`llmsrv` 和 `medrec` 是这台机器上既有的，别去动它们。

---

## 8. 改文件的纪律（6 次踩坑换来的，别问我怎么知道的）

在这台服务器上改文件，**按危险程度排序**：

| 规矩 | 原因 |
|---|---|
| **长 heredoc 一律不用** | 粘贴会被截断（实测 6 次），**有时不报错**——文件写一半语法还合法，最坏的情况 |
| 含正则/反斜杠/中文的补丁 → `cat > /tmp/x.py <<'EOF'` + `wc -l` 校验 | shell 双引号会吞 `\n`（曾把 `re.split("[。；;\n]")` 写坏） |
| **命令一次一条；超过 8 行就写文件传过去** | 粘贴截断 + HTML 污染（`<pre>`/`<font>` 混进终端导致 bash 语法错误） |
| 改 Python：`.py.new` → `py_compile` → **真的 `import`** → 才 `mv` | `py_compile` **抓不到 NameError**；曾把函数插在它依赖的常量定义之前，运行时才炸 |
| **自检要真 `import` 并跑一遍，不能只 `grep` 文本** | 只 grep 的自检曾打印 `[ok]` 而代码其实是坏的（引用了不存在的 `_L`/`rlv`/`runk`） |
| 不要写 `patch && check \|\| rollback` | 链尾的 `\|\|` 在任何环节失败都回滚，**会抹掉之前所有补丁** |
| 回滚前确认备份点 | 曾回滚到「两个补丁之前」，白做一轮 |
| 改完必须重建包并**投递注册** | 只写进工作区不注册 = 对方拿不到，会被追问「没有这个文件」 |

### 8.1 重建部署包

改完 `deploy-v2/` 里的任何文件，**一条命令**重建清单 + 包（两者绑在一起，防止漏掉一边）：

```bash
cd D:\visonalbe && python _build_pkg_deployv2.py --tag r3
```

它会：重算 `SHA256SUMS`（`SHA256SUMS` 自身不入清单）→ 打平铺 zip → 自检
`testzip` + **非 ASCII 文件名必须带 UTF-8 标志位**。

> ⚠ 最后那条自检不是多余的：`unzip` 遇到没带 UTF-8 标志位(bit 11)的中文文件名会解成
> **乱码**，曾导致 `DEPLOY-v2-全套部署流程.md` 被解成
> `DEPLOY-v2-хЕихеЧщГич╜▓ц╡БчиЛ.md` 而 15-check 报 FAILED。
> 用 Python 的 `zipfile` 打包（会自动设这一位），**不要用 PowerShell 的 `Compress-Archive`**。

### 8.2 打包与投递的边界

`deploy-v2/` 里除 `SHA256SUMS` 外的每个文件**就是**部署包的内容，`deploy-v2/SHA256SUMS`
是它的清单（每行 `<sha256>  <文件名>`，`SHA256SUMS` 自身不入清单 —— 否则自指）。
落地的服务器目录是 `~/medrec/scripts/`。

清单项数会随包增删变化（本手册加入后是 **16 项**），**以 `SHA256SUMS` 的实际行数为准**，
别背数字。

---

## 9. 待确认清单（第 0 步的输出会回答）

写这份手册时，以下事情**从交付包里确认不了**，所以都用 ⚠ 标了：
（第 1 条已由 `medrec-cli-v2/README-f3-swap.md` 确认 → 见 §4）

| # | 待确认 | 怎么确认 |
|---|---|---|
| 1 | ~~`medrec` 有没有 `route` 子命令~~ **已确认：有（v2 CLI）** | — |
| 2 | `nihss_doctor.sh` / `f6.sh` 的**实际路径**（手册按 `~/medrec/scripts/` 写，但脚本自己的注释里写的是 `~/f3-hc/`） | `ls -l ~/medrec/scripts/*.sh ~/medrec/src/f6*.py` |
| 3 | `f6.sh` 是否还需要 `f6_prep.py`（`nihss_doctor.sh` 的「F6 六件套」里列了它） | 同上 |
| 4 | `envs/medrec` 环境是否还在（`run_f3_fast.sh` 依赖它） | 第 0 步的 `for` 循环 |
| 5 | schema 版本到底是 `f3_schema.json` 还是 `f3_schema_v2/v3.json`（工作区三份都在） | `ls -l ~/medrec/src/f3_schema*.json` |
| 6 | `qwen-small`（8101）还有没有人在用 | 问使用方；`ss -ltnp \| grep 8101` |
| 7 | 留出集 ID 清单 `nihss_holdout_ids.txt` 在服务器上的位置（部署包**不含**它，它在 `laya-ft-20260927.zip` 里） | `ls ~/medrec/src/nihss_holdout_ids.txt` |

---

## 附：本手册的事实来源

| 结论 | 来自 |
|---|---|
| 五个服务、端口、GPU 分配、环境路径 | `deploy-v2/config.env`（逐行读过） |
| `13-svc-all.sh` 的全部子命令与三条「脾气」 | 脚本自带 header + `port_of`/`pidf_of`/`probe_of` 函数 |
| 第 1–4 步命令链、`LLM_URL` 默认值陷阱、`/v1` 陷阱 | `medrec` CLI 源码（213 行，逐行读过） |
| F3 参数与 `envs/medrec` | `run_f3_fast.sh`（47 行） |
| F6 键位映射、端到端成绩、4 格金标准修正、视野疑点 | `F6分流改动说明.md`（168 行） |
| `f3_to_wide.py` 的全部参数 | `f3_to_wide.py` 的 `add_argument` |
| `label_nihss.py` 的 15 项与参数 | `label_nihss.py` 的 `ITEMS` / `add_argument` |
| `nihss_doctor.sh` 的用途与「不要粘贴长命令」的理由 | 脚本自带 header |
| `15-check.sh` / `20-smoke.sh` / `22-score-pipeline.py` 的用法 | 三个脚本自带 header |
| `run_tests.py` = 24 项 | 源码的 `SUITES` + `CHECKS` |
| Mamba cache block、heredoc 禁令、FSL gcc、PATH 污染、`patch && check \|\| rollback` | 项目 Mnemon Documents 里的《medrec v2 流水线架构与部署手册》§6/§7 |
| Laya 微调结论、`LAYA_PRELOAD`、代理 socks 坑 | `config.env` 的实测校正注释 + F6 文档 |
