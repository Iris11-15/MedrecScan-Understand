# 接口 · 端口 · 拓扑（2026-09-30 实测版）

> 按需阅读：先 `grep -n '<关键词>' ~/medrec/docs/interfaces.md` 定位小节，再读那几十行。**不要整读**。

## 0 · 拓扑一张图

| 卡 | 服务 | 端口 | 说明 |
|---|---|---|---|
| **GPU 0** | PaddleX 版面 + OCR 产线 | **8100** | 上游识别（**当前未起**：产线模型目录被删，见 §3） |
| | OvisOCR2（vLLM） | **8110** | 视觉语言 OCR，权重 1.6GB |
| | Laya 打分微服务 | **8111** | NIHSS 打分 + 病历快判（`laya_service.py`） |
| **GPU 1,2** | **agent-strong**（Qwen3.5-35B-A3B GPTQ-Int4） | **8102** | **Codex 的主模型**，vLLM TP=2 |
| **GPU 3** | 空闲 | — | 备用 |

- Python：`/home/ubuntu/anaconda3/envs/vl-env/bin/python`（脚本、统计、只读取证都用它）
- vLLM：`/home/ubuntu/anaconda3/envs/qwen-env/bin/vllm`（8102 与 8110 都由它承载；**不是** llmsrv、也**不是** vl-env）
- 启停：`bash ~/medrec/scripts/start-agent.sh {start|stop|status|warm}`、`bash ~/medrec/scripts/start-ovis.sh {start|stop|status|warm}`

## 1 · 8102 · agent-strong（Codex 主模型）

### 1.1 判活（唯一正确姿势）

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8102/v1/models    # 200 = 活
```

⚠️ **不要用 `pgrep -f` 判断服务是否在跑** —— Codex 跑在 PID 命名空间里，`pgrep` 只会看到 `pid=1` 并误判。

### 1.2 启动（已验证可用的完整命令）

```bash
nohup env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  CUDA_VISIBLE_DEVICES=1,2 \
  PATH=/home/ubuntu/anaconda3/envs/qwen-env/bin:/usr/local/bin:/usr/bin:/bin \
  VLLM_USE_FLASHINFER_SAMPLER=0 \
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  /home/ubuntu/anaconda3/envs/qwen-env/bin/vllm serve \
  /home/ubuntu/medrec/models/Qwen3.5-35B-A3B-GPTQ-Int4/snapshots/master \
  --served-model-name agent-strong --host 127.0.0.1 --port 8102 \
  --tensor-parallel-size 2 --gpu-memory-utilization 0.90 \
  --max-model-len 65536 --attention-backend TRITON_ATTN \
  --enable-prefix-caching --max-num-seqs 4 --enforce-eager \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 \
  > ~/agent_start.log 2>&1 &
```

参数要点（少一个就会踩坑）：

| 参数 | 为什么必须有 |
|---|---|
| `VLLM_USE_FLASHINFER_SAMPLER=0` + `--attention-backend TRITON_ATTN` + **不带 fp8 KV** | 三者任一漏掉都会撞 FlashInfer 的 JIT 编译失败（见 §8.2） |
| `--enforce-eager` | 绕开 torch.compile；想提速可试着去掉（失败就加回来） |
| `CUDA_VISIBLE_DEVICES=1,2` + `--tensor-parallel-size 2` | 35B 权重 22.7GB，单卡会没 KV（GLM 就是这么折的） |
| `PATH=...qwen-env/bin:...` | 里面是 `ninja` 等构建工具；缺了报 `No such file or directory: 'ninja'` |
| `env -u ...代理变量` | 本机 7897 代理无效，会劫持一切 HTTP 请求 |
| `--tool-call-parser qwen3_coder` | 模型吐的是 **XML** 工具调用；`hermes`（JSON）实测拿不到 `tool_calls` |
| `--reasoning-parser qwen3` | 思考进独立 `reasoning` 项，正文只留答案 |

### 1.3 端点与已验证行为

| 端点 | 用途 | 2026-09-30 实测 |
|---|---|---|
| `GET /v1/models` | 判活 | `200` |
| `POST /v1/chat/completions` | 通用对话 / 工具 | `tool_calls=[{function:{name:get_weather,arguments:'{"city": "北京"}'}}]` ✅ |
| `POST /v1/responses` | **Codex 走的就是它** | `[('reasoning',None,None), ('function_call','get_weather','{"city": "北京"}')]` ✅ |

KV 容量：`GPU KV cache size: 775,034 tokens`，65,536/请求时并发 **11.83x**（显存很宽裕）。

### 1.4 三条自检 curl

```bash
# 通用对话
curl -s -X POST http://127.0.0.1:8102/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"agent-strong","messages":[{"role":"user","content":"回复：就绪"}],"max_tokens":32}' | head -c 300

