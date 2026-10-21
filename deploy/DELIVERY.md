# DELIVERY —— medrec round52 部署包（迁移到 2×L40）

> 生成机：源服务器 4×RTX 4090（Ubuntu 22.04.5，驱动 580.178.04）
> 包目录：`/home/ubuntu/medrec-deploy-round52/`
> 依据：`medrec-round52-deploy-tune-brief.md`（下称 brief）+ `medrec-round52-codex-task.md`
> ★ 本轮**没有**改源机器的任何配置、没有重启任何服务、没有删任何文件（副作用披露见 §6-5）

## §1 交付物：一个 bundle，里面两个包

**要发出去的就是一个文件**：`medrec-deploy-1.0.0-r52-2xl40.tar.gz` —— 解压出同名目录
`medrec-deploy-1.0.0-r52-2xl40/`，进去 `bash install.sh` 就把环境装好（conda 环境 + 权重 + Strata + 起服务 + 冒烟）。

发布名 = `medrec-deploy-<版本>-<构建>-<目标机>`。改名不用改脚本：
`RELEASE_VERSION=1.1.0 RELEASE_BUILD=r53 RELEASE_TARGET=4xl40 bash make_bundle.sh`。
★ 历史日志与 §2 的验收记录里出现的 `medrec-round52-bundle.tar.gz` 是**同一个东西的旧名**（改名前打的包），
  证据不改写，看到旧名别当成另一个包。

★★ **本文档在包内** ⇒ 改它一个字节，bundle 的 size/sha256 就变。所以
**权威 sha256 写在包外**：`medrec-deploy-1.0.0-r52-2xl40.tar.gz.sha256`（与 bundle 同目录，发过去时一起发）。
下表与下文里的 sha 是「最后一次打包」那一刻的快照，核对时以 sidecar 为准。

| 包 | 压缩后 | sha256（前 12） | 条目 / 成员 | 解压后 |
|---|---|---|---|---|
| **`medrec-deploy-1.0.0-r52-2xl40.tar.gz`** | 见 sidecar | 见 sidecar | **19 条目 / 17 文件 / 16 必需成员** | **3.4 GB** |
| `medrec-code-round52.tar.gz` | 792,077 B（0.76 MB） | `8c17a7f0fdae` | 149 / 138 | 2,477,507 B |
| `medrec-assets-round52.tar.gz` | 3,637,990,435 B（3.39 GB） | `5f3ed519fad1` | 178 / 134 | 4,529,681,952 B（4.22 GB） |

完整 sha256：

- code `8c17a7f0fdaeb461ebdc94d2d6c8682e134ea490362d523fe175ed7a4254f9bd`
- assets `5f3ed519fad1144bfc2512a5287effc21e1812b45ff16178dafbcc3249f2e204`
- bundle：改名后重打的那一份 ⇒ **只认包外 sidecar**。旧名时期的快照依次为
  `123ce87f48fc…` / 3,638,101,693 B、`75a4fb744f68…` / 3,638,101,582 B、
  `90ba92b62af9…` / 3,638,100,140 B、`8e14efe5d8ca…` / 3,638,093,248 B、
  `045fdd711222…` / 3,638,092,937 B（第一版，被三处 `install.sh` 修复作废，见 §2.5）。
  bundle 的 sha **每重打包一次就变一次**（本文档在包内），所以**别拿这一行核对**。
   历史快照：`90ba92b62af9…` / 3,638,100,140 B（第三版）、`8e14efe5d8ca…` / 3,638,093,248 B（第二版）、
   `045fdd711222…` / 3,638,092,937 B（第一版，被三处 `install.sh` 修复作废，见 §2.5）。
   四版之间**只有 `DELIVERY.md`/`QUICKSTART.txt` 变**；`install.sh` 与两个 tar.gz 逐字节未变。

bundle 内容（`work/bundle_listing.txt` 18 条目）：`install.sh` `tune.sh` `start-all.sh` `stop-all.sh`
`health.sh` `pkg_tool.py` `DELIVERY.md` `QUICKSTART.txt` + 两个 `tar.gz` 与两份 `manifest.json`
+ `env/install-{vl,ocr,qwen}-env.txt` 与 `env/README.md`。

```
$ bash make_bundle.sh            # 第四版，work/make_bundle4.log（自查约 60 s，tar -tzf 要过 3.6 GB）
[..] 打包 /home/ubuntu/medrec-deploy-round52/work/bundle-stage/medrec-round52 → …/medrec-round52-bundle.tar.gz

===== 打包后自查 =====
条目数：18
禁运内容命中：0（要求 0）
[OK] 15 个必需成员都在
大小：3638101582 字节
75a4fb744f6839f2220314b7cc8c29afaba275d07413cd1020878bdb438d5fc4  /home/ubuntu/medrec-deploy-round52/medrec-round52-bundle.tar.gz
sidecar：/home/ubuntu/medrec-deploy-round52/medrec-round52-bundle.tar.gz.sha256
自查落盘：/home/ubuntu/medrec-deploy-round52/work/bundle_selfcheck.txt

$ cat work/bundle_selfcheck.txt
archive=/home/ubuntu/medrec-deploy-round52/medrec-round52-bundle.tar.gz
bytes=3638101582
entries=18
files=16
banned_hits=0
required_members=15
sha256=75a4fb744f6839f2220314b7cc8c29afaba275d07413cd1020878bdb438d5fc4
```

清单文件：`medrec-code-round52.manifest.json`、`medrec-assets-round52.manifest.json`
（顶层 `archive_sha256 / archive_bytes / file_count / entry_count / total_bytes` + `files[]`：`archive / source / kind / size / mode / sha256`）。

### 1.1 内容摘要（`work/summarize.py` 按包内前缀汇总）

```
medrec-code/src      entries=79  files=75  bytes=1568155
medrec-code/scripts  entries=33  files=32  bytes=414584
medrec-code/webui    entries=17  files=14  bytes=195100
medrec-code/bin      entries=6   files=5   bytes=133690
medrec-code/docs     entries=8   files=7   bytes=92938
medrec-code/config   entries=6   files=5   bytes=73040

medrec-assets/paddlex  entries=91  files=75  bytes=2449025941   # ~/.paddlex/official_models（PaddleX 侧权重）
medrec-assets/models   entries=41  files=31  bytes=2079676837   # PaddleOCR-VL-1.6 / PP-DocLayoutV3 / PP-OCRv5_mobile_rec
medrec-assets/smoke    entries=7   files=3   bytes=838029       # 冒烟 2 例（合成假病历）+ probe_page.png
medrec-assets/modules  entries=34  files=21  bytes=123914       # models/modules（PaddleX 缓存的模块源码）
medrec-assets/wheels   entries=5   files=4   bytes=17231        # 三个 conda 环境的 freeze 清单
```

★ **包里不含 Strata 引擎与 GGUF 权重**（落盘 86 GB，见下面实测）—— 人的决定：安装时从 GitHub 抓。
  这一条把 assets 从原方案的 ~90.7 GB 压到 3.39 GB。代价见 §5-3 / §6-1。

  Strata 在目标机上的**落盘体积实测**（不是估计）：

  ```
  $ du -sh /raid5/strata-data      → 86G
  $ du -sh /raid5/strata-data/*   → 78G models / 6.5G mtp / 1.5G packs
  $ du -sh ~/Strata               → 657M   # 引擎 + 源码（git 仓库）
  $ grep -n '"IQ3_S"' -A2 ~/Strata/setup.py → "download_gb": 83.6, "ram_gb": 62
  ```

  ⇒ `install.sh` 的 preflight ④ 现在按 **90 GB** 给 Strata 预留（`STRATA_DISK_GB=` 可覆盖，
  `--skip-strata` 时不计）。改前它只要求「包+解压 ×2 = 15 GB」，会在第 3 步下载中途爆盘。

### 1.2 禁运内容自查（brief §4.1 红线）

