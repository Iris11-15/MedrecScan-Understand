# 项目交接 · 病历结构化取数（细则，按需阅读）

> 本文件**不常驻上下文**：AGENTS.md 只留红线与环境，细则都在这里。需要时 `grep -n '<关键词>'` 定位后再读那几段。

## 一 · 项目是什么

把扫描病历（PDF）变成结构化数据，三层链路：

1. **识别层**：PaddleX 产线（8100）→ `compiled.md` + `tables.json`（表格已展开成矩阵）；难块可用 vLLM 上的视觉语言 OCR（8110）升级识别。
2. **取数层**：按**院区规则**（`~/vital_rules.json`）从表格抽「入院首条」体征 5 列（体温 / 脉搏 / 呼吸 / 收缩压 / 舒张压）。
3. **对账层**：与 `structured_final/登记表.xlsx` 逐列比对，产出差异清单；必要时回填。

**已定口径**：主来源 = 入院记录里「查体 / 体检」那句话；护理记录单（`tables.json`）只作**交叉核对**。两者一致 → 采信；不一致 → 标「待核」。**模型只提议，真数据验收后才写入。**

## 二 · 工具清单

| 工具 | 用途 | 备注 |
|---|---|---|
| `~/vital_extract.py` | 取数主程序 | `--probe` 体检单例 / `--wizard` 生成规则 / `--final` 出交付值 / `--bad --why` 找不匹配并回正文取证 / `--src` 双来源对照 / `--root <批次>` 指定批次（默认写死旧批次，**必须显式传**） |
| `~/vital_rules.json` | 院区规则（生产文件） | **只读**。含 5 个院区 + `default` 兜底块 |
| `~/rule_llm.py` | 让模型提议规则 + 自校验 | 提议 → 校验 → 人确认 |
| `~/apply_vitals.py` | 把结果写进登记表 | 默认 dry-run，`--write` 才落盘 |
| `~/medrec/scripts/pipeline.py` | 链路编排 | `status` / `stage1-ocr` / `stage2-extract` / `stage3-verify` / `stage4-apply` |
| `~/medrec/scripts/docmap.py` / `docsum.py` | 长文档地图 / 分块摘要 | 应对 68K 的 `compiled.md`：**先地图，再点读** |
| `~/medrec/scripts/ledger_report.py` | 登记表逐列填充率 | 统计交给脚本，**不要交给模型** |
| `~/medrec/scripts/mem.py` | 长期记忆（SQLite + FTS5） | 库在 `~/codex-scratch/memory/memory.db` |

## 三 · 术语

| 术语 | 含义 |
|---|---|
| 入院首条 | 该例所有体征表里 `(日期, 时间)` 最早的一条记录 |
| 两来源 | 正文（入院记录查体句） vs 护理单（`tables.json`） |
| 强体征列 | 体温 / T(℃) / TC / MEWS —— 出现即认定是体征表，可忽略排除词 |
| 登记表空 | 该例登记表那几列本来就没值，我们是在**补空**，不是在对账 |
| 无来源 | 该例既没从正文也没从护理单抽到体征 |

**已知坑（来自 `vital_rules.json` 文件头）**：① 别名别用裸 `PR`/`BP`（`PR3-ANCA`、`HbA1c` 会误命中）；② 日期要兼容 `2025/1/10`、`10-26` 及 `2023-11-25 10:52` 同格格式，`MM-DD` 必须两位。

## 四 · 当前状态（2026-09-30）

**数据**：106 例、5 个院区 —— 奉化 20 / 玉环第一 26 / 安吉 20 / 桐乡二院 20 / 长兴人民 20。

**取数结果**（`vitals_final4.csv`，来源批次 `batch_0928_1538`）：

```
两来源一致 11 | 仅护理单 30 | 仅正文 30 | 两来源不一致 8 | 无来源 27
```

**按院区（例数 / 缺失）**：玉环第一 26/21 ｜ 桐乡二院 20/5 ｜ 安吉 20/1 ｜ 奉化 20/0 ｜ 长兴人民 20/0。

**登记表填充率线索**（`ledger_report.py` 与取数日志都指到同一处）：

```
xlsx cols not in JSON items: ['住院号','来源目录','输入文件','NIHSS评分','NIHSS来源','NIHSS升级项']
```

→ 这 6 列在取数产物里**根本没有对应字段**，所以永远是空的：**不是抽取漏了，是没人负责填**（要么生成器补列，要么确认它们该由别的环节写）。

## 五 · 待办（按价值排序）

1. **恢复 8100 PaddleX 产线**：模型目录 `~/medrec/models/PaddleOCR-VL-1.6` 被删，8100 目前起不来 —— 上游断了，整条链路无法端到端跑。
2. **玉环第一 21 例无来源** → 需要院区规则草案（先用 `vital_extract.py --probe` 真跑 3 例取证，再照「奉化」的结构写 JSON，每条规则必须能对到 evidence）。
3. **登记表 6 个"没人填"的列** → 定位生成登记表的脚本，判断是补列、还是该由别处写。
4. **桐乡二院 5 例**整例没有体征表（只有医嘱单/检验单）→ 建议如实留空并注明。
5. **安吉护理单**是多行表头 + 合并单元格 → 需按列位置取数。
6. **长兴人民 4 例「体温单」**没有列名 → 位置式规则或转人工。

## 六 · 本机环境须知（踩过的坑）

| 坑 | 现象 | 处理 |
|---|---|---|
| PATH 里 fsl 抢 python | 裸 `python`/`pip` 解析到 `/home/ubuntu/fsl/bin/python` | 一律用绝对路径 `/home/ubuntu/anaconda3/envs/vl-env/bin/python` |
| GPU 卡号 | 手写命令漏 `CUDA_VISIBLE_DEVICES` → 抢到别人的卡 → `Engine core initialization failed` | 只用脚本启停服务，别手搓 vllm 命令 |
| 无效代理 7897 | 所有 HTTP（含 ModelScope/pip/git）连接被拒 | 命令前加 `env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY` |
| FlashInfer | `Ninja build failed`（fp8 KV + sm89 + head_dim 256 触发的 JIT） | `VLLM_USE_FLASHINFER_SAMPLER=0` + `--attention-backend TRITON_ATTN` + 不用 fp8 KV |
| 配置键被忽略 | Codex 会 `warning: ... is ignored` | 见到就删那个键（例：`model_max_output_tokens` 在 0.159.2 已废） |
| 启动脚本旧模型 | `11-start-qwen.sh` / `13-svc-all.sh` 仍指向已删除的 27B | **别再跑它们**；用 `start-agent.sh` |

## 七 · 回执格式（每次任务最后打印）

```
做了什么：
产出在哪：（绝对路径）
缺什么：
建议下一步：
```
