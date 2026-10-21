# medrec 病历取数产线 · 使用者手册

> 这份是给**放数据、点开始、拿宽表**的人看的。
> 改代码 / 改配置 / 排障请看 `docs/HOWTO.md`、`webui/README.md`、`AGENTS.md`。

## 1. 它做什么，给你什么

输入一批病历扫描件，输出一张宽表（一行一个病例，23 列）。

```
输入  <源根>/<院区>/<病例目录>/*.pdf   或   一个病例一个文件夹的散页图片   或   .zip
  ⓪ locate  定位首页 / 首次病程记录
  ① s1_ocr  整页 OCR
  ② s2_preprocess  清洗
  ③ s3_extract  抽字段
  ④ s4_normalize  标准化（含来源守卫）
  ⑤ s5_qc  质检
  ⑥ s6_table  出表
产物  <产物根>/<批次>/06_table/wide.csv   ← 你要的那张表（产物根见 §5）
```

- 列定义唯一来源：`config/columns.yaml`（当前版本 `2026-10-15.1`，**23 列**）。
- 判重键 = （标准化就诊医院, 病案号, 住院号, 入院日期）。任一分量为空 ⇒ 该行不合并并记 QC。
- 跑批只访问 `127.0.0.1` 上的本机服务（OCR / 抽取模型），病历不出网；网页只绑环回地址，`0.0.0.0` 直接拒绝启动。
- 产物目录 `700`、文件 `600` —— 里面全是患者信息。

## 2. 日常用法：网页三步（推荐给非技术使用者）

```bash
cd /home/ubuntu/medrec
/home/ubuntu/anaconda3/envs/vl-env/bin/python webui/server.py
```

浏览器打开 <http://127.0.0.1:8090>。

1. **选数据**：填本机路径（默认允许 `/raid5` 和 `/home/ubuntu`），或上传 `.zip`。
2. **看计划**：点「预览」，会跑守卫 G1–G6 并列出例数、院区、页数。全过才让点开始。
3. **开始 → 看进度 → 下载**：进度页有各阶段状态、稳态 s/例、日志尾巴；跑完点「下载打码后 wide.csv / wide.xlsx」。

常驻（关终端也不死；重启网页不会打断正在跑的批次）：

```bash
setsid nohup /home/ubuntu/anaconda3/envs/vl-env/bin/python \
  /home/ubuntu/medrec/webui/server.py \
  > /home/ubuntu/medrec/workspace/webui.log 2>&1 < /dev/null &
```

要点：

- 同一时刻只允许一个批次（锁 `<产物根>/_health/run.lock`）。上一个没跑完点开始会被拒；陈旧锁会自动改名接管。
- 网页**没有**：改配置、删批次、启停服务、`--force`。这不是漏了，是有意不给（见 §8）。
- 例数 < 11 时「稳态 s/例」显示为空。口径 = 丢掉**前 10 例**（预热段）再按完成时刻平均（`bin/medrec_cli.py:956 DROP_FIRST = 10`），不是 bug。
- 想换可浏览的目录：起网页前设 `MEDREC_LOCAL_ROOTS=/raid5:/data`（冒号分隔）；暂存根 `MEDREC_STAGE_ROOTS`（默认 `/raid5/.medrec_stage`）。

## 3. 日常用法：命令行一条

```bash
cd /home/ubuntu/medrec

bin/medrec run /raid5/history_13_15        # 批次名 = 目录名，产物 run/wide/history_13_15/
bin/medrec run /raid5/history_19 --batch h19   # 指定批次名（重名会直接拒绝，不会静默加后缀）
bin/medrec run <目录> --detach             # 后台跑，立刻返回并打印日志路径
bin/medrec run <目录> --dry-run            # 只看要跑什么，不落盘
```

跑完 / 跑中检查：

```bash
bin/medrec stages <批次>          # 各阶段速度表（哪个阶段慢，一眼看出）
bin/medrec check  <批次>          # 结构、列命中、FAIL / WARN
bin/medrec check  A --compare B   # 两批逐格 sha 对照（重跑一致性）
bin/medrec doctor                 # 环境体检：服务活不活、列数对不对、产物根可写
```