```
$ tar -tzf medrec-code-round52.tar.gz   | grep -nE "(^|/)(run|workspace|logs|__pycache__|\.git)(/|$)|\.bak|~$"   → 空
$ tar -tzf medrec-assets-round52.tar.gz | grep -nE "(^|/)(run|workspace|logs|__pycache__|\.git)(/|$)|\.bak|~$"   → 空
```

`install.sh` 在解包**之前**会重跑这条自查（`selfcheck_pkg`），命中就 `[FAIL]` 并拒绝安装。
实测两条都在 `work/install_fake1.log` / `work/install_dry.log` 里打 `[OK] 解包前自查通过`。

### 1.3 包内路径 → 目标机落点（只在 `pkg_tool.py` 的 `GROUPS` 写一份）

| 包内前缀 | 落点 |
|---|---|
| `medrec-code/` | `$MEDREC_ROOT/` |
| `medrec-assets/models/` | `$MEDREC_ROOT/models/models/` |
| `medrec-assets/modules/` | `$MEDREC_ROOT/models/` |
| `medrec-assets/paddlex/` | `$HOME/.paddlex/` |
| `medrec-assets/smoke/` | `$MEDREC_ROOT/smoke/` |
| `medrec-assets/wheels/` | `$MEDREC_ROOT/wheels/` |

包内**没有** symlink 条目，`models/PaddleOCR-VL-1.6 → models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master`
这条软链由 `install.sh` 的 `link_models` 建（brief §3.2 要求路径必须到 `snapshots/master`）。

### 1.4 `env/` 三份 pip 清单（本轮新增 —— 「一键装环境」读这里）

`install.sh` 第 3 步默认代跑 `conda create` + `pip install -r env/install-<env>.txt`。
这三份由 `python pkg_tool.py envlists <wheels/> env/` 从 assets 里的 `wheels/freeze-<env>.txt`
（源机 `pip freeze` 原样，逐文件 sha256 已验）生成，**只洗 8 条**，理由写在 `env/README.md`：

```
$ python pkg_tool.py envlists work/fakeroot4/medrec/wheels env
  DROP qwen-env: conda 自带: setuptools==80.10.2
  DROP qwen-env: conda 自带: wheel==0.48.0
WROTE env/install-vl-env.txt kept=415 dropped=4
  DROP vl-env: VCS/editable: aslprep @ git+https://github.com/pennlinc/aslprep.git@d484c8ef…
  DROP vl-env: VCS/editable: neuromaps @ git+https://github.com/netneurolab/neuromaps@ae3c88a6…
  DROP vl-env: conda 自带: setuptools==83.0.0
  DROP vl-env: conda 自带: wheel==0.47.0
TOTAL_DROPPED 8
```

- 洗掉的 2 条 `@ git+` 是上一个项目（神经影像）留在同一个 env 里的：
  `grep -rniE 'aslprep|neuromaps' src/ scripts/ webui/ bin/` 在源机 medrec 仓库 **0 命中**。
  留着 = 目标机多两次 GitHub 克隆，任一失败整条 `pip install` 非零退出 ⇒ 环境装成半成品，
  而报错文字指向 pip，不指向「这两个包本来不该装」。
- **没有洗掉** `paddlepaddle-gpu @ https://paddle-whl.cdn.bcebos.com/stable/cu129/…whl#sha256=`
  （vl-env 第 268 行）：只有这个 URL 能装对 CUDA 变体（源机 vl-env = cuda 12.9 / ocr-env = cuda 12.6）。
  ⚠ 目标机驱动带不动 cu129 wheel 时要换 URL —— 见 §5-4。

### 1.5 权重两条路线（assets 不通就走网络，本轮新增）

| 路线 | 开关 | 权重来源 | 磁盘 |
|---|---|---|---|
| A（默认） | 裸 `bash install.sh` | 包内 `medrec-assets-round52.tar.gz` 的 `models/` + `paddlex/` 两组 | 解压 4.22 GB |
| B | `bash install.sh --weights-online` | 第 3 步用 `modelscope` CLI 现下 **7 个落点 / 6 个 id**（`PP-DocLayoutV3` 两处都要） | 约 5 GB（`WEIGHTS_GB=` 可改） |

B 路线的 6 个 modelscope id 实测可达（`work/modelscope_probe2.txt`）：

```
$ for id in PaddlePaddle/PaddleOCR-VL-1.6 PaddlePaddle/PP-DocLayoutV3 PaddlePaddle/PP-OCRv5_mobile_rec \
            PaddlePaddle/PP-OCRv5_mobile_det PaddlePaddle/PP-LCNet_x1_0_doc_ori PaddlePaddle/PP-LCNet_x1_0_textline_ori; do
    curl -sS -m 20 -o /dev/null -w "%{http_code}\n" "https://www.modelscope.cn/api/v1/models/$id"; done
200	PaddlePaddle/PaddleOCR-VL-1.6
200	PaddlePaddle/PP-DocLayoutV3
200	PaddlePaddle/PP-OCRv5_mobile_rec
200	PaddlePaddle/PP-OCRv5_mobile_det
200	PaddlePaddle/PP-LCNet_x1_0_doc_ori
200	PaddlePaddle/PP-LCNet_x1_0_textline_ori
```

★ B 路线在源机**只跑过 `--dry-run`**（红线：不在源机下 5 GB 权重）。真下未在目标机验过 —— 见 §5-9。

### 1.6 Strata 档位（量化大小）—— 默认固定，不自动降档

`install.sh` 默认 `STRATA_MODEL=IQ3_S`（与源机一致）。档位表抄自上游 `setup.py:127-134` 的 `MODELS`：

| 档位 | 上游说明 | `ram_gb`（最低主机内存） | `download_gb`（权重量） | 家族 |
|---|---|---|---|---|
| `IQ3_S` | 3.5-bit，质量最好（与全量模型持平），最慢 | **62** | 83.6 | qwen |
| `IQ3_XXS` | 3-bit，质量次之，CPU 活更多 | 60 | 75.8 | qwen |
| `IQ2_XS` | 2-bit i-quant，略好于 Q2_0，速度接近 | 48 | 68.0 | qwen |
| `Q2_0` | 2-bit，最快 | 48 | 66.4 | qwen |

行为（`strata_resolve_model()`，`install.sh`）：

- **默认不自动选**。内存不够 ⇒ 直接 `[FAIL]` 停下，**不下那 83.6 GB**，并给出换档命令。
  理由：降档 = 改抽取质量，属于必须让人知道的变更（红线⑦）；静默降档就是静默错。
- `STRATA_MODEL=auto` ⇒ 按 `MemTotal` 从大到小挑第一个装得下的，**明打一行**「自动定档：X（要 N GB 内存 / M GB 下载；本机 K GB）」，
  装不下的每一档也各打一行「跳过」。
- 配置文件名跟着档位走：`$STRATA_HOME/strata-<档位小写>.json`（上游就是 `strata-<model>.json`）。
  写死 `strata-iq3_s.json` 会在换档后把「已装过」判断指错文件。
- `STRATA_DISK_GB`（磁盘预检预留，默认 90）仍是**按 IQ3_S 写死的保守值**；换小档不会少预留，只会多留。

实测矩阵（`bash work/t55_strata_size.sh` → `work/t55_strata_size.out`；被测函数是从 `install.sh` 里
`eval` 出来的同一份代码，不是复制第二份）：

| 内存 | `STRATA_MODEL=IQ3_S` | `STRATA_MODEL=auto` |
|---|---|---|
| 32 GB | `[FAIL]` rc=1，不下权重 | `[FAIL]` rc=1（四档都要 ≥48） |
| 50 GB | `[FAIL]` rc=1 | 定档 `IQ2_XS`，cfg `strata-iq2_xs.json` |
| 61 GB | `[FAIL]` rc=1 | 定档 `IQ3_XXS` |
| 64 GB | `[OK]` IQ3_S | 定档 `IQ3_S` |
| 251 GB（源机） | `[OK]` IQ3_S | 定档 `IQ3_S` |

