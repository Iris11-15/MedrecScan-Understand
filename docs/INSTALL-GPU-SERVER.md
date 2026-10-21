# medrec 部署指引（2×L40 / Ubuntu）

> 目标机：Ubuntu 20.04+、恰好 2 张 L40、conda 已装、能访问 github.com。
> 一条命令开始装，装完做三项自检即可跑批。

---

## 1. 先确认目标机满足条件

在服务器上跑这两条，30 秒出结果：

```bash
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
df -h / /data /raid5 2>/dev/null; free -g; conda info --base
```

| 项 | 要求 | 不满足时 |
|---|---|---|
| 系统 | Ubuntu 20.04 / 22.04 | preflight ① FAIL |
| 显卡 | **恰好 2 张 L40 / L40S** | preflight ③ FAIL（包按 2×L40 写死） |
| 驱动 | `nvidia-smi` 版本 **≥ 580.65** | preflight ② FAIL |
| 磁盘 | 装抽取模型 **≥ 105 GB**；只要 OCR+宽表 **≥ 15 GB** | preflight ④ FAIL |
| 内存 | 默认档 IQ3_S 需 **62 GB** | 用 `STRATA_MODEL=auto` 自动挑档 |
| conda | `conda` 在 PATH 里 | preflight ⑦ FAIL |
| 网络 | 能出 github.com（抓 Strata）与 pypi.org | 装不了引擎/环境 |
| 端口 | 8080 / 8110 / 8090 空闲 | preflight ⑤ FAIL |

内存档位参考：IQ3_S 62 GB（默认，质量最好）/ IQ3_XXS 60 / IQ2_XS 48 / Q2_0 48。
**默认不会自动降档**——内存不够就停，不会悄悄换个差模型。要自动挑档就显式加 `STRATA_MODEL=auto`。

---

## 2. 安装：一条命令

```bash
curl -fsSL -o bootstrap.sh \
  https://raw.githubusercontent.com/Iris11-15/MedrecScan-Understand/main/deploy/bootstrap.sh
bash bootstrap.sh
```

脚本按顺序做五件事，每步打印 `[OK]` / `[FAIL]`：

1. **环境自检**：curl / sha256sum / tar 在不在；自动挑可用空间最大的分区作为工作目录
2. **下载**：拉 `part00` + `part01` 两个分卷（共 3.4 GiB），**默认断点续传**，断网重跑同一条命令即可接着来
3. **合并 + 校验**：`cat` 合并成整包，比字节数 + 比 sha256 两道都过才算数
4. **解包**：展开到工作目录（约 15 GB）
5. **preflight**：跑 `install.sh --dry-run`，看 7 项是否全绿；最后**问你要不要真装**

真装那段直接交棒给包内 `install.sh`——本脚本不重复实现、不绕过任何检查。

### 常用变体

```bash
bash bootstrap.sh --preflight      # 只到 preflight，不真装（第一次建议先这样摸底）
bash bootstrap.sh --yes            # 不问，直接装
bash bootstrap.sh --skip-strata    # 只要 OCR+宽表，省 90 GB 磁盘
bash bootstrap.sh --verify-only    # 只校验已下载的分卷
```

### 指定工作分区

默认自动挑最大的分区（`/data` `/raid5` `/mnt/data` `/home` …）。想手动指定：

```bash
WORK_ROOT=/data bash bootstrap.sh
```

空间不够时脚本会**直接停下**并列出所有可用分区——比装到 90% 才炸好。

### 国内网络

`install.sh` 建 conda 环境会调 pip，慢的话：

```bash
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash bootstrap.sh --yes
```

---

## 3. 装的过程有多久

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 下载分卷 | 5–45 min | 看带宽，3.4 GiB |
| 解包 | ~6 min | 15 GB |
| pip 三个环境 | 20–60 min | vl-env / ocr-env / qwen-env，约 735 个包 |
| Strata 权重 | 30–90 min | 86 GB，最慢的一段 |
| 起服务 + 冒烟 | ~10 min | 抽取模型冷启动约 9 分钟 |

**总计 1–3 小时。全程别关终端。** 中断了重跑 `bash bootstrap.sh`，已下载的包不会重下。

装坏了回滚（只删本次安装自建的东西，恢复被改的配置备份）：

```bash
cd medrec-deploy-1.0.1-r54-2xl40 && bash install.sh --rollback
```

---

## 4. 装完三项自检（缺一不可）

```bash
cd medrec-deploy-1.0.1-r54-2xl40

bash health.sh                          # 三个服务在不在、是否只绑 127.0.0.1
bash scripts/10-start-ocr.sh check      # OCR 是否真在 GPU 上干活（rc≠0 就别跑批）
bin/medrec doctor                       # 列数 23 / 服务活不活 / 产物根可写
```