# 工具调用（chat 协议）
curl -s -X POST http://127.0.0.1:8102/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"agent-strong","messages":[{"role":"user","content":"查一下北京天气"}],"tools":[{"type":"function","function":{"name":"get_weather","description":"查询城市天气","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}],"tool_choice":"auto","max_tokens":128}' \
| /home/ubuntu/anaconda3/envs/vl-env/bin/python -c "import sys,json;m=json.load(sys.stdin)['choices'][0]['message'];print('tool_calls =',m.get('tool_calls'))"

# 工具调用（responses 协议 —— Codex 用的）
curl -s -o /tmp/t.json -X POST http://127.0.0.1:8102/v1/responses -H 'Content-Type: application/json' \
  -d '{"model":"agent-strong","input":"查一下北京天气","tools":[{"type":"function","name":"get_weather","description":"查询城市天气","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}],"tool_choice":"required","max_output_tokens":128}'
/home/ubuntu/anaconda3/envs/vl-env/bin/python -c "import json;d=json.load(open('/tmp/t.json'));print([(o.get('type'),o.get('name'),o.get('arguments')) for o in d.get('output',[])])"
```

## 2 · 8110 · OvisOCR2（视觉语言 OCR）

- 权重：`/home/ubuntu/medrec/models/OvisOCR2/snapshots/master`（1.6GB，`Qwen3_5ForConditionalGeneration`，含 `vision_config`）
- 实测占用：**GPU0 ≈ 6.7GB**（含视觉塔）

```bash
nohup env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  CUDA_VISIBLE_DEVICES=0 \
  PATH=/home/ubuntu/anaconda3/envs/qwen-env/bin:/usr/local/bin:/usr/bin:/bin \
  VLLM_USE_FLASHINFER_SAMPLER=0 \
  /home/ubuntu/anaconda3/envs/qwen-env/bin/vllm serve \
  /home/ubuntu/medrec/models/OvisOCR2/snapshots/master \
  --served-model-name OvisOCR2 --host 127.0.0.1 --port 8110 \
  --gpu-memory-utilization 0.30 --max-model-len 32768 \
  --limit-mm-per-prompt '{"image":1}' --enforce-eager \
  --attention-backend TRITON_ATTN \
  > ~/ovis_start.log 2>&1 &
```

调用形态（**与原来的 PaddleOCR-VL 通道一致**，所以 `layered.py --vl-url http://127.0.0.1:8110` 不用改）：

```bash
/home/ubuntu/anaconda3/envs/vl-env/bin/python - <<'PY'
import base64, json, urllib.request
IMG = "/home/ubuntu/medrec/jobs/batch_0928_1538/pages/奉化__20230728_009/page_0004.png"
b = base64.b64encode(open(IMG, "rb").read()).decode()
body = json.dumps({"model": "OvisOCR2", "temperature": 0, "max_tokens": 1024,
    "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}},
        {"type": "text", "text": "OCR:"}]}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8110/v1/chat/completions", data=body,
                             headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=300).read().decode()[:800])
PY
```

已知答案对照（p4 护理单首行）：**36.7 / 64 / 18 / 128-66** —— 用它验证 OvisOCR2 认不认这张表。

## 3 · 8100 · PaddleX 产线（**当前未起，需先恢复权重**）

- 启动：`bash ~/medrec/scripts/10-start-ocr.sh paddle`（内部走 `python -m paddlex --serve --pipeline PaddleOCR-VL-1.6 --device gpu:0`）
- ⚠️ **现状缺口**：`~/medrec/models/PaddleOCR-VL-1.6` 目录已删 → 8100 起不来。恢复前先核实还有没有别的副本：

```bash
ls -d /home/ubuntu/medrec/models/*/ | sort
du -sh /home/ubuntu/medrec/models/models/* 2>/dev/null | sort -h | tail
```

- 接口：`POST {base}/layout-parsing`、`GET {base}/health`、Swagger 在 `{base}/docs`（根路径 404 是正常的）
- 请求体：`{"file": <base64>, "fileType": 1, "useLayoutDetection": false, "promptLabel": "ocr", "returnMarkdownImages": false, "maxPixels": 1003520}`（`fileType` 0=PDF / 1=图片）
- 响应：先判 `r["errorCode"] != 0` → `r["errorMsg"]`；正文在 `r["result"]["layoutParsingResults"][*]["prunedResult"]["parsing_res_list"][*]["block_content"]`
- 健康：`{"errorCode":0,"errorMsg":"","result":{"currentStatus":"healthy","runningStatus":"running"}}`

## 4 · 8111 · Laya（打分微服务）

- 启动：`bash ~/medrec/scripts/12-start-laya.sh start`
- 端点：`POST /score`（NIHSS 打分，返回逐题 expected/level/confidence）、`POST /classify`
- 判活：`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8111/score`（根路径 404 正常）
- 环境：`laya-env`（torch 2.14 / transformers 5.x）

## 5 · 链路脚本接口