只重出表（不重跑 OCR）：`cd src && /home/ubuntu/anaconda3/envs/vl-env/bin/python -u -m wide.s6_table --batch <批次>`。
单独重跑某一例见 `docs/HOWTO.md` 的六条 `python -m wide.*` 命令。

## 4. 数据怎么摆（决定能不能跑起来）

| 形态 | 摆法 | 注意 |
|---|---|---|
| **PDF** | `<源根>/<院区>/<病例目录>/xxx.pdf` | 深度必须是 3 层（守卫 G1 硬校验） |
| **散页图片** | 一个病例 = 一个文件夹，里面全是这个病人的页 | 层深可以不一样（会自动归一）；支持 `.jpg .jpeg .png .tif .tiff .bmp` |
| **.zip** | 网页上传或给本机路径都行 | 先做三检（G5）再解压归一，坏包会被拒 |

- 散页图片会在起跑前**自动合成一个 PDF**（按自然序拼页），之后走和 PDF 完全一样的流程。
- 图片文件名用定宽数字最稳（`001.jpg … 012.jpg`）。产线走**自然序**，`10.jpg` 不会排到 `2.jpg` 前面；页序错了 ⇒ 首页定位和字段抽取全废。
- 一个文件夹只能是一个病人。混了两个病人会被守卫拦（见 §6）。
- 病例目录名会进日志。建议用 `院区_住院号` 这种命名，别把患者姓名放目录名（网页显示时打码，但原始日志不改）。

## 5. 结果在哪、怎么拿

| 要什么 | 在哪 |
|---|---|
| 宽表（打码） | 网页「下载打码后 wide.csv / wide.xlsx」 |
| 宽表（原文） | `<产物根>/<批次>/06_table/wide.csv`、`wide.xlsx`（磁盘上这两份**不打码**） |
| 质检标记 | `<产物根>/<批次>/05_qc/_summary/qc_flags.csv` |
| 各阶段中间产物 | `<产物根>/<批次>/00_index … 05_qc/<case_id>/` |
| 跑批日志 | `<产物根>/<批次>/_pipeline_logs/` |
| 归一/合并清单 | 暂存目录下 `_merge_manifest.csv`、`_merge_alerts.json`（默认暂存根 `/raid5/.medrec_stage`） |

- **产物根有两个，别搞混**：网页跑的批次落 `/raid5/medrec-run/wide/<批次>/`（环境变量 `MEDREC_WEBUI_RUN_ROOT`）；命令行 `bin/medrec run` 落 `config/pipeline.yaml` 的 `paths.run_root`（现 `/home/ubuntu/medrec/run/wide`）。旧批次的根在网页里只读可见，不搬不删 —— 找不到批次先确认它属于哪个根。
- 网页上「查看原文」只在浏览器显示、不落盘，审计会记时间 / 列名 / 行号。
- 勾选「不打码」才会取原文（预览与下载都取原文）；启动时加 `--disable-plain` 可整体禁掉这个出口。
- 数字字段（住院号 / 病案号 / 电话）在 Excel 里可能显示成科学计数法 —— 那是 Excel 打开 csv 的行为，csv 本身是文本。要给业务方直接看就用 `wide.xlsx`。

## 6. 报警是什么意思、该怎么办

| 报警 | 含义 | 你该做什么 |
|---|---|---|
| `multi_patient`（散页守卫） | 一个散页目录里读到 **2 个及以上不同的住院号或姓名** ⇒ 禁止合并。合成一份等于凭空造一份不存在的病历 | 打开暂存目录里的 `_merge_alerts.json` 找到那个目录，人工拆成一人一个文件夹，再重跑。**不要**想办法绕过 |
| `multi_patient_suspect` / `guard_unreadable` | 守卫抽不出住院号/姓名（页太糊），无法证明只有一个病人 | 同样按人工确认处理，别硬跑 |
| G1 源根形状 | PDF 不在 `<院区>/<病例>/` 这层 | 按 §4 把目录摆正 |
| G2 空间不足 | 需要「输入字节 × 3」的空闲空间 | 清盘，或让维护者换 `paths.run_root` |
| G3 服务不活 | OCR(8110) 或抽取模型(8080) 没起 | 见 §7；**不要自己重启服务** |
| G5 压缩包三检 | zip 损坏 / 条目过多 / 解压后过大 | 重新打包，或只传需要的那部分 |
| G6 `--steps` 缺上游 | 只跑某阶段但上游哨兵缺失，会每例白等 900 s | 别只给 `--steps`，或补 `--manifest-from <上一批>` |
| 「批次目录不存在」 | 批次名打错，或那一批在**另一个产物根**（网页 `/raid5/medrec-run/wide` vs 命令行 `/home/ubuntu/medrec/run/wide`） | 网页会列出旧根；用 `bin/medrec stages <名字>` 确认名字 |
| 两批逐格 sha 有差异 | OCR 不是完全确定：现网 `--max-num-seqs 64` 下 **1.0–2.0%** 属正常噪声带 | 超过这个带才找维护者 |