★ 未验证：`ram_gb` 是上游写的**最低**值，不是「实测能跑」；62 GB 的机器上跑 IQ3_S 会不会被
  OCR 与产线挤爆内存，本轮没测（§5-3）。`--context 131072 --kv int8` 也会加内存占用。

## §2 `install.sh` 怎么用（六条实际执行记录 A–F）

### 2.0 子命令 / 开关 / 可覆盖变量（`bash install.sh --help` 原文，`work/install_help3.log`）

```bash
bash install.sh preflight     # 只跑 7 项环境检查，零写入
bash install.sh               # = install：preflight → unpack+verify → env setup → start+smoke（顺序固定）
bash install.sh online        # 强制按外网可达处理（Strata 安装时从 GitHub 抓）
bash install.sh offline       # 强制离线：模型走包内 assets；Strata 会 [FAIL] 并给三条补救
bash install.sh --rollback    # 停服务 + 删安装期自建目录（按 ledger 记账）+ 恢复配置备份
bash install.sh --dry-run     # 零副作用跑通全流程，只打印将执行的命令
# 开关：--no-env --with-env --weights-online --skip-strata --no-start --force-assets --no-cron
#       ★ 默认【代跑】conda create + pip install（一键装环境）；--no-env 退回只打印命令
#       ★ --weights-online：不 unpack assets 的 models/ 与 paddlex/，第 3 步用 modelscope 现下（约 5 GB）
# 可覆盖：MEDREC_ROOT STRATA_* OCR_VLLM_* OCR_MAX_* STRATA_GPU STRATA_DISK_GB CODE_PKG ASSET_PKG CODE_MAN ASSET_MAN
#         MEDREC_PY SMOKE_BATCH SMOKE_RUN_ROOT STATE LEDGER MARK
#         INSTALL_ENVS WEIGHTS_ONLINE WEIGHTS_GB MODELSCOPE_CACHE_DIR CONDA_PY PIP_INDEX_URL
```

★ 目标机上**不需要**任何环境变量，裸 `bash install.sh` 即可。下面 B/C 两条记录里的
`HOME` / `MEDREC_ROOT` / `PORT_*` / `LEDGER` 覆盖只属于**假根演练**（见 2.4）。

### 2.1 记录 A —— `--dry-run`（源机 4×4090，`work/install_dry2.log`）

```
$ bash install.sh --dry-run          # rc=0
[FAIL] ③ 卡：检测到 4 张，其中含 L40 字样 0 张 —— 本包按 2×L40 写死
[FAIL] ⑤ 端口 8080 已被占用 …:8080 …("python",pid=3564696)
[FAIL] ⑤ 端口 8110 已被占用 …:8110 …("vllm",pid=1023536)
[FAIL] ⑤ 端口 8090 已被占用 …:8090 …("python",pid=1573332)
preflight: 5/7 项通过
[..] （--dry-run：preflight 未过也继续把后面的计划打印完 —— 本模式零副作用）
[DRY] tar -xzf … -C /home/ubuntu/medrec/models/models …
[DRY] pkg_tool.py csvshape /home/ubuntu/medrec/run/r52smoke/06_table/wide.csv → 期望 data_rows=2 cols=23
```

源机上 ③⑤ 必然 `[FAIL]`（4 张 4090 + 三个服务都在跑）—— 这正是 preflight 该有的表现。
零副作用证明（同一条命令前后对比）：

```
$ md5sum /home/ubuntu/medrec/config/* /home/ubuntu/medrec/scripts/config.env   # 前后 → MD5_SAME
$ find ~/medrec-deploy-round52 -maxdepth 1 | sort                             # 前后 → LS_SAME
$ grep -c '^\[DRY\]' work/install_dry2.log → 26
```

### 2.2 记录 B —— 完整安装 + 幂等重跑（假根，`work/install_fake9_install.log` / `install_fake10_idempotent.log`）

```
preflight: 7/7 项通过            # 假 nvidia-smi 报「恰好 2 张 L40」，见 2.4
[OK] code 包 sha256：OK sha256=8c17a7f0… bytes=792077 files=138
[OK] assets 包 sha256：OK sha256=5f3ed519… bytes=3637990435 files=134
[OK] 解包前自查通过：medrec-assets-round52.tar.gz 无 run/ workspace/ logs/ __pycache__ .bak
[OK] 解出 31 个文件：medrec-assets/models/     （modules 21 / paddlex 75 / smoke 3 / wheels 4）
[OK] verify assets：verified=134 sha_diff=0 patched=0 missing=0 total_files=134
[OK] verify code：verified=138 sha_diff=0 patched=0 missing=0 total_files=138
[OK] 软链 …/models/PaddleOCR-VL-1.6 → models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master
[OK] 权限已设：文件 600 / 目录 700，可执行位保留；未碰 run/ workspace/ logs/
[OK] config.env 已落实陷阱 1/2/4（卡0=OCR、gmu 0.22、mns 64、mbt 16384）
[WARN] 陷阱3 未落实：…/Strata/strata-iq3_s.json 不存在（本次 --skip-strata）
```

记账：`61 CREATED / 2 PATCHED / 2 BACKUP`（`wc -l` 见 `work/fakeroot6/ledger`）。
第二趟重跑（幂等，brief §4.6）：

```
$ bash install.sh --skip-strata --no-cron --no-start     # rc=0
[..] [SKIP] .installed 里两个包的 sha256 与当前包一致 —— 整组跳过   × 6（5 组 assets + 1 组 code）
[OK] verify assets：verified=134 sha_diff=0 patched=0 missing=0
[OK] verify code：verified=137 sha_diff=0 patched=1 missing=0 total_files=138   # patched=1 = 安装期改过的 config.env
```

冒烟（第 4 步，真跑 2 例，在源机真根上跑的，`work/smoke_run.log:367`）：

```
TABLE …/r52smoke/06_table/wide.csv rows=2 cols=23 nonempty_cells=34/46 主诉=2/2 查体=2/2
```

### 2.3 记录 C —— `--rollback`（`work/install_fake11_rollback.log` / `install_fake12_rollback2.log`）

```
$ bash install.sh --rollback      # rc=0
[SKIP] webui 没在跑（端口 18090 无监听）   （vLLM / Strata 同）
[..] [RM] …/fakeroot6/home/medrec/scripts/rotate-logs.sh
[..] [RESTORE] …/fakeroot6/home/medrec/scripts/config.env ← backup/20261020-125935/scripts/config.env
[..] [RM] …/fakeroot6/home/medrec/src … bin … config … webui … docs … models … wheels … smoke …
[..] [RM] …/fakeroot6/home/.paddlex
rollback 完成：撤销 62 项，跳过 1 项（其中 1 项属于其它 MEDREC_ROOT 的历史记账，不碰）
```

目录条目 **341 → 8**（`find fakeroot6 -printf '%y %M %p\n' | sort`，前后快照
`work/fakeroot6.tree_after_install.txt` / `.tree_after_rollback.txt`）。剩下 8 项逐条：

| 剩余 | 为什么留 |
|---|---|
| `fakeroot6/`、`fakeroot6/home/` | 假根本身；`$HOME` 按设计永不删（`in_whitelist` 显式排除） |
| `fakeroot6/home/medrec/` | `MEDREC_ROOT` 本身，同上（目标机上它就是用户目录，删了就是删用户物） |
| `fakeroot6/ledger`、`state` | 演练用的记账/断点文件，不是安装产物 |
| `home/.conda/aau_token`、`home/.nv/ComputeCache` | conda / CUDA 在 `$HOME` 下自己落的，不是 `install.sh` 建的 ⇒ 不记账也不删 |

第二次 `--rollback` 零动作（幂等）：`rollback 完成：撤销 0 项`。