预期 `bin/medrec doctor`：`rc=0 FAIL=0 WARN=6`，且 `COLUMNS yaml=23 drop=[] expect_wide=23`。

> `/health` 返回 200 **不代表**服务在 GPU 上干活，必须跑中间那条 check。
> 判活别用 `pgrep`（会误报），用 `curl -s http://127.0.0.1:8110/v1/models`。

---

## 5. 跑第一批

数据摆放有硬约束：**PDF 必须三层** `<源根>/<院区>/<病例目录>/*.pdf`（守卫 G1 硬校验）。

```bash
bin/medrec run /raid5/history_13_15 --dry-run   # 先看计划，不落盘
bin/medrec run /raid5/history_13_15             # 真跑（批次名 = 目录名）
bin/medrec check history_13_15                  # 结构 / 列命中 / FAIL / WARN
bin/medrec stages history_13_15                 # 哪个阶段慢
```

结果在 `<产物根>/<批次>/06_table/wide.csv`（一行一个病例，23 列）。

**两个产物根，别搞混**：

| 跑法 | 产物落点 |
|---|---|
| 网页跑批 | `/raid5/medrec-run/wide/<批次>/`（`MEDREC_WEBUI_RUN_ROOT`） |
| 命令行 `bin/medrec run` | `config/pipeline.yaml` 的 `paths.run_root`（现 `/home/ubuntu/medrec/run/wide`） |

或走网页（推荐给不熟命令行的同事）：起 `webui/server.py` 后浏览器开 <http://127.0.0.1:8090>，
选数据 → 预览跑守卫 G1–G6 → 开始 → 下载打码后的 `wide.csv` / `wide.xlsx`。

---

## 6. 常见故障

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `sha256 不一致` | 分卷被截断 / 代理插了东西 | `rm -rf medrec-deploy-*-dl` 后重跑 |
| preflight ② 驱动 FAIL | 低于 580.65 | 升级 `nvidia-driver-580-server`；确是 575.x 可试 `DRV_MIN=575.57.08 bash install.sh` |
| preflight ③ 卡 FAIL | 不是 2 张 L40 | 本包按 2×L40 写死（卡0=OCR，卡1=Strata）；其它配置需改 `OCR_VLLM_GPU`/`STRATA_GPU` 并跑 `tune.sh --sweep` |
| preflight ④ 磁盘 FAIL | 空间不够 | `--skip-strata`（省 90 GB）或 `WORK_ROOT=/data bash bootstrap.sh` |
| preflight ⑤ 端口 FAIL | 8080/8110/8090 被占 | 停占用进程，或改 `PORT_STRATA`/`PORT_OCR_VL`/`PORT_WEBUI` |
| preflight ⑦ conda FAIL | 不在 PATH | 装 miniconda3 后**重开 shell** |
| 装到一半像卡住 | 正常 | 看 `[..]` 进度行，别 kill |
| pip 超时 | 外网慢 | `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` |
| Strata 装不上 | GitHub 不可达 | `STRATA_GIT_URL` 指内网镜像；已有 GGUF 时 `STRATA_GGUF_DIR=/path` |
| 内存不足 62 GB | IQ3_S 需 62 GB | `STRATA_MODEL=auto bash install.sh` |
| 两批 sha 有差异 | OCR 非完全确定 | 现网 `--max-num-seqs 64` 下 **1.0–2.0%** 属正常噪声，超了才找维护者 |

---

## 7. 六条红线

1. **别 kill / pkill / 重启任何服务。** OCR 起一次几分钟，抽取模型冷启动约 9 分钟，起坏了没有第二个能救。
2. **别删 `run/` 下的旧批次。** 那是上一轮对账证据，想重跑就换批次名。
3. **别改 `config/` 和 `scripts/config.env`**，除非维护者明确说改哪个键。
4. **别把 `wide.csv` / `01_ocr/` 发到外网、聊天工具、网盘。** 全是患者身份信息。
5. **别为了快关守卫、减页、关 crop-blocks。** 少抽字段和跑得快在数字上长得一模一样。
6. **字段空就是 null。** 别用「未提及 / 无 / 否」填，也别让机器猜。

---

## 8. 更多文档

| 想看 | 去读 |
|---|---|
| 网页每个接口、停法、端口绑定 | `webui/README.md` |
| 单例/批量命令、参数、排错 | `docs/HOWTO.md` |
| 端口 / 模型 / 启动命令 / 请求形态 | `docs/interfaces.md` |
| 安全边界、隔离跑法、审计 | `docs/security.md` |
| 包内一页速查 | `deploy/QUICKSTART.txt`、`deploy/DELIVERY.md` |