## 7. 跑批前的 30 秒自检

```bash
cd /home/ubuntu/medrec
bash scripts/10-start-ocr.sh check    # OCR 是不是真的在 GPU 上干活（rc≠0 就别跑批）
~/strata-ctl.sh status                # 抽取模型（8080）在不在
bin/medrec doctor                     # 产线自检：列数 / 服务 / 产物根可写
```

端口：**8110** = OCR(vLLM)、**8080** = 抽取模型(Strata)、**8090** = 本网页。
判活用 `curl -s http://127.0.0.1:8110/v1/models`，不要用 `pgrep`（会误报）。
`/health` 返回 200 **不代表**服务在 GPU 上干活 —— 必须跑上面那条 check。

## 8. 六条别踩

1. **别 kill / pkill / 重启任何服务。** OCR 起一次要几分钟，抽取模型冷启动约 9 分钟，起坏了没有第二个能救回来。
2. **别删 `run/` 下的旧批次。** 上一轮产物是对账证据。想干净重跑就换个批次名。
3. **别改 `config/` 和 `scripts/config.env`**，除非有人明确说改哪个键。
4. **别把 `wide.csv` / `01_ocr/` 发到外网、聊天工具、网盘。** 全是患者身份信息。
5. **别为了跑得快去关守卫、减页、关 crop-blocks。** 少抽字段和跑得快在数字上长得一模一样。
6. **字段是空就是空（null）。** 别用「未提及 / 无 / 否」去填，也别让机器猜。

## 9. 参数在哪（改之前先问维护者）

| 想调什么 | 文件 | 键（现值） |
|---|---|---|
| 例级并行 | `scripts/config.env` | `R15_CASE_PARALLEL=24` |
| 每例 OCR 请求并发 | `scripts/config.env` | `R23_OCR_CONCURRENCY=32` |
| OCR 单次响应上限 | `scripts/config.env` | `R23_OCR_MAX_TOKENS=3072` |
| 首页锚点升级扫描窗口 / 上限 | `scripts/config.env` | `R22_ESC_WINDOW=13` / `R22_ESC_MAX_CALLS=24` |
| 服务端口 | `scripts/config.env` | `PORT_OCR_VL=8110` / `PORT_QWEN=8080` |
| 产物根（命令行跑批） | `config/pipeline.yaml` | `paths.run_root`（现 `/home/ubuntu/medrec/run/wide`） |
| 产物根（网页跑批） | 环境变量 | `MEDREC_WEBUI_RUN_ROOT`（现 `/raid5/medrec-run/wide`） |
| OCR 后端与地址 | `config/pipeline.yaml` | `ocr.backend=vllm` / `ocr.vllm_url` |
| 列定义 | `config/columns.yaml` | `version: 2026-10-15.1`（23 列） |

改完 `config.env` 或 `pipeline.yaml` **必须重启网页和跑批进程**才生效 —— 老进程读的还是旧值。

## 10. 换一台机器怎么装

**最省事：一行命令**（slim 框架包 1.5 MB，模型权重安装时在线拉）：

```bash
export MEDREC_PAT=<你的 GitHub PAT>        # 仓库设成公开后这一行可以去掉
curl -fsSL -H "Authorization: Bearer $MEDREC_PAT" -H "Accept: application/octet-stream" \
  https://api.github.com/repos/Iris11-15/MedrecScan-Understand/releases/assets/661145943 \
  | bash -s -- --dry-run                  # 先只看计划；确认后去掉 --dry-run 再跑一次
```