★ 本轮修的三处（改前备份 `install.sh.bak-round52-r10` / `install.sh.bak-round52-r11`）：
1. **记账前移到解包之前**：`tar` 会顺手建出父目录，原来解完再 `ensure_dir` 看见已存在就不记 ⇒
   回滚漏删（实测旧版回滚后 `src/ bin/ config/` 原地不动）。现在先按 manifest 的目录成员
   `pkg_tool.py mkdirs` 逐个 `ensure_dir` 记账，再 `tar`。
2. **rollback 补两处**：① `PATCHED` / `ROLLED_BACK` 不再报「未知记账类型」（旧版打 `[WARN] [SKIP]`）；
   ② `BACKUP` 的**恢复目标**也要过白名单，且记账按 `MEDREC_ROOT` 分域 + 写 `ROLLED_BACK` 标记
   （旧版会把别的根的历史记账拿来撤，一次演练打出 18 条 `[KEEP]` 噪音，还会重复恢复三次备份）。
   同时 `STATE/LEDGER/MARK` 改为可被环境覆盖，多根演练各用各的记账。
3. **preflight ④ 磁盘检查补上 Strata 那 90 GB**（静默错：Strata 不再打包 ⇒ 磁盘是**安装时**才消耗的，
   原检查只按包体积算，会放行、然后在第 3 步下载中途爆盘）。依据：`du -sh /raid5/strata-data` = **86G**
   （models 78 + mtp 6.5 + packs 1.5）、上游 `setup.py` 的 IQ3_S `download_gb=83.6 / ram_gb=62` ⇒
   默认预留 **90 GB**；`STRATA_DISK_GB=` 可覆盖，`--skip-strata` 不计这块。
   过程中踩到两个坑，都已修并留注释（`install.sh:161-168`）：
   ① **单位**：`need` 是**字节**、`df` 给的是 KB，混算实测打出「需要 ≥ 15674 GB」（`work/install_preflight3.log:9`）；
   ② **`${extra:+…}`** 在 `extra="0"` 时是**非空字符串 "0"** ⇒ 条件成立，会打出「+ Strata 0 GB」，改用 `strata_note` 变量。
   回归（两臂 + 假根）：`work/install_preflight5_src.log:9`
   `[OK] ④ 可用 136 GB ≥ 需要 105 GB（包+解压 ×2 = 15 GB + Strata 90 GB）`；
   `work/install_preflight6_src_nostrata.log:9` `--skip-strata` → `需要 15 GB`；
   `work/install_preflight4_l40fake.log`（假 2×L40）**7/7 项通过 rc=0**。

### 2.4 假根演练的做法（为什么不会伤到源机）

```bash
FR=~/medrec-deploy-round52/work/fakeroot6; mkdir -p "$FR/home"
env HOME="$FR/home" MEDREC_ROOT="$FR/home/medrec" \
    LEDGER="$FR/ledger" STATE="$FR/state" MARK="$FR/.installed" \
    PORT_STRATA=18080 PORT_OCR_VL=18110 PORT_WEBUI=18090 \
    PATH=~/medrec-deploy-round52/work/shim:$PATH \
  bash install.sh --skip-strata --no-cron --no-start
```

- `work/shim/nvidia-smi`：只回答 preflight / stop-all 用到的三条查询，返回「2 张 L40 / 49140 MiB / 驱动 550.90.07」。
  目标机上不需要它（真 `nvidia-smi` 就在 PATH 里）。
- `HOME` 指到假根 ⇒ 模型、`.paddlex`、`Strata` 全落在假根里，**源机的 `~/medrec`、`~/.paddlex`、`~/Strata` 一个字节都没动**。
- `PORT_*` 换 18xxx ⇒ 不与源机在跑的 8080/8110/8090 冲突；`--no-start` 不起服务（红线：不重启源机服务）。
- ⚠ 演练遗留：`work/fakeroot5/`（4.3 GB）是一次**被中途打断**的安装，脚本在运行中被编辑导致尾部
  `syntax error near unexpected token ')'`（bash 边读边执行）。该次结果**不作证据**，结论全部来自 fakeroot6。
  要不要清掉等人一句话（红线：不自行删产物）。

### 2.5 记录 D / E / F（本轮新增）+ 本轮三处 `install.sh` 修复

**记录 D —— 网络权重路线（`work/install_dry4_wonline.log`，`--dry-run --weights-online`）**

```
rc=0；:30/:33 两组 [SKIP] --weights-online：整组不解 medrec-assets/models/ 与 paddlex/（第 3 步用 modelscope 现下到同一落点）
:41 [..] 软链留到第 3 步权重下完之后再建（--weights-online）
:57 [OK] modelscope CLI：/home/ubuntu/anaconda3/envs/vl-env/bin/modelscope
:58-66 七条 [SKIP] 已就位（源机已有模型 ⇒ 只验落点与 SKIP 分支，没真下）
```

**记录 E —— 假根真装 + 幂等（`work/install_fake13_r7.log` / `install_fake14_idem.log`）**

```
真装：preflight 7/7；verify assets verified=134 sha_diff=0 missing=0；
      verify code  verified=138 sha_diff=0 missing=0；软链已建；权限已设 10 个目录
      该根专属 ledger：61 CREATED / 2 PATCHED / 2 BACKUP / 1 CRON（work/fakeroot7/ledger）
幂等重跑：6 组 [SKIP] .installed 里两个包的 sha256 与当前包一致；
          verify code verified=137 sha_diff=0 patched=1（config.env 是安装期改的，原件在 backup/）
```

★ 记账按根分开（`LEDGER=`/`STATE=`/`MARK=` 三个覆盖）：`grep -c fakeroot7 install.ledger` → `0`，
  说明这次真装没有污染共享 ledger，回滚也只对本次根生效。

**记录 F —— 从 bundle 解包后装（本轮主验收：「解压即装」）**

```
$ tar -xzf medrec-round52-bundle.tar.gz -C work/bundle-test2      → EXTRACT_OK（3.4 GB）
$ bash work/bundle-test2/medrec-round52/install.sh --dry-run --no-env --skip-strata
  rc=0 / preflight: 7/7 项通过 / 138 条 [DRY] / 0 条 [FAIL]      （work/install_dry8_bundle.log）
$ bash work/bundle-test2/medrec-round52/install.sh --no-env --skip-strata --no-start --no-cron
  work/install_fake15_bundle.log（真装）
```

| # | 修复 | 改前（原始输出） | 改后（原始输出） | 备份 |
|---|---|---|---|---|
| 1 | 演练根不写真 crontab | `crontab -l` 出现 `*/30 * * * * … fakeroot7 … rotate-logs.sh` | `[..] 演练根（HOME=… ≠ 账号真实 home /home/ubuntu）：不写 crontab`（`install_fake14_idem.log` 末段） | `install.sh.bak-round52-r13` |
| 2 | ④ 磁盘 `df` 逐级向上找已存在祖先 | `install_dry5_bundle.log:8 [FAIL] ④ 磁盘：df 查不到 …/home/medrec 所在分区` → `preflight: 6/7`、rc=1 | `install_dry6_df.log:8 [OK] ④ 磁盘：可用 122 GB ≥ 需要 15 GB` → `preflight: 7/7` | `install.sh.bak-round52-r14` |
| 3 | `link_models` 在 `--dry-run` 里不做存在性检查 | `install_dry6_df.log:158 [FAIL] OCR 模型目录不存在：…snapshots/master`，rc=1 | `install_dry7_df.log` rc=0、0 条 `[FAIL]`、138 条 `[DRY]` | `install.sh.bak-round52-r15` |

★ 三条都属于「**说谎的报错**」这一类：两条假 FAIL 会把人指向「磁盘不行 / 模型名写错」，
  而真因只是「还没解包」。第 1 条相反 —— 它**不报错**却把行写进了真账号的 crontab（静默副作用）。
  行数：`install.sh` 832 → 838 → 840 → 842 行（`wc -l`）。