| 入口 | 作用 | 权限 |
|---|---|---|
| `medrec run <输入目录>`（`/home/ubuntu/medrec/src/medrec`） | 整批识别 → 生成 `~/medrec/jobs/<批次>/{pages,compiled,structured_final}` | 人 |
| `~/medrec/scripts/pipeline.py status` | 全链路状态（服务/批次/GPU/汇总）+ 刷新 AGENTS.md 自动区 | **Codex 可跑（只读）** |
| `pipeline.py stage1-ocr <dir> [--dry]` | 起识别批处理（后台） | 人 |
| `pipeline.py stage2-extract [--batch DIR]` | 抽数 → `vitals_final.csv`（先备份旧 CSV，写 `.batch` 标记） | 需人授权 |
| `pipeline.py stage3-verify` | 对账 → `reports/待核清单_*.md` | 需人授权 |
| `pipeline.py stage4-apply` | 默认 dry-run；`PIPELINE_ALLOW_WRITE=1` 才写登记表（先备份） | **只能人** |
| `~/vital_extract.py --probe/--wizard/--final/--bad/--why/--src [--root <批次>]` | 取数 / 体检 / 向导 / 对账 | 只读（除 `--csv*`） |
| `~/medrec/scripts/docmap.py <file>` | 长文档「行号地图」（先跑它再 sed 读） | 只读 |
| `~/medrec/scripts/docsum.py <file> --focus "…" --out <md>` | map-reduce 分块摘要（本机 27B/35B，零 API 费） | 写 scratch |
| `~/medrec/scripts/ledger_report.py --top 40 --csv <csv>` | 登记表逐列填充率统计（**统计不要交给模型**） | 只读 |
| `~/medrec/scripts/mem.py` | 长期记忆 add/recall/list/confirm/stats | 写 scratch |

## 6 · 数据约定

- 批次目录：`~/medrec/jobs/<批次>/`
  - `pages/<病例>/page_00NN.png` —— 页图，编号是 **PDF 顺序**
  - `compiled/<病例>/compiled.md` —— 逐页正文（含 `<!-- ===== 第 N 页 ===== -->` 标记，`docmap.py` 靠它出行号地图）
  - `compiled/<病例>/tables.json` —— 每张表 `{rows, n_rows, n_cols, header_rows}`，`rows` 是矩形矩阵（合并单元格已实体化）
  - `structured_final/登记表.xlsx` —— 交付表
- 日志：`~/medrec/logs/`（各服务自己的）+ `~/medrec/logs/pipeline/`（stage 日志）
- 运行时：`~/medrec/run/*.pid`

## 7 · 网络与代理

- 基本无外网；本机 `127.0.0.1:7897` 是**无效代理**（Clash 默认端口，抄来的配置）。
- 命令前统一加清代理前缀：

```bash
NOPROXY="env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY"
```

- 探网：`$NOPROXY curl -sI --max-time 8 https://www.modelscope.cn | head -2`
- 下载模型（ModelScope）：`$NOPROXY modelscope download --model <repo_id>`（注意 CLI 会用 fsl 的 python，落点在 `~/.cache/modelscope/models/`，需自行搬到 `~/medrec/models/`）

## 8 · 排错速查

### 8.1 `Engine core initialization failed. See root cause above.`
**这只是外壳**。真因在它**上面**的 EngineCore/Worker 日志里：

```bash
L=~/agent_start.log
n=$(grep -n 'Engine core initialization failed' "$L" | head -1 | cut -d: -f1)
sed -n "$((n-60)),$((n))p" "$L"
grep -nE 'ValueError|RuntimeError|assert|not supported|out of memory|is larger than' "$L" | head
```

### 8.2 `RuntimeError: Ninja build failed`（FlashInfer JIT）
链条：FlashInfer 想编 `batch_prefill_with_kv_cache...head_dim_qk_256` 内核 → 失败。**解法就三处**（本机已固定进启动命令）：
`VLLM_USE_FLASHINFER_SAMPLER=0` + `--attention-backend TRITON_ATTN` + **不带 `--kv-cache-dtype fp8`**。

### 8.3 KV 不够：`(0.5 GiB KV cache is needed, which is larger than the available KV cache memory (0.42 GiB))`
说明权重已加载成功，只是 KV 预算太小。按顺序调：`--gpu-memory-utilization` ↑ → `--max-model-len` ↓ → `--cpu-offload-gb N`（内存换显存，慢但能起）。
**教训**：别用「权重装得下」当能否部署的标准 —— 3×24GB 的安全线是**每卡权重 ≤16GB**。

### 8.4 `No such file or directory: 'ninja'`
PATH 里没有 `qwen-env/bin`（`ninja` 在那儿，370KB 可执行）。

### 8.5 端口判活别用 pgrep / 根路径 curl
- 8100 / 8111 是自建服务：**根路径 404 正常**，要看 `/health` 或 `/score`。
- 8102 / 8110 是 vLLM：看 `/v1/models` 或 `/health`。
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