它做四件事：下载 slim 包 → `sha256sum -c` 校验 → 解压 → `STRATA_SOURCE=modelscope bash install.sh --weights-online`。
包里**没有模型权重**：OCR 权重 ≈5 GB 和 Strata 都在安装时从 modelscope 现下
（实测 39.9–48.8 MB/s；HuggingFace 国内实测 ~0.01 MB/s，所以默认走 modelscope）。

- GitHub 太慢或不通：先把包 scp 到目标机，再 `MEDREC_LOCAL_PKG=/path/medrec-deploy-1.0.1-r54-slim-2xl40.tar.gz bash bootstrap.sh`（校验与安装照跑）。
- 要离线整包（权重在包里，3.64 GB 两个分卷）：`MEDREC_FAT=1 bash bootstrap.sh`，脚本会自动取两卷、逐个校验、`cat` 合并。
- 目标机要求：恰好 2 张 L40/L40S、Ubuntu 20.04+、conda、空闲 ≥90 GB（只要 OCR 和宽表就加 `--skip-strata`，约 15 GB）。

### 手动装（离线整包 / 想自己验包时）

看 `deploy/QUICKSTART.txt`（三步：`sha256sum -c` 验包 → `bash install.sh --dry-run` 应 preflight 7/7 → `bash install.sh`）。
要点：装抽取模型要 **≥105 GB** 空闲盘；只要 OCR 和宽表就加 `--skip-strata`（15 GB）；
内存不够 62 GB 时装 `STRATA_MODEL=auto bash install.sh` 让它按内存挑档；装完 `bash health.sh` 自检。

### 包在哪、怎么拿

包在本仓库的 **Releases**（tag `v1.0.1-r54`）。GitHub 单个 Release 资产上限实测 **2 GiB**
（API 报 `size must be less than 2147483648`），3.64 GB 的整包传不上去 ⇒ 以**两个分卷**发布：

| 资产 | 是什么 |
|---|---|
| `medrec-deploy-1.0.1-r54-slim-2xl40.tar.gz` | **推荐**：框架 1.5 MB（1,534,649 B / `2cedc7ed…92aec`），权重安装时在线拉 |
| `…slim-2xl40.tar.gz.sha256` | 上面那个包的校验行 |
| `bootstrap.sh` | 一行安装的入口脚本（asset_id 661145943） |
| `…2xl40.tar.gz.part00` + `.part01` | 离线整包 3.64 GB（权重在包里），下载后 `cat` 合并 |
| `SHA256SUMS.parts` / `…2xl40.tar.gz.sha256` | 分卷校验行 / 合并后整包校验行 |

```bash
cat medrec-deploy-1.0.1-r54-2xl40.tar.gz.part00 \
    medrec-deploy-1.0.1-r54-2xl40.tar.gz.part01 \
  > medrec-deploy-1.0.1-r54-2xl40.tar.gz
sha256sum -c SHA256SUMS.parts                              # 分卷本身没坏
sha256sum -c medrec-deploy-1.0.1-r54-2xl40.tar.gz.sha256   # 整包 = bd254b3b…3392
```

合并后的 sha256 与 `deploy/QUICKSTART.txt` 里那个是同一个数 —— 分卷只是 GitHub 传输限制下的搬运方式，
不是包结构变化。内网直接拷整包就不用合并这一步。

## 11. 已知限制（不是 bug）

- `bin/medrec run` 的 stdout 会带含姓名的 case 目录名（网页显示时打码，原始日志未改）。
- 网页下载默认打码；要原文必须显式勾「不打码」，且会被审计记录。
- 标准化就诊医院为空时，本批产物**不能**直接拿去对登记表（判重键缺分量 ⇒ 不参与合并）。
- 院区名从首页 OCR 里读，读不到写 null —— 不会从拼音目录名猜中文院名。

## 12. 还想查什么

| 想看 | 去读 |
|---|---|
| 网页每个接口、停法、端口绑定 | `webui/README.md` |
| 单例 / 批量命令、参数含义、排错 | `docs/HOWTO.md` |
| 端口 / 模型 / 启动命令 / 请求形态 | `docs/interfaces.md` |
| 安全边界、隔离跑法、审计 | `docs/security.md` |
| 新机器部署全过程 | `deploy/QUICKSTART.txt`、`deploy/DELIVERY.md` |
| 当前状态与待办 | `reports/` 下最新一份 |