★ 源机 crontab 已恢复原状（见 §6-5）。恢复命令与验证：

```
$ crontab /home/ubuntu/medrec-deploy-round52/backup/20261020-132144/crontab.before   → RESTORED rc=0
$ crontab -l | cat -A                                                               → 只有一行 "$"（空）
```

## §3 tune.sh 的实际输出

### 3.1 L0 + L1 + L2（源机实测，`work/tune_final.log`）

```
[OK] 指纹：NVIDIA GeForce RTX 4090 x 4（24564 MiB/卡，显存带宽 1008 GB/s）｜CPU 144 核｜/home/ubuntu/medrec 空闲 150 GB
[OK] vLLM 端口 8110 model=PaddleOCR-VL（期望含 PaddleOCR-VL）
[OK] Strata 端口 8080 model=qwen3.8-flash-next-iq3_s（期望含 qwen3.8-flash-next）
  [01] 端到端 0.408640s  TTFT 0.065956s  生成 206 tok  TPOT 1.672 ms
  ...
  [10] 端到端 0.477671s  TTFT 0.040885s  生成 206 tok  TPOT 2.131 ms
[..] 均值：TPOT 1.859 ms｜单请求 0.433s｜串行吞吐 2.309 req/s｜生成 2060 tok（10/10 次成功）
[..] 预期 TPOT = 1800/1008 = 1.79 ms（brief §5.3：1.8 GB 权重 / 显存带宽）
[OK] 判定：TPOT 1.859 ms 落在预期区间（预期 1.79 ms，容差 15%）—— 这台机器正常，可以往下调参。
```

判据满足：L2 输出里有**明确判断**（不是只给一个数），且带「>5 ms 判机器有问题」的分支（`tune.sh` 里 `L2_EXPECT` / `L2_BAD_MS=5`）。
L40 的预期值由查找表算：`1800/864 = 2.08 ms`（brief §5.3 给的 L40 带宽 864 GB/s）。

### 3.2 `source` 三种取值都出现过

| 取值 | 怎么跑出来的 | 产物 |
|---|---|---|
| `table:4090-4x` | `bash tune.sh --level L0,L1,L2`（指纹自动命中查找表） | `/home/ubuntu/medrec/workspace/tuning.json` |
| `default` | `bash tune.sh --level L0 --profile l40-2x`（L40×2 在 brief §5.4 是「待测」→ 只给保守默认 R15=24 R23=32） | `work/tuning-l40-default.json` |
| `sweep:20261020` | `bash tune.sh --level L0 --sweep --bench-dir .../smoke_src --cases 2 --min-cases 1 --arms 16x16` | `work/tuning-sweep.json` |

★ 第三行是**管道演示**，不是调参结论：只有 2 例、1 个臂，且 `RUN_META.warnings=2`（跑批期间有外部 Strata 请求插队）。
  `tune.sh` 自己把这条 WARN 打出来了。L40×2 的真实 L3 必须在目标机上用真实病历目录跑（≥20 例/臂，`--min-cases` 默认 20）。

### 3.3 只读自证（brief §5.1 / 验收 Part B 第 1 条）

每次跑完都打：

```
[OK] 只读自证：config/ + scripts/config.env 跑前后 md5 逐文件一致（19 个文件）
```

`tune.sh` 全程不写 `config/`、不改 `config.env`、不重启服务；唯一输出是 `tuning.json`（默认 `$MEDREC_ROOT/workspace/tuning.json`，可用 `--out` 指到别处）。
L3 会另起批次目录（`SWEEP_DIR`，默认 `$MEDREC_ROOT/workspace/tune-sweep/`）—— 这是 brief §6 允许的例外，因为 s/例 只能真跑批测；产物与正式 `run_root` 隔离。

### 3.4 L3 默认不跑

```
[SKIP] L3 默认不跑（brief §5.2：一次 20-60 min）。要跑：--sweep --bench-dir <病历目录>
```

## §4 brief §3.5 七个陷阱的处置

| # | 陷阱 | 源机实测值（原始命令） | 处置 | 状态 |
|---|---|---|---|---|
| 1 | `config.env` 的 GPU 变量是旧共卡值 | `sed -n 44p scripts/config.env` → `export GPU_VL_TP="0"`；但线上 8110 实际在 **GPU1**（`nvidia-smi` + `/proc/1023536/cmdline`）。真钩子是 `10-start-ocr.sh:55 VLLM_GPU="${OCR_VLLM_GPU:-$GPU_VL_TP}"` | `install.sh` 写 `OCR_VLLM_GPU=0`（可 `OCR_VLLM_GPU=` 覆盖） | 已落地 |
| 2 | `--gpu-memory-utilization 0.7` 是 24 GB 卡算的 | `sed -n 116p scripts/config.env` → `export OCR_VLLM_GMU=0.7`（24564 MiB × 0.7 = 16.8 GB）；L40 48 GB 上 0.7 = 33.6 GB，同卡挤爆 | 写 `OCR_VLLM_GMU=0.22`（48 GB × 0.22 ≈ 10.6 GB，给同卡留位） | 已落地 |
| 3 | Strata 配置里 `gpu` 是源机的卡 | `grep -n "\"gpu\"" ~/Strata/strata-iq3_s.json` → `"gpu": [2, 3],` | `pkg_tool.py patchjson $STRATA_CFG gpu "[$STRATA_GPU]"`，默认 `[1]`；改前自动 `.bak-round52` | 已落地（Strata 存在时） |
| 4 | 文档写的 `--max-num-seqs 1` 已过时 | `tr "\\0" " " < /proc/1023536/cmdline` → 线上实参 `--max-num-seqs 64 --no-enable-prefix-caching --max-num-batched-tokens 8192` | `install.sh` 写 `OCR_VLLM_DETERMINISM="--max-num-seqs 64 --no-enable-prefix-caching --max-num-batched-tokens 16384"`，并同步 `OCR_MAX_NUM_SEQS` / `OCR_MAX_NUM_BATCHED_TOKENS` 给 `start-all.sh`（它自己拼 argv） | 已落地；**16384 是 round51 A 段的方向，最终值待裁决（§6-6）** |
| 5 | locate 先验键名对不上 | `locate_pages.py:317-323 prior_row()` 用 `case_id.split("__")[0]` 精确匹配，批次键是拼音（`jinhua_rmyy`）、yaml 键是中文短名（`安吉`）⇒ 240/240 `matched=False` | **本轮不改代码**（改 `src/wide/` 不在本轮授权内）。round51 已出 `config/locate_prior.yaml` 草案 | 未落地，见 §6-4 |
| 6 | locate 缓存无条件写 | `locate_pages.py:624` 只挡「读」缓存，`:1086-1091` 无条件写 | 不改代码；`install.sh` 的冒烟固定带 `--no-locate-cache`，bench 用独立批次名 | 规避，见 §6-4 |
| 7 | OCR 日志只增不轮转 | `wc -c -l logs/ocr_vllm.log` → `105200918 B / 1080381 行`（本轮实测，还在涨） | `pkg_tool.py write-rotate` 生成 `scripts/rotate-logs.sh`：**copytruncate**（`cp -a` → `truncate -s 0` → `gzip`），阈值 `MEDREC_LOG_MAX_BYTES` 默认 512 MiB；默认再挂一条 crontab（每 30 min，改前 `crontab -l` 备份）；另出 `/etc/logrotate.d` 草案（不 sudo） | 已落地 |

★ 陷阱 7 为什么只能 copytruncate：产线执行器持 `O_APPEND` 的旧 fd，`rename`+新建之后它继续写旧 inode ⇒ 日志**静默断流**（不报错、账全错）。

## §5 未知项

