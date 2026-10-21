# Codex 任务书：medrec 迁移部署包（round52 → 2×L40）

> **执行者**：源服务器（4×RTX 4090）上的 Codex
> **最终目标**：产出可直接拷贝到 2×L40 目标机「一键安装」的部署包 + 自调参工具
> **必读文档**：`medrec-round52-deploy-tune-brief.md`（与本任务书同目录，下称「brief」）

---

## 0. 开工前（必须先做，不许跳过）

1. **通读 brief 全文**，重点 §1–§3（背景与已知事实）、§6（两个交付物边界）、§7（红线）。
2. **现场确认** brief 中标注「需现场确认」的项，把「原始命令 + 原始输出」记录下来，写进 DELIVERY.md §5：
   - §3.5 七个陷阱的线上实际值（`scripts/config.env` 全文、`strata-iq3_s.json` 的 `gpu` 字段、8110 实际 argv：`ps -ef | grep vllm`）
   - 目标卡是 L40 还是 L40S（如可查询）
   - Strata 引擎的实际启动方式（二进制路径 + argv，`ps -ef | grep -i strata`）
3. 查不到的项标 `unknown` 并写清「缺什么信息才能确定」；**不许用「应该/大概/估计」填空**（brief §9）。

**禁止事项（违反即返工）**：
- ❌ 不许改源机器的任何配置、不许重启源机器的服务、不许删源机器的文件
- ❌ 不许把 `run/`、`workspace/`、`logs/` 打进任何包（含患者姓名，红线1）
- ❌ 不许 `pip install` / `conda install` 清单外的包（缺包就报「包名+版本+完整命令」）
- ❌ 不许 heredoc；所有 `curl` 带 `--max-time`；所有脚本带 `set -euo pipefail`

---

## 1. 交付物清单（少一件都不算完成）

全部产物放在 **`~/medrec-deploy-round52/`** 目录下：

| # | 产物 | 要求 |
|---|---|---|
| 1 | `medrec-code-round52.tar.gz` + `manifest.json` | 含 `src/ bin/ scripts/ config/ webui/ docs/`；每个文件带 sha256 |
| 2 | `medrec-assets-round52.tar.gz` + `manifest.json` | 全部模型权重（HF + PaddleX 两份 OCR、PP-DocLayoutV3、PP-OCRv5、Qwen3.8 IQ3_S）+ Strata 引擎 + 离线 wheels + 冒烟用 2 例样本 |
| 3 | `install.sh` | 只做四件事：preflight → unpack+verify → env setup → start+smoke（brief §4.2），**不许内联任何调参逻辑** |
| 4 | `tune.sh` | 只读探测，唯一输出 `workspace/tuning.json`（brief §5），**不改配置、不重启服务** |
| 5 | `start-all.sh` / `stop-all.sh` / `health.sh` | 按 brief §4.4，`health.sh` 输出格式与样例逐字一致 |
| 6 | `DELIVERY.md` | 按 brief §10 六节结构 |

---

## 2. 各脚本硬性要求（逐条落实，验收要勾）

### install.sh
- 子命令齐全：`preflight` / `online` / `offline` / `--rollback` / `--dry-run`，`--help` 能打印全部
- preflight 7 项检查（OS / 驱动 / 卡数型号=恰好2张L40 / 磁盘≥包×2 / 端口8080+8110+8090空闲 / 外网探测定 online|offline / conda），**每项打印 `[OK]` 或 `[FAIL]+原因+补救`，不许静默跳过**
- 解包前先 `tar -tzf | grep` 自查包内无 `run/ workspace/ logs/ __pycache__ .bak`，有则拒绝安装
- 解包后按 manifest.json **逐文件校验 sha256**；顺序：先 assets 后 code
- 幂等：`.installed` 标记 + sha256 判断，重跑全跳过；断点续装用 `install.state`
- `--rollback`：只删自己创建的（用创建清单记账），恢复被改配置的 `.bak`，**不许碰用户原物**
- `--dry-run`：零副作用跑通全流程
- 装配置时落实 brief §3.5 陷阱修正：卡0=OCR 显式 `OCR_VLLM_GPU=0`；`gpu-memory-utilization 0.22`；strata `gpu:[1]`；`max-num-seqs 64`；日志轮转**只能 copytruncate**（执行器 O_APPEND）
- HF 模型路径必须指到 `.../snapshots/master` 层（brief §3.2），脚本里加存在性检查，报错文案写明「HFValidationError = 目录不存在」
- 产物权限：文件 600、目录 700

### tune.sh
- `--level L0|L1|L2` 可单独调用；L3 默认不跑，必须 `--sweep` 显式开启
- 卡型号查找表预置 `4090×4 → R15=24, R23=32`（源机器实测），L40×2 标「待测」
- L2 输出必须有「TPOT 是否落在预期区间」的**明确判断**（预期 = 1800/带宽GB/s ms，L40=2.08ms；>5ms 判「机器有问题，先修机器别调参」）
- `tuning.json` 格式严格按 brief §5.5，`source` 字段只能是 `table:<profile>` / `sweep:<日期>` / `default`
- 只读自证：跑前跑后对 `config/` + `config.env` 做 md5sum 快照比对，不一致就报错退出

### start-all.sh / stop-all.sh / health.sh
- 启动顺序 Strata(8080) → vLLM(8110) → webui(8090)，每个等健康检查通过再启下一个；停止反向
- webui 必须 `cd` 进目录再起（`server.py` 相对路径找 `static/`），只绑 `127.0.0.1`，起完用 `ss` 校验非 0.0.0.0
- 裸 `python`/`pip` 不可用（FSL 劫持），全部用 `/home/ubuntu/anaconda3/envs/<env>/bin/python` 绝对路径

---

## 3. 执行步骤

```
Phase 1  §0 现场确认 + 记录
Phase 2  写 5 个脚本，每个写完立即 bash -n 语法检查 + shellcheck（如已装）
Phase 3  打包：tar 时 --exclude，打完后 tar -tzf | grep 证明无 run//workspace//logs/__pycache__
         生成 manifest.json（文件清单+sha256）
Phase 4  本机自检：
         - bash install.sh --help / --dry-run 跑通
         - bash tune.sh --level L0 / L1 各跑一次（源机器服务在跑，可实测）
         - 对照 brief §8 验收清单逐条勾，没勾的写明原因
Phase 5  写 DELIVERY.md（brief §10 六节），粘贴值打码，不许出现患者身份
```

---

## 4. 完成后输出

打印交付清单：

```
📦 交付清单：
- [x] medrec-code-round52.tar.gz（xx MB，n 个文件）
- [x] medrec-assets-round52.tar.gz（xx GB，n 个文件）
- [x] install.sh / tune.sh / start-all.sh / stop-all.sh / health.sh
- [x] DELIVERY.md
- 验收清单：brief §8 共 15 条，勾 x 条，未勾原因：…
- 未知项 x 条 / 待裁决 x 条（见 DELIVERY.md §5/§6）
```

有任何一条验收勾不上：**停下来报告**，不许隐瞒继续往下做。