1. **L40 还是 L40S**：源机没有 L40，查不到。`nvidia-smi --query-gpu=name` 在目标机上跑一次即可确认。
   影响：显存带宽（L40 864 GB/s / L40S 同为 864 GB/s，显存 48 GB 相同）⇒ TPOT 预期 2.08 ms 不变；但 `tune.sh` 的指纹匹配靠 `name` 里的 `L40` 字样，两者都能命中 `l40-2x`。
2. **L40×2 的 R15 / R23 最优值**：brief §5.4 标「待测」，本轮没有 L40 硬件 ⇒ 只能给保守默认 24 / 32（`source=default`）。必须在目标机跑 `tune.sh --sweep` 才有实测值。
3. **Strata 在 2×L40 上能不能起来**：上游 `setup.py` 的 IQ3_S 档 `download_gb=83.6 / ram_gb=62`，
   源机实测落盘 **86 GB**（`du -sh /raid5/strata-data` → `86G`；models 78 + mtp 6.5 + packs 1.5），
   46.84 GiB 是**专家权重**那一份、`--kv int8` 另算 ⇒ 单张 48 GB 的 L40 放不下全部，源机用的是 GPU1/2/3 三卡。
   目标机只有 1 张卡给 Strata ⇒ `setup.sh --gpu 1` 会不会退化成 CPU 卸载、冷启动多久、s3 抽取速度多少，**全部 unknown**。
   缺的信息：① 目标机上跑一次 `~/strata-ctl.sh status` + 一次真实补全；② **目标机 RAM 是否 ≥ 62 GB**
   （`ram_gb=62` 是上游给的门槛，源机没在这条上验过 L40）；③ **目标机磁盘分区布局** —— 86 GB 落在
   `$MEDREC_ROOT` 所在分区之外的那个分区时，preflight ④ 只按 `MEDREC_ROOT` 的 `df` 算，会算漏
   （默认 `STRATA_DATA=$MEDREC_ROOT/strata-data`（`install.sh:28`）⇒ 与 ④ 的 `df` 同分区，**不会**算漏；
   只有人把 `STRATA_DATA=` 指到别的分区时 ④ 才看不见那 90 GB —— 此时 `install.sh:445,462` 会把它
   传给 `setup.sh --data-dir`，但 ④ 仍按 `MEDREC_ROOT` 算。目标机若两个分区，先确认这一点。）
4. **conda 环境靠联网 pip 装**（本轮已改成默认代跑）：`wheels/` 里只有三个 `freeze-*.txt`，**没有 .whl**
   （brief §4.1 说 assets 含「离线 wheels」，实际没打进去）。现在 `install.sh` 默认跑
   `conda create -n <env> -y python=3.12` + `pip install --no-input --retries 5 --timeout 120 -r env/install-<env>.txt`
   （§1.4）。仍 unknown 的三件事：
   ① 目标机能否出 PyPI（不可达时 preflight ⑥ 打 `[WARN]` 并提示 `PIP_INDEX_URL=`，但**不 FAIL** —— 环境装不出来要到第 3 步才炸）；
   ② `paddlepaddle-gpu` 的 **cu129** wheel 能否在目标机驱动上装（源机驱动 580.178.04；目标机若 <580 需换 URL）；
   ③ 洗单后的清单**从未在干净环境里装过**（源机红线不许 `pip install`）—— 三份清单只做过「生成 + 逐行核对」。
   完全离线 ⇒ 需另打 wheel 包：`pip download -r env/install-vl-env.txt -d wheels/vl-env/`（在能出网的机器上跑）。
5. **真实安装未在真目标机上跑过**：源机不是 2×L40，真装会覆盖源机仓库。本轮用「假根 + 假 nvidia-smi」跑了完整安装路径（§2.2 记录 B），但目标机上的真装仍需人跑一次。
6. **冒烟样本是合成假病历**（`测试医院/SMOKE0001`、`SMOKE0002`，各 3 页）：宽表 23 列 / 2 行 / `主诉=2/2 查体=2/2` 只证明**链路通**，不代表真实病历的字段命中率。
7. **`config/columns.yaml` 版本**：包里带的是源机当前版（`2026-10-15.1`，23 列）。目标机若要用别的模板，需换 code 包（本轮不改正式配置）。
8. **目标机能否出 GitHub / HuggingFace**：Strata 不在包里 ⇒ 装的时候必须抓。本轮在源机实测**同一小时内**
   GitHub 时好时坏（同一台机器、同一份脚本）：13:01 `work/install_dry2.log:15` 与 13:03
   `work/install_preflight3.log:17` 都是 `GitHub 可达：Strata 引擎与权重可在安装时抓取`；
   13:04 `work/install_preflight4_l40fake.log:11` 与 13:05 `work/install_preflight5_src.log:15`
   变成 `[WARN] GitHub 不可达：Strata 装不了`。
   缺的信息：目标机 `curl -sS -m 15 -o /dev/null -w '%{http_code}\n' https://github.com` 的返回值；
   不可达 ⇒ 用 `STRATA_GIT_URL=` 指镜像（源机实际用 `https://ghfast.top/https://github.com/Niko1221/Strata.git`），
   或走 §6-1 的 B 方案人工搬 GGUF。
   ★ 本轮 13:36 又抓到一次相反的：`work/install_fake15_bundle.log:15` 是 `[WARN] GitHub 不可达`，
     同一分钟 `work/install_dry8_bundle.log` 是「GitHub 可达」⇒ 这条探测**本身不可靠**，
     目标机上以第 3 步 `git clone` 的实际结果为准。
9. **`--weights-online` 只跑过 `--dry-run`**：源机红线不许下 5 GB 权重。已验的是「7 条落点 + 已就位则 SKIP +
   modelscope CLI 找得到」（`work/install_dry4_wonline.log:57-66`）和「6 个 id `GET` 返回 200」
   （`work/modelscope_probe2.txt`）。**未验**：真下要多久、`modelscope download --local_dir` 的落点层级
   是否与 assets 解出来的 `snapshots/master` 完全一致、`PP-LCNet_x1_0_doc_ori` 等 4 个预热模型是不是
   PaddleX 真正会去要的那几个 id。
10. **`modelscope` CLI 必须在 conda 环境里存在**：`weights_online()` 在 vl-env / ocr-env / qwen-env 里找
    `<base>/envs/<env>/bin/modelscope`，找不到只打一行补救（`install.sh:513-518`）。源机 vl-env 里有，
    目标机取决于 pip 清单里 `modelscope` 是否装成功 —— unknown。
11. **bundle 体积**：单一包 3.39 GB。若目标机带宽够而搬运 3.39 GB 反而麻烦，可只发 code 包（0.76 MB）
+ `--weights-online`；此时 assets 的 `smoke/`（冒烟样本）与 `modules/` 也没了，冒烟会 `[WARN] 缺 smoke` 跳过 —— unknown 是否可接受。
12. **解包按组重压 5 次**（本轮实测出来的成本，未修）：`install.sh:332` 是**每组一次**
    `tar -xzf … -T members.txt`，assets 有 5 组 ⇒ 那个 3.6 GB 的 gzip 被完整解压 5 遍。
    真装一次（含 sha 校验 + verify）实测 **5 min 52 s**（`work/install_fake15_bundle.log` 日志窗口
    13:41:25 → 13:47:17）。能改成一趟解完再按 manifest 分发，或把 assets 拆成 5 个小包；
    本轮不动 —— 能跑，且改了要把整套 verify 逻辑重验一遍。
13. **自己踩过一次「读日志读早了」**（本轮的假缺陷，已证伪，记下来防重犯）：
    `work/make_bundle3.log` 当时只有 3 行（打包行 + 空行 + `===== 打包后自查 =====`），
    我据此判定「自查 stdout 丢了」。实际是**自查还没跑完就去读** —— `tar -tzf` 要过一遍 3.6 GB gzip，
    要 ~60 s。证据：`ls -l work/make_bundle*.log` → 1/2/3 版日志**都是 572 字节**（内容完整），
    第四版 `work/make_bundle4.log` 跑完后同样完整（`条目数：18` / `禁运内容命中：0` /
    `[OK] 15 个必需成员都在` / `大小：3638101582 字节` / sha `75a4fb744f68…`）。
    ⇒ 不是包的缺陷，是取证纪律：**日志没看到结尾 = 还没跑完，不能下结论**。
    顺手加了两条防线（不是修 bug，是把证据固化）：自查结果同时落
    `work/bundle_selfcheck.txt`；sha256 同时写包外 sidecar `…tar.gz.sha256`。
14. **`--dry-run` 不带 `MEDREC_ROOT` 会指向 `$HOME/medrec`**（`install.sh:27` 的默认值）：
    在**目标机**上这是对的（那里就是安装根）；但在**源机**上演练时它指向真实产线根。
    本轮踩过一次：`work/install_dry9_bundle3.log:1` 是 `MEDREC_ROOT=/home/ubuntu/medrec`。
    因为 `DRY=1` 全程没写盘 —— 复核 `crontab -l | cat -A` 仍只剩一行 `$`、
    `find /home/ubuntu/medrec -maxdepth 1 -newermt "2026-10-20 13:50"` 空输出。
    ⇒ 不是包缺陷，是**演练纪律**：源机上任何 `install.sh`（含 `--dry-run`）必须同时覆盖
    `HOME / MEDREC_ROOT / STRATA_HOME / LEDGER / STATE / MARK` 三个以上，且带 `--no-cron`。
    `QUICKSTART.txt` 只面向目标机，故不改。

## §6 待裁决

1. **Strata 到底怎么上目标机**（人已定「不打包、安装时抓」）：
   - A：目标机能出网 ⇒ `bash install.sh online` 自动 `git clone` + `setup.sh`
     （上游 `setup.py` 的 IQ3_S `download_gb=83.6`，源机实测落盘 **86 GB**；引擎 clone 657 MB。
     556 s 是**启动时**把 46.84 GiB 专家铺进 VRAM 的耗时，不是下载耗时）。
   - B：目标机不出网 ⇒ 必须人先把 GGUF 拷过去，`STRATA_GGUF_DIR=/path bash install.sh online`（不再下载）。
   - 代价：A 装的时候占 **86 GB** 磁盘 + 下载时间；B 需要人工搬 **86 GB**。
     `git` 走的是 `https://github.com/Niko1221/Strata.git`，源机实际用的是镜像 `https://ghfast.top/...`（`git -C ~/Strata remote -v`）⇒ 国内直连慢时用 `STRATA_GIT_URL=` 指镜像。
2. **冒烟样本要不要换成真实病历**：换 ⇒ 冒烟才有字段命中率意义；不换 ⇒ 零 PII 外发风险，但只验链路。当前是**合成假病历**（`测试医院`，姓名「测试甲/测试乙」）。
3. **L40×2 的 K/CONC 是否在安装时预设**：本轮**不自动改** `R15_CASE_PARALLEL` / `R23_OCR_CONCURRENCY`（红线⑦：没有实测就不写）。装完先 `tune.sh --sweep` 再由人写。
4. **陷阱 5/6 是否本轮落地**：round51 已出 `config/locate_prior.yaml` 草案（键名映射 + 每院区 geometry）。落地要改 `config/` 与 `src/wide/locate_pages.py`，不在本轮授权内。
5. **源机副作用披露**（都是新增/追加，没有改动或删除既有文件）：
   - 新建 `/home/ubuntu/medrec/workspace/tuning.json`（tune.sh 的默认输出位置，工地目录）
   - 冒烟跑批自动追加一行到 `/home/ubuntu/medrec/run/wide/_health/baselines.jsonl`（日志里 `BASELINE_APPENDED=`）
   - 新建演练目录 `work/smoke-run/r52smoke/`、`work/tune-sweep/`、`work/fakeroot/`…`work/fakeroot6/`
     本轮再加 `work/fakeroot7/`（4.3 GB 真装）、`work/bundle-test/`、`work/bundle-test2/`（各 3.4 GB）、
     `work/bundle-stage/`（3.4 GB）、`work/fresh-dry/`（空壳）
     （fakeroot5 是被打断的半成品 4.3 GB，fakeroot6 已回滚到只剩空壳；都占盘，要不要清等人一句话）
   - 新建 `backup/<时间戳>/scripts/config.env`（每次安装/演练对被改配置的原样备份，共 9 份）与
     `install.ledger`（含 fakeroot5 的历史记账）、`install.state`、`.installed`
   - ★ **本轮曾污染过源机 crontab，已恢复**：`work/fakeroot7` 那次真装没带 `--no-cron`，
     而 `crontab` 按 **uid** 记账、不认 `HOME` 覆盖 ⇒ 往 ubuntu 的真 crontab 写了一条 rotate-logs 行。
     已用 `crontab backup/20261020-132144/crontab.before`（1 字节 = 原本为空）恢复，
     现在 `crontab -l` 只剩一个空行。同时给 `install.sh` 加了护栏（§2.5 修复 1），
     演练根下不再写 crontab：`work/install_fake14_idem.log` 末段
     `[..] 演练根（HOME=… ≠ 账号真实 home /home/ubuntu）：不写 crontab`。
   要不要清掉，等人一句话。
6. **8110 的 `--max-num-batched-tokens`**：线上现在 **8192**，包里写 **16384**（round51 A 段的方向）。
   若人裁决「保持 8192」⇒ 安装时给 `OCR_MAX_NUM_BATCHED_TOKENS=8192`，或装完改回。
7. **crontab**：`install.sh` 默认给目标机加一条每 30 min 的日志轮转 cron（改前备份原 crontab 到 `backup/<ts>/crontab.before`）。不想动 crontab 就 `--no-cron`。
8. **conda 环境默认【代跑】是否接受**（本轮按「解压即装」的要求把默认翻成 ON）：裸 `bash install.sh`
   会 `conda create` 三个环境 + `pip install` 共 735 条包，**要出网、要几十分钟**，且 §5-4 的三个 unknown
   全在这一步兑现。想先看命令再自己跑 ⇒ `--no-env`（或 `INSTALL_ENVS=0`）。
9. **发出去的 bundle 含不含 assets**：现在含 ⇒ 3.39 GB。若目标机出网稳、嫌搬 3.4 GB 麻烦，
   可只发 code 包（0.76 MB）+ `bash install.sh --weights-online`；代价是冒烟样本 `smoke/` 与
   `modules/` 也没了（冒烟会 `[WARN] 缺 smoke` 跳过），且权重路线本身只跑过 dry-run（§5-9）。
10. **目标机上是否强制先 `--dry-run`**：`QUICKSTART.txt` 第 2 步已写成先干跑。本轮修的两处假报错
    （§2.5 修复 2/3）正是为了让这条干跑在**全新目录**上能 rc=0 —— 否则人第一次自检就会被
    「df 查不到分区」「OCR 模型目录不存在」两条假 FAIL 劝退。

## §7 brief §8 验收清单（15 条）

### Part A（9 条，全勾）

| # | 条目 | 状态 | 证据（命令 → 关键行） |
|---|---|---|---|
| A1 | 两个包 + 各带 `manifest.json` | ✅ | §1；`medrec-code-round52.manifest.json` / `medrec-assets-round52.manifest.json` |
| A2 | 包内无 `run/ workspace/ logs/ __pycache__` | ✅ | `tar -tzf medrec-code-round52.tar.gz \| grep -cE "(^\|/)(run\|workspace\|logs\|__pycache__\|\.git)(/\|$)\|\.bak\|~$"` → `0`；assets 同（`work/lists/assets_archive_listing.txt` 178 行 → `0`） |
| A3 | `--help` 打印全部子命令 | ✅ | `work/install_help3.log`（本轮重跑，rc=0）：`install / preflight / online / offline / --rollback / --help` 六个都在，开关行含新增的 `--no-env` `--weights-online` |
| A4 | preflight 7 项都有 `[OK]`/`[FAIL+原因]` | ✅ | 源机 `work/install_dry2.log`（5/7，③⑤ FAIL 带补救）；假根 `work/install_fake9_install.log`（7/7）；补 Strata 磁盘后：源机 `work/install_preflight5_src.log`（5/7，④ `[OK] … ≥ 需要 105 GB`）、`work/install_preflight6_src_nostrata.log`（`--skip-strata` → `需要 15 GB`）、假 2×L40 `work/install_preflight4_l40fake.log`（**7/7 rc=0**） |
| A5 | `--dry-run` 零副作用 | ✅ | §2.1：`md5sum config/* + scripts/config.env` 前后 `MD5_SAME`；`find -maxdepth 1` 前后 `LS_SAME` |
| A6 | 三个启停脚本存在，`health.sh` 输出与 §4.4 样例逐字一致 | ✅ | `bash health.sh` 实跑：三行 `8080 Strata [OK] /v1/models → qwen3.8-flash-next-iq3_s` / `8110 vLLM [OK] …PaddleOCR-VL` / `8090 webui [OK] HTTP 200`，rc=0 |
| A7 | webui 启动脚本里有 `cd` | ✅ | `start-all.sh:42 ( cd "$wd" && setsid nohup "$@" … )`；`:118` 还先查 `webui/static` 存在性 |
| A8 | 重跑 `install.sh` 显示全跳过 | ✅ | §2.2：6 组 `[SKIP] .installed 里两个包的 sha256 与当前包一致`，rc=0 |
| A9 | 冒烟产出 `06_table/wide.csv`，行数=2、列数=23 | ✅ | `work/smoke_run.log:367 TABLE … rows=2 cols=23 nonempty_cells=34/46 主诉=2/2 查体=2/2` |

### Part B（6 条，全勾）

| # | 条目 | 状态 | 证据 |
|---|---|---|---|
| B1 | `tune.sh` 不写 `config/`、不改 `config.env`、不重启服务 | ✅ | §3.3：每次跑完打 `[OK] 只读自证：config/ + scripts/config.env 跑前后 md5 逐文件一致（19 个文件）` |
| B2 | L0/L1/L2 能单独调用 | ✅ | `work/tune_l40_default.log`（`--level L0` 单跑，日志里 `[SKIP] L1 未选` `[SKIP] L2 未选`）；`work/tune_final.log`（`--level L0,L1,L2`） |
| B3 | L2 有「TPOT 是否落在预期区间」的明确判断 | ✅ | §3.1：`[OK] 判定：TPOT 1.859 ms 落在预期区间（预期 1.79 ms，容差 15%）`；另有 `L2_BAD_MS=5` 的「先修机器别调参」分支 |
| B4 | `tuning.json` 的 `source` 非空且三种取值都出现过 | ✅ | §3.2 表：`table:4090-4x` / `default` / `sweep:20261020` |
| B5 | L3 默认不跑，需 `--sweep` | ✅ | §3.4：`[SKIP] L3 默认不跑（brief §5.2：一次 20-60 min）。要跑：--sweep --bench-dir <病历目录>` |
| B6 | 卡型号表有 4090×4 一行且与源机实测一致 | ✅ | `tune.sh:116 4090-4x) SRC_TBL="table:4090-4x"`，`R15=24 R23=32`（`tune.sh:114-115`）与 `scripts/config.env:165-166` 的 `R15_CASE_PARALLEL=24 / R23_OCR_CONCURRENCY=32` 一致 |

★ 未勾 0 条。但 A9 的冒烟样本是**合成假病历**（§5-6），只证明链路通；
  真机（2×L40）上的完整 `install.sh`（含起服务 + Strata 抓取）**仍未跑过**（§5-5）—— 这两条不是勾不勾的问题，是必须在目标机补的验证。

### 增量验收（本轮「网络权重 + 单一 bundle」新增的 6 条）

| # | 条目 | 状态 | 证据（命令 → 关键行） |
|---|---|---|---|
| C1 | 单一 bundle 存在、可解、含 15 个必需成员、禁运命中 0 | ✅ | `bash make_bundle.sh` 自查（`work/make_bundle4.log` + `work/bundle_selfcheck.txt`）：`entries=18` / `files=16` / `banned_hits=0` / `required_members=15` / `bytes=3638101582` / `sha256=75a4fb744f68…`。核对最终包一律 `sha256sum -c medrec-round52-bundle.tar.gz.sha256` |
| C2 | 解包后 `install.sh` 能跑通干跑（解压即装路径成立） | ✅ | `work/install_dry8_bundle.log`：`rc=0`、`preflight: 7/7 项通过`、`138` 条 `[DRY]`、`0` 条 `[FAIL]` |
| C3 | 解包后 `install.sh` 能真装（解包 + verify + 权限 + 记账） | ✅ | `work/install_fake15_bundle.log`（`--no-env --skip-strata --no-start --no-cron`）：`preflight: 7/7`、6 组 `[OK] 解出`、`verify assets verified=134 sha_diff=0 missing=0`、`verify code verified=138 sha_diff=0 missing=0`、软链已建、`权限已设：10 个目录`、该根 ledger `60 CREATED / 1 PATCHED / 1 BACKUP`；日志窗口 13:41:25 → 13:47:17（**5 min 52 s**） |
| C4 | 幂等：同根重跑整组跳过 | ✅ | `work/install_fake14_idem.log`：6 组 `[SKIP] .installed 里两个包的 sha256 与当前包一致` |
| C5 | `--weights-online` 计划正确（不解两组、软链推迟、7 落点 + SKIP） | ✅ | `work/install_dry4_wonline.log:30,33,41,57-66`；6 个 id `GET` → `200`（`work/modelscope_probe2.txt`） |
| C6 | pip 清单可被 pip 吃（洗掉 VCS/自带包，保留 cu129 paddle URL） | ⚠ 半 | `python pkg_tool.py envlists …` → `kept=415/115/205`、`TOTAL_DROPPED 8`；**未在干净环境真装**（§5-4③） |
| C7 | 解包后的 `install.sh` 在**假根**下跑通干跑（覆盖 `HOME/MEDREC_ROOT/STRATA_HOME/LEDGER/STATE/MARK` + 端口 18080/18110/18090 + `work/shim` 假 `nvidia-smi`） | ✅ | `tar -xzf medrec-round52-bundle.tar.gz -C work/bundle-test3` → `EXTRACT_OK`、权限原样（`-rw-------` / `drwx------`）；`bash work/bundle-test3/medrec-round52/install.sh --dry-run --no-env --skip-strata --no-cron` → `work/install_dry10_bundle3.log`：`rc=0`、`preflight: 7/7 项通过`、`138` 条 `[DRY]`、`0` 条 `^[FAIL]`、`1` 条 `^[WARN]`（陷阱5/6 的固定提示，非故障）。与 C2 的 138 逐数一致 |

★ C3 的**真装**证据（`work/install_fake15_bundle.log`）是对 **make_bundle 第二版**的包做的；
  第三版只改了 `DELIVERY.md` / `QUICKSTART.txt` 两份文档（+6,892 B），代码与两个 tar.gz 逐字节未变
  （code `8c17a7f0fdae` / assets `5f3ed519fad1` 两个 sha 在 C1、C7 两次解包里都一致）
  ⇒ 真装结论可继承，但**最终包本身只做到 C7（干跑）**，没再真装一次。要补就在目标机上补。

★ C1/C7 里的 size 与 sha 是**当次打包的快照**；因为 `DELIVERY.md` 在包内，改文档就会让它们过期。
  核对一律用包外 sidecar：`sha256sum -c medrec-round52-bundle.tar.gz.sha256`（`make_bundle.sh` 自动生成）。
