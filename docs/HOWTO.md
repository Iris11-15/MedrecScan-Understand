# HOWTO —— 病例信息科研总结表产线（columns.yaml 23 列 / 宽表输出 23 列）

一行一例：病历首页患者信息（模板 32 列）+ 首程「主诉」原文 + 首程「查体」原文。
范围之外（检验/用药/诊断组、编码映射、RAG、CDISC/OMOP）**未实现**，只在代码里留了接口位。

> 列定义自 `2026-10-09.4` 起是 24 列（原 34 列删 10 列，含 4 个勾选框列）；
> `2026-10-15.1` 又删了 `就诊医院`（round33 §1.4：106/106 全空）⇒ 现在 yaml 里是 **23 列**。
> **宽表实际输出列数 = yaml 列数 − `normalize.drop_output_columns`**（当前 `[]` ⇒ **23 列**）。
> ⚠ **别再用「24 列 = 新链」判新旧链**（round34 E2：删列之后这条判据失效）。判据换成：
>   ① `medrec doctor` 打印的 `COLUMNS yaml=<n> drop=[…] expect_wide=<n>`；
>   ② `medrec check <batch>` 的 `WIDE cols=… expect=…`（基准 = columns.yaml − drop_output_columns）。
> 去重键形态同样由 doctor 打印（`去重键形态 key=quad 分量=[hospital_std, case_no, adm_no, date]`）。
> 变更留痕见 `docs/column_mapping.md` §〇。

---

## 0. 前置

```bash
P=/home/ubuntu/anaconda3/envs/vl-env/bin/python     # 必须绝对路径：PATH 里 fsl 会抢 python
cd /home/ubuntu/medrec/src                          # 所有命令都在 src/ 下以 -m 方式跑
```

两个本机服务（**只许调用，不许改**）：

| 服务 | 地址 | 自检 |
|---|---|---|
| OCR：PaddleOCR-VL-1.6 | `http://127.0.0.1:8100/layout-parsing` | `curl -s http://127.0.0.1:8100/health` |
| LLM：Strata `qwen3.8-flash-next-iq3_s` | `http://127.0.0.1:8080/v1/chat/completions` | `curl -s http://127.0.0.1:8080/v1/models` |

发请求前必须清代理（`common.http_json` 已内置 `clear_proxy()`，手工 curl 时要自己加
`env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY`）。
本机 `127.0.0.1:7897` 是无效代理，挂着它请求会超时。

配置只有三个文件，代码里不硬编码参数：

- `config/columns.yaml` —— **宽表列定义的唯一来源**（`2026-10-15.1` = 23 列，含重名列、`mirror_of`、`source`、`pii`、`candidate_labels`）
  ⚠ 23 是 **yaml 里的列数**；宽表输出 = 23 − `normalize.drop_output_columns`（当前 `[]` ⇒ **23**）。
- `config/pipeline.yaml` —— 服务地址、OCR 变体、空白页守卫、LLM 温度/`max_tokens`、定位词表、PII 与 QC 阈值
- `config/label_vocab.yaml` —— 首页标签写法种子表（别名 → 标准写法）
- `config/field_mapping.yaml` —— **第一遍映射的产出**（由 `tools/pass1_mapping.py` 写，人工确认后 `status: confirmed`）

**OCR 服务（8100）的后端与重启**：当前跑的是**朴素后端**（不带 `--use_hpip`），启动命令原文：

```bash
/home/ubuntu/anaconda3/envs/vl-env/bin/python -m paddlex --serve \
  --pipeline PaddleOCR-VL-1.6 --host 127.0.0.1 --port 8100 --device gpu:0   # scripts/10-start-ocr.sh
```

`--use_hpip`（FastDeploy / 高性能推理插件）**未启用**：环境缺依赖，且 PaddleX 的 HPI 模型清单里
**没有 PaddleOCR-VL 条目**（详见 `reports/speedup-round2-20261009.md` 步④）。别照着通用教程
直接加 `--use_hpip` 重起 —— 现在加了会 `DependencyError: The high-performance inference plugin is not available.`。

---

## 1. 一条命令跑单例

```bash
cd /home/ubuntu/medrec/src
B=smoke; CID='奉化__20230728_008'
$P -u -m wide.tools.locate_pages --batch $B --case "$CID" && \
$P -u -m wide.s1_ocr          --batch $B --case "$CID" --crop-blocks && \
$P -u -m wide.s2_preprocess   --batch $B --case "$CID" && \
$P -u -m wide.s3_extract      --batch $B --case "$CID" && \
$P -u -m wide.s4_normalize    --batch $B --case "$CID" && \
$P -u -m wide.s5_qc           --batch $B && \
$P -u -m wide.s6_table        --batch $B
```

`wide.s4_normalize` 默认**拒绝**用未确认的映射表（`config/field_mapping.yaml` 的
`status` 必须是 `confirmed`）。试跑加 `--allow-draft`，正式产出不要加。

**整链 + 逐段计时（测速 / 排错推荐用这个，标志位不会漏）**：

```bash
$P -u -m wide.tools.run_steps --batch $B --case "$CID" --force --allow-draft \
     --ocr-log /tmp/paddlex_ocr.log --json-out /home/ubuntu/medrec/run/tmp/speed/x.json
```

逐段 `wall_ms` + `rc` + 该段 **OCR 请求数增量** + 定位扫页记录（`locate_scan`）全部写进 `--json-out`。
OCR 请求数**两个口径同时记**：主口径 `ocr_requests` = PaddleX 日志里 `POST /layout-parsing` 的行数增量；
对照口径 `ocr_wc_delta` = 整日志 `wc -l` 增量（另存 `ocr_log_wc_before/after`）。
以 POST 为主是因为 `wc -l` 会把 `GET /v1/models` 健康检查也算成请求；实测同一窗口两口径相等
（报告 `reports/speedup-20261009.md` §6.1）。`--no-locate-cache` 会转成 `locate_pages --no-cache`
（测提速时必须加，否则缓存会冒充提速）。

**rc 语义**：某段 rc≠0 **不会**中止后续段（计时口径要跑完整链），
但进程退出码 = 第一个非 0 的 rc，且每段都打 `--- <step> rc=… <ms> ms ocr_requests=… ---`。
排错时逐段 grep：`grep -E "^--- .* rc=" <json-out 同名 .log>`，别只看最后一行。

## 2. 一条命令跑批量

```bash
cd /home/ubuntu/medrec/src
B=smoke; CF=/home/ubuntu/medrec/run/tmp/sample15.txt     # 一行一个 case_id
$P -u -m wide.tools.locate_pages --batch $B --case-file "$CF" && \
$P -u -m wide.s1_ocr          --batch $B --case-file "$CF" --crop-blocks && \
$P -u -m wide.s2_preprocess   --batch $B --case-file "$CF" && \
$P -u -m wide.s3_extract      --batch $B --case-file "$CF" && \
$P -u -m wide.s4_normalize    --batch $B && \
$P -u -m wide.s5_qc           --batch $B && \
$P -u -m wide.s6_table        --batch $B
```

- 每个阶段都认 `--case`（可重复）与 `--case-file`；不给就是全批次（`00_index/manifest.tsv`）。
- **批量前建议先跑离线定位**（不调 8100/8080，实测 106 例 ≈15 min）：

  ```bash
  $P -u -m wide.tools.map_pages_from_old --batch $B      # → 00_index/page_index.json
  ```

  实测（106 例，`reports/rebuild-20261009.md` §3「全量映射覆盖率」）：**104/106 可映射、97 例首页+首程都定位到**、选中页像素对账最小 corr 0.9991。
  剩下 **9 例**（玉环第一 5、长兴人民 4）离线定位拿不到 → 再用 `wide.tools.locate_pages` 现场扫页眉条，**务必给 `--max-scan-pages`**（266 页的例全扫 ≈45–60 min）。
  离线映射只用来**选页**，不取任何字段值；每例在 `locator_source` / `evidence` / `verify_corr` 里留来源。
- ①-④ 例级幂等：例目录里有 `.done` 就跳过，重跑加 `--force`（`--force` 是**例级**，会重跑该例的全部选中页）。
- ⑤ 质控有 `--force`；⑥ 表格是**批次级汇总**，每次跑都重算，**没有** `--force`（传了会 `unrecognized arguments` 直接退出）。
- 阶段之间**只通过文件交接**，任何一步都能单独重跑；`.incomplete` 表示该例有页级/环节失败。
- 长任务后台跑：`setsid nohup bash run/tmp/run_sample15.sh > run/tmp/sample15_driver.log 2>&1 < /dev/null & disown`
  （必须 `python -u`，否则日志块缓冲看不到进度）。

## 3. 六阶段：输入 / 输出 / 落盘

`run/wide/<batch>/` 下按阶段分目录（例：`run/wide/smoke/`）。

| 阶段 | 命令 | 输入 | 输出 |
|---|---|---|---|
| ⓪ 页面定位 | `wide.tools.locate_pages` | `00_index/manifest.tsv` + 源 PDF | `00_index/page_index.json`（首页页号 = 标题页+续页、首程窗口、evidence、strips 摘要、跳过的低信息页） |
| ① OCR | `wide.s1_ocr` | `page_index.json` + 源 PDF | `01_ocr/<case>/page_NNNN.png`、`page_NNNN.json`（blocks + block_bbox + 副变体 `blocks_*`）、`_manifest.json`、`.done`/`.incomplete`、可选 `block_NNN.png` 裁剪图 |
| ② 预处理 | `wide.s2_preprocess` | `01_ocr/<case>/page_NNNN.json` | `02_text/<case>/page_NNNN.json`（表格 HTML → `cells` 矩阵、去重复行、`text`/`text_<variant>`、输入输出哈希）、`page_NNNN.md` |
| ③ 抽取 | `wide.s3_extract` | `02_text` + `page_index.json` + `label_vocab.yaml` | `03_extract/<case>/frontpage.json`（cell/block/coord/llm 四种策略候选 + 命中数）、`first_progress.json`（主诉/查体原文 + `verbatim` 原文照抄核对：抽取值是否为输入文本的子串）、`label_inventory.json` |
| ④ 归一化 | `wide.s4_normalize` | `03_extract` + `config/field_mapping.yaml` | `04_norm/<case>/fields.json`（34 列取值 + `source_label`/`strategy`/`mapping_version`） |
| ⑤ 质控 | `wide.s5_qc` | `04_norm` | `05_qc/<case>/qc.json`；`05_qc/_summary/{missing_rates.csv,variable_dictionary.csv,outliers.csv}` |
| ⑥ 表格 | `wide.s6_table` | `04_norm` + `05_qc` | `06_table/{wide.csv,wide.xlsx,qc_flags.csv,audit.jsonl,_manifest.json}` |
「原文照抄」是硬要求，所以 `s3` 会做**子串核对**（空白归一化后判断抽取值是否出现在首程文本里），
结果写进 `03_extract/<case>/first_progress.json` 的 `verbatim`，`s4` 透传到字段记录，
`s5` 在不一致时打 `not_verbatim` flag（只记布尔与长度，不回显原文）。核对失败**不会**自动改值。


第一遍映射（阶段②之后、阶段④之前跑）：

```bash
$P -u -m wide.tools.pass1_mapping --batch $B --case-file "$CF"      # 产出 draft
# 人核对 docs/column_mapping.md → 把 config/field_mapping.yaml 的 status 改成 confirmed
```

审计日志：`run/wide/<batch>/_audit.jsonl`（各阶段追加）。每例每页都有
**OCR 输入哈希**（`png_sha256`）、**OCR 响应哈希**、**Prompt 哈希**（`prompt_sha256`/`system_sha256`）、
**输出哈希**（`content_sha256`/`fields_sha256`）；`s6` 汇总成 `06_table/audit.jsonl`（PII 已打码）。

质控三件套（`s5`）分工，别把三张表当同一张看：

- `05_qc/_summary/missing_rates.csv` —— 每字段缺失率（`n_rows/n_filled/n_null/missing_rate` + 打码示例值）
- `05_qc/_summary/variable_dictionary.csv` —— 变量字典（列序 / id / 表头 / source / 是否 PII / dtype / 取值域 / 定义）
- `05_qc/_summary/outliers.csv` —— **只放异常与冲突**（`format` / `outlier` / `conflict` / `single_path` / `bleed` / `not_verbatim`），缺失不在此重复
- 全量 flag（含 `missing`）在 `06_table/qc_flags.csv`

## 4. 参数与默认值

`wide.tools.locate_pages`

| 参数 | 默认 | 含义 |
|---|---|---|
| `--batch` | 必填 | 批次名 |
| `--case` / `--case-file` | 全批 | 只定位这些例 |
| `--out` | `00_index/page_index.json` | 定位文件（逐例增量写，中途挂不丢已完成例） |
| `--prior-index` | 同目录 `*DEPRECATED*.json` | 旧批次页号，**只用来排扫描顺序**，不作结论 |
| `--strip-frac` | `0.20` | 页眉条高度占页高比例（实测 top20% 够读标题、1.2–3.2s/页；top12% 会漏关键词。**注意**：高度够不等于位置对，见下一行） |
| `--strip-positions` | `top` | 页眉条裁哪几段：`top` / `bottom` / `top,bottom`（拼成一张送 OCR，**调用次数不变**）。首页标题的纵向位置因院区而异，只裁 top 对安吉这类院区结构上失效 |
| `--fallback-frac` | `0.45` | 定位不全时加高重扫未覆盖页（0=关掉） |
| `--fallback-positions` | `top,bottom` | 兜底重扫裁哪几段（默认含 bottom，就是为了解决标题在页脚的院区） |
| `--max-scan-pages` | `0`（不限） | 单例最多扫多少页，超线停下并在索引里记 `scan_budget_hit: true`。页眉条 OCR 实测 ≈10–13.5s/页，266 页的例全扫 ≈45–60 min，所以批量时必须给（本轮 smoke 用 80） |
| `--grace-pages` | `25` | 退让早停：该例**没有**最高优先级首程写法（`首次病程记录`）时，已有次优命中（`入院记录`）且扫过这么多页就停，索引里记 `grace_used: true`。0=关掉（死等 top 写法会全扫到底） |
| `--grace-rank` | `pipeline.yaml:locator.grace_rank`=1 | 退让**门槛**：只有最佳首程命中的优先级 rank ≤ 此值才允许靠 grace 收手。rank 按 `locator.first_progress_priority` 排序（首次病程记录=0，病程记录=1，入院记录=2）。安吉 bug 就是 rank=2 的「入院记录」也能触发退让 |
| `--grace-pages-hard` | `pipeline.yaml:locator.grace_pages_hard`（缺省=2×`--grace-pages`） | 最佳首程命中 rank > `--grace-rank` 时，要扫过这么多页才允许退让（0=关） |
| `--no-geometry-preprobe` | 关（即默认**开**预探针） | 关掉「页眉几何预探针」：top 窄条读不到首页标题时，先用 bottom 窄条试同一批页，命中就把后续扫描切到 `20%×bottom`。安吉标题在页底页眉（y≈0.81–0.92），top 结构上看不见 |
| `--ring` | `12` | 预测位置外扩圈数 |
| `--no-early-exit` | 关 | 关掉早停、全页扫 |
| `--max-frontpage` / `--max-first-progress` | 2 / 3 | 首页最多取几页；首程窗口最多几页 |
| `--follow` | `pipeline.yaml:locator.first_progress_follow_pages`=2 | 首程标题页往后带几页（首程常跨页） |
| `--no-probe` | 关 | 关掉两阶段探针扫描，回到旧的 ring/两端全扫顺序（改动②的开关） |
| `--pn-window` | `25` | 探针猜不中首程时，最多外扩到锚点 ±这么多页；仍不中 → 回 ring 全扫兜底 |
| `--no-cache` | 关 | 忽略并覆盖定位缓存（**测提速时必须加**，否则缓存会冒充提速） |
| `--cache-dir` | `run/wide/_locate_cache/` | 定位缓存目录（跨批次共享，按内容哈希失效） |

定位的两条硬规则（都是实测踩出来的，改之前先看注释）：

- **扫描预算与退让**：`--max-scan-pages` 超线 → 该例可能定位不全（`scan_budget_hit: true`），
  报告里必须如实列出；补救：单独重跑该例并加大预算或 `--no-early-exit`。
  `grace_used: true` → 首程窗口取的是次优写法（如「入院记录」而非「首次病程记录」），
  主诉/查体原文仍来自该窗口，但报告里要写明写法优先级。
- **首页续页**：病案首页常占两页，续页**常常没有标题**。实测 `奉化__20230728_008`
  标题页在 0based 89，续页（手术及操作 / 伤口愈合 / 麻醉方式）在 0based 88，即标题页**前**一页。
  只扫 `p+1` 或只认关键词都会漏掉它 → 离院方式 / 手术 / 质控整页丢失。
  现在标题页 `p±1` 都扫，邻页若不是别的文书标题且命中
  `locator.frontpage_continuation_markers`（手术操作、伤口愈合、麻醉医师、离院方式、质控…）
  就并入 `frontpage_pages`，并在索引里分开记
  `frontpage_title_pages` / `frontpage_continuation_pages`（来源可追）。
- **低信息页不送 OCR**：定位阶段也用 `s1_ocr.classify_page()`（墨迹 + 灰度标准差），
  索引里 `pages_skipped_kind` 记每页判定的 `blank`/`low_info`。
- **标题不一定在页眉**：`run/tmp/probe/title_pos.py` 实测各院区「住院病案首页」标题的相对页高
  —— 桐乡 0.04–0.14、玉环 p0 0.07–0.17、长兴 p102 0.02–0.09，但**安吉 0.85–0.89**、
  玉环/长兴的**续页**标题也在 0.85–0.97。只裁 top 对安吉结构上失效（扫 25 页 0 命中）。
  解法：`--strip-positions top,bottom`（或兜底档默认 `top,bottom`）。
  验证（`run/tmp/stripbot.log`，只扫 4 页）：
  `安吉__img20231201_11004386: 首页 [264, 265]（标题 [265]/续页 [264]）`
  —— 与离线映射（`tools/map_pages_from_old.py`）给的 `[264, 265]` **互相印证**。

**两阶段少扫页（改动②，2026-10-09）**

旧顺序 = 先验预测位置 ±`--ring` 圈 + 两端向中间，实测奉化 008 要扫 13 页候选
（8 次页眉条 OCR）才早停。现在分两阶段探：

- **阶段 A 首页**：先验预测页 → 尾部两页 → 头部两页，命中即停
  （实测 15 例首页只在**末尾 2 页**（奉化/安吉/长兴）或**开头 2 页**（桐乡/玉环），没有夹中间的）。
- **阶段 B 首程**：以首页**锚点**（标题页；若续页在标题页**前**，实测奉化 008 就是这样，则用续页当锚点）
  为基准按实测相对偏移猜页 —— 尾部组首程在锚点**前** 7..15 页（实测偏移 7×3、11×2、13×2、15×2），
  头部组在锚点**后** 2..8 页（实测 2×5、8×1）；猜不中逐步外扩到 `--pn-window`。
- **兜底不丢内容**：探针没拿全（首页没命中，或最高优先级首程写法没命中）→ `probe.fallback_used=true`
  并回到旧的 ring/两端**全扫**。探针只是换顺序，**不会少扫页**。

奉化 008 实测：候选 13 页 → **扫 3 页**（89 首页标题 / 88 首页续页 / 77 首程标题），
页眉条 OCR 8 次 → **3 次**，结论与全扫完全一致（首页 `[88,89]`、首程 `[77,78,79]`）。

**定位缓存（改动①，2026-10-09）**

`run/wide/_locate_cache/<case_id>__<key[:16]>.json`。缓存键材料（任一变化 → 未命中）：
源 PDF `sha256` + `config/pipeline.yaml` `sha256` + **代码 sha**（`locate_pages.py` +
`s1_ocr.py` + `common.py`）+ 先验索引 `sha256` + 全部生效定位参数（词表 / 裁条比例 /
早停 / 兜底 / `blank_guard`）+ `case_id`。

- 命中前还要过 `cache_is_complete()`：`frontpage_pages` 非空、`evidence` 非空且每条带
  `strip_sha256`、`strips` 非空、`pages_scanned == len(strips)`、`source_pages == 当前 PDF 页数`、
  PDF 非 0 字节 —— 任一不过就当未命中现场重扫。
- **只有定位成功才写缓存**（定位失败的例下次仍现场重扫，不把失败固化）。
- 改了 prompt / 词表 / 扫描顺序 → 代码或配置哈希变 → 键变 → 旧缓存自动作废
  （实测：改动② 之后缓存文件名从 `…__8b916eee59378985.json` 变成 `…__ca01bb8d9f65b9a2.json`）。
- ⚠️ **缓存只利同一例重复跑（调试），对批量吞吐零收益** —— 106 例每例 PDF 哈希不同，必然未命中。

**如何看扫页记录（漏页时先查这里）**

- `00_index/page_index.json` 每例新增：`scan_trace`（逐页 `{page, phase, hit|skipped|error}`，
  `phase` ∈ `fp_probe0..2` / `pn_probe` / `probe_fallback` / `ring` / `taller_strip`）、
  `fp_hit_page`、`pn_hit_page`、`probe{group, anchor, pn_tried, pn_expansions, fallback_used}`、
  `pages_scanned` / `pages_blank_skipped` / `pages_skipped_kind`、`locate_cache{hit, key, built, cache_file}`。
- 命令行也会打印：`源 90 页 / 扫 3 页（空白跳过 0）→ 首页 [88, 89]（标题 [89]/续页 [88]） / 首程 [77, 78, 79]`
  和 `探针 组=tail 锚点=88 首程试=1 外扩=0 兜底=否`。
- 整链跑时 `run_steps.py` 的 JSON 里 `locate_scan.<case>` 同步带这些字段（缓存命中时
  `pages_scanned` 是缓存里的历史值，本次请求数 0 可佐证）。

`wide.s1_ocr`

| 参数 | 默认 | 含义 |
|---|---|---|
| `--all-pages` | 关 | OCR 全部页（默认只 OCR 定位到的页） |
| `--index` | `00_index/page_index.json` | 定位文件 |
| `--max-pages` | 0（不限） | 每例最多 OCR 几页 |
| `--crop-blocks` | 关 | 额外按 `block_bbox` 落裁剪图（验证风险③用，会多很多文件） |
| `--force` | 关 | 已 `.done` 也重跑 |

OCR 变体由 `config/pipeline.yaml:ocr.variants` 决定（`layout: true` 主 / `plain: false` 副），
`variants_frontpage_only: true` = 副变体只跑首页（勾选框在首页，首程页省掉副变体）。

**变体语义（`pipeline.yaml` v`2026-10-09.2` 起，改动③）**：

- 写 `false` 的变体**一律不跑**（旧代码只在「非首页」过滤 falsy 变体，首页照样把 `false`
  的副变体跑一遍 → 奉化 008 首页 `plain` 白跑 71.8 s + 13.6 s = 85.4 s）。
- `variants_frontpage_only` 只在**开着的**副变体上生效；副变体全关时它是空操作（不用改）。
- 主变体 = 声明顺序里**第一个开着**的变体；声明的第一位被关掉时打 `warn` 而不是静默换。
- `ocr.variants` 全 `false` → 该页记 `status=config_error` 并报错退出，不会静默产出空页。
- 当前 columns.yaml 的 23 列（宽表输出 23 列）**没有任何勾选框字段**（`2026-10-09.4` 删了性别/医保类型/入院途径/离院方式），
  所以 `plain: false` 是对的；要恢复勾选框列 → 改回 `true` 并先人工授权 checkbox 回填规则。
- ⚠ **已知语义坑（未修，等人确认）**：`variants` 的值**同时**当「开关」和
  `useLayoutDetection` 用（`src/wide/s1_ocr.py:385` `for vname, use_layout in variants.items()`
  → `ocr_page_variant(..., bool(use_layout))`；`:364` 又把 falsy 项直接丢掉）。
  所以 **`plain: true` 跑出来的是「第二次 layout」（`use_layout_detection=true`），不是 plain**。
  实测（`run/wide/sp2_2b`，旧 24 列口径）：安吉首页 `plain use_layout=True blocks=6 83.1 s`，24 列命中数
  仍是 9（= 关掉时的 9，**没救回来**）；真 plain（`use_layout=false`）只有旧代码
  （`run/wide/pilot10`：`plain use_layout=False blocks=1`）能表达。
  建议改法：把「启用」与「use_layout」拆成两个键（例：`variants: {layout: {on: true, layout_detection: true}, plain: {on: false, layout_detection: false}}`）。
  在此之前，**别用 `variants.plain: true` 做「plain 全开」对照** —— 那测的不是 plain。

**副变体按需补跑（`pipeline.yaml` v`2026-10-09.6` 起，步②）**：

`plain: false` 不等于「永远不跑 plain」。`ocr.variants_on_demand: true` 时，`s1` 在主变体跑完后
按判据**只对个别页**补跑 plain（`src/wide/s1_ocr.py:on_demand_variants()`）：

| 键（`config/pipeline.yaml:ocr.on_demand`） | 默认 | 含义 |
|---|---|---|
| `variant` | `plain` | 补跑的副变体名 |
| `required_columns` | 10 个首页必填列 | 判据：这些列的 `candidate_labels` 在**首页页集 layout 文本并集**里一个都没出现 → 缺失 |
| `demographic_markers` | 16 个人口学标签 | 挑页用：页内命中数最多的页优先（人口学区在哪个页一目了然） |
| `demographic_min_markers` | `3` | 低于此数的页不补（诊断表续页只有 1 个标记 → 补了白跑） |
| `max_pages` | `1` | 每例最多补几页；每补一页立刻重算缺失，补齐即停 |

- 触发条件全满足才补：有缺失列 **且** 有候选页 marker ≥ `demographic_min_markers`。
- 不触发 = **零额外成本**（实测奉化/桐乡/玉环/长兴 8/8 例不触发；安吉 2/2 例各补 1 页 ≈13–15 s）。
- 一刀切（`plain: true`）实测每例多 4–8 次 OCR、+88.8 s/例（2 例对照，`run/wide/sp2_2b`），
  其中不触发的院区纯白付 → 所以默认按需，不开一刀切。
- provenance：补跑过的页在 `01_ocr/<case>/page_NNNN.json` 写
  `variants.<v>.on_demand: true` 和 `on_demand: {variant, triggered, missing_columns_before, demographic_markers}`；
  `_manifest.json` 汇总 `on_demand_variants`；audit 记 `kind=ocr_output, on_demand=true`（只记哈希/计数）。
- 想做「一刀切」对照臂：`variants.plain: true` + `variants_on_demand: false`（主循环会写
  `blocks_plain`，按需段因此 `reason=no_candidate_pages` 自动空操作）。
  ⚠ 但受上面那个语义坑影响，这样跑出来的 `plain` 是 `use_layout_detection=true`（第二次 layout），
  **不是真 plain**，救不回人口学字段（实测安吉命中数仍 9）。真 plain 一刀切只能改代码；
  其成本参考旧产线 `run/wide/pilot10`（`plain use_layout=False blocks=1`）：
  安吉 p54 14.7 s + p55 12.1 s = 26.8 s/例，奉化 p88 76.1 s + p89 14.4 s = 90.5 s/例。
  按需补跑走的是 `use_layout = bool(variants[<vname>])`（`s1_ocr.py:243`）→ `plain: false` 时
  补的是**真 plain**（`use_layout_detection=false`），实测安吉 p56 补 1 页 12.6–14.9 s 即补齐。

空白/低信息守卫（`config/pipeline.yaml:blank_guard`）：

| 键 | 默认 | 含义 |
|---|---|---|
| `ink_min` | `0.003` | 暗像素占比低于此值 → 判 `blank`，任何情况下都不送 OCR |
| `dark_threshold` | `200` | 灰度低于此值算暗像素 |
| `std_min` | `12.0` | 灰度标准差低于此值 → 判 `low_info` |
| `low_info_ink_min` | `0.010` | 墨迹在 `ink_min` 与此值之间 → 判 `low_info` |
| `requested_page_policy` | `ocr_low_info` | **定位选中的页**（首页/首程）：`ocr_low_info` = 只有 `blank` 才跳过，`low_info` 仍送 OCR 并保留标记；`skip_low_info` = 旧行为（`low_info` 也跳过） |
| `hallucination_watchwords` | 古籍名列表 | `s2` 对页文本命中即标 `watchwords`，`s3` 一律排除该页 |

实测依据（`run/tmp/probe/lowinfo_check.py`，12 例 60 个选中页）：严格档会跳过
`安吉__img20231201_11004386` 0based 250（ink 0.00333 / std 12.68），而该页正是
**首次病程记录标题页**（旧批次 OCR 正文 1302 字）→ 主诉/查体会整页丢失。

`wide.s3_extract`：`--strategies cell,block,coord`（默认三种全跑）、`--no-llm` 只跑确定性策略（对照实验用）。
`wide.s4_normalize`：`--mapping`（默认 `config/field_mapping.yaml`）、`--allow-draft`。

勾选框列（`columns.yaml` 里 `widget: checkbox`：`sex` / `insurance_type` / `admit_route` / `discharge_mode`）
由 `pipeline.yaml:normalize.checkbox` 控制：

> ⚠️ 这 4 列已在 `columns.yaml 2026-10-09.4` 里**删除**（23 列 / 宽表 23 列里都没有勾选框字段）。
> 下面的机制保留是给「将来恢复勾选框列」用的；当前产线不会命中它，`plain` 副变体也已关掉（改动③）。

| 键 | 默认 | 含义 |
|---|---|---|
| `fill` | `null` | `null` = 读到整排选项就判缺失；`number` = 按选项编号回填（**属推断，未实现**，改成 `number` 只会显式报错，不会静默回填） |
| `option_pattern` | `(?:[①-⑩]|\d+\s*[.、])` | 选项编号的判据 |
| `min_options` | `2` | 命中这么多次编号就算「整排选项」而非取值 |
| `box_chars` | `☐☑□▢` | 勾选框字符 |

实测 `奉化__20230728_008`：`性别` 在版面模式是 `☐ 1. 男 2. 女`（选中项丢失），plain 模式是
`离院方式 ① 1. 医嘱离院 2. …`（`①` 疑似选中记号，含义未证实）→ `guard_checkbox()` 判 null，
原文存进 `04_norm/<case>/fields.json` 的 `raw_option_text`（可追，不冒充有值）。

**只在独立批次上试跑**（不动正式批次产物）：把 `00_index/manifest.tsv`、`00_index/page_index.json`
和要用的 `03_extract/<case>/` 复制到 `run/wide/dryrun/`，再 `--batch dryrun` 跑 s4→s5→s6。

`wide.s5_qc` / `wide.s6_table`：只有 `--batch/--case/--case-file/--force/--quiet`。

## 5. 常见故障与排查

**风险①：首页是表格，整页当一个块会丢掉「字段名 ↔ 值」对应**
- 表现：`02_text/.../page_*.json` 里首页只有一个块、`cells` 为空，或 `text` 是一整行糊住的字符串；`性别` 的勾选框在版面模式里渲染成 `☐`（选中项丢失）。
- 处理：首页必须 `useLayoutDetection=true`（`config/pipeline.yaml:ocr.variants.layout`）**并且**跑 `plain` 副变体（选中项在 plain 里以 `①`/数字保留）。`s3` 的 `page_variants()` 会对两种变体各跑一遍策略，`page_text()` 把两种文本都喂 LLM 并标注来源。
- 实测（奉化 008 第 90 页，同一张 PNG）：layout=true 给表格 HTML 但整块合并成 1 个 `colspan` 大格；layout=false 整页一行但保留 `性别 1 男 2. 女`、`入院途径 ①1. 急诊`。两者互补，所以两种都跑。

**风险②：空白页被模型幻觉**
- 表现：一页稀疏扫描，OCR 却输出「欽定四庫全書」这类与病历无关的整段文本，不报错。
- 实测（2026-10-09，`奉化__20230728_008`，定位阶段的页眉条 OCR）：0based 82 页输出
  「識典古籍」、0based 84 页输出「晉書卷八」。两页墨迹**高于** `ink_min`，只按 `ink_min` 卡会放行；
  `classify_page()` 按 `gray_std`/`low_info_ink_min` 判 `low_info`；定位阶段这些页**未选中**，所以没送 OCR
  （见 `00_index/page_index.json` 的 `pages_skipped_kind`）。注意：**定位选中的页**判 `low_info` 时仍然送 OCR（见下面两档），
  否则「首次病程记录」标题页这种稀疏正文页会被整页丢掉（实测 `安吉__img20231201_11004386` 0based 250）。
- 处理（两档）：`s1_ocr.classify_page()` 按墨迹覆盖率（`blank_guard.ink_min=0.003`）与灰度标准差（`std_min=12`）判 `blank`/`low_info`。
  **未选中的页**（定位扫描、`--all-pages`）：`blank` 与 `low_info` 都不送 OCR，页记录 `status=skipped_low_information`。
  **定位选中的页**（首页/首程，`requested_page_policy: ocr_low_info`）：只有 `blank` 不送，`low_info` 仍送 OCR 但保留 `page_kind=low_info`。
  `s2` 再用 `hallucination_watchwords` 兜底；`s3` 只认 `ocr_status=ok 且 page_kind∈{ok,low_info} 且 watchwords 为空` 的页。
- 排查：`01_ocr/<case>/_manifest.json` 的 `pages_low_information` / `pages_low_info_ocr`；日志里 `[!] ... 判为 low_info`、`[!] ... 但是定位选中页`。

**风险③：退化裁剪框静默丢页**
- 表现（旧产线已确认）：`PIL.crop` 遇 `right<=left` 抛异常 → 异常被吞 → `--quiet` 压掉 → 照样 `touch .done`，118 页只出 84 个产物。
- 处理：`common.safe_crop()` 先夹取到图幅再校验，退化就**回退整页**并在 `rec["crops"]` 里记 `degraded`；`--quiet` 只压正常流水，`warn/err` 永远可见；`s1` 每例页数对账，产物数 ≠ 请求页数 → 写 `.incomplete` 且非零退出。
- 排查：`01_ocr/<case>/_manifest.json` 的 `pages_requested` / `pages_written` / `pages_ocr_ok` / `errors`。

**LLM 返回空 content**
- 原因：该模型有思维链，`reasoning_content` 也计入 completion_tokens；`max_tokens` 小了会 `finish_reason=length` 且 `content` 为空。
- 处理：`llm.max_tokens: 16384` + `abort_on_length: true`（`length` 一律记失败，绝不把半截输出当结果）。审计里看 `finish_reason`。
- 现在多了一层：`pipeline.yaml:llm.enable_thinking: false`（v`2026-10-09.7`，步③）→ `s3` 的
  `llm_call()` 透传 vLLM `chat_template_kwargs: {"enable_thinking": false}`（旧产线一直在用，
  见 `src/f3_escalate.py:410`）。实测同 prompt 的 completion_tokens 24630 → 1254、s3 墙钟
  178.3 s → 26.7 s（2 例）。`max_tokens: 16384` 与 `abort_on_length` **保留不动**（兜底仍在）。
  该键**删掉即回到带思维链**（`llm_call` 只在键存在时才发 `chat_template_kwargs`）。
  每个 `03_extract/<case>/{frontpage,first_progress}.json` 记
  `enable_thinking` / `prompt_tokens` / `completion_tokens` / `usage`，审计同步记。
  ⚠ 代价（实测）：安吉 `查体` 由 107 字连续原文（`verbatim=true`）变成 156 字两段拼接
  （`verbatim=false`，`s3_extract.py:238` 判非连续）。命中数没降，但 verbatim 质量降了；
  「verbatim=false → 带思维链重试」的守卫**尚未实现**。

**定位失败 / 首页列为 null**
- 看 `00_index/page_index.json` 里该例的 `frontpage_pages`、`pages_scanned`、`evidence`、`locator_source`。
- 加高重扫：`--strip-frac 0.30`；全页扫：`--no-early-exit`；换关键词：`config/pipeline.yaml:locator.frontpage_keywords`。

**`IsADirectoryError: '.'`**
- 配置项默认空串时 `Path("")` == `Path(".")` 且为真值。代码里已改成先判空串；自己加配置项时注意。

**看含 PII 的产物**
- 用 `run/tmp/peek.py <page.json>`（身份证留前 3 后 2，电话/地址只留长度）。别把 PII 打到 stdout 或日志。
- 输出文件权限 600（`pii.output_file_mode`）。

## 6. 目录结构

```
/home/ubuntu/medrec/
├── config/          columns.yaml · pipeline.yaml · label_vocab.yaml · field_mapping.yaml(第一遍产出)
├── src/wide/        common.py + s1_ocr / s2_preprocess / s3_extract / s4_normalize / s5_qc / s6_table
│   ├── reserved.py  范围外能力的接口桩（lab/用药/诊断、编码映射、检索、CDISC/OMOP、训练验证分离）—— 调用即抛 NotInScope
│   └── tools/       locate_pages.py（页眉条 OCR 定位）· map_pages_from_old.py（旧批次证据 + 墨迹序号映射定位）
│                    pass1_mapping.py（第一遍映射）· build_page_index.py（旧，已弃用）
├── docs/            HOWTO.md · column_mapping.md（第一遍映射给人看的版本）
├── reports/         rebuild-<YYYYMMDD>.md
└── run/wide/<batch>/
    ├── 00_index/    manifest.tsv（例清单）· page_index.json（定位）
    ├── 01_ocr/ 02_text/ 03_extract/ 04_norm/ 05_qc/ 06_table/
    ├── _audit.jsonl 全批次审计
    └── <stage>/<case_id>/.done | .incomplete
run/wide/_locate_cache/  定位缓存（跨批次共享，按 PDF/配置/代码哈希失效；可安全删除 = 强制重扫）
run/tmp/             临时文件与探针脚本（md5 快照、样本清单、对照实验脚本）
```

`case_id` = 来源目录路径把 `/` 换成 `__`，例如 `奉化/20230728_008` → `奉化__20230728_008`。
扫描根**本身就是病例目录**时（`medrec run <病例目录>`，`rel.parent='.'`），`case_id` 和 `来源目录`
都改用「根的上一级 / 根名」补回：`…/安吉/img20231211_08580132` → `case_id=安吉__img20231211_08580132`、
`来源目录=安吉/img20231211_08580132` ⇒ 与全根跑法出**同一个** case_id，`标准化就诊医院`
（来源 = `来源目录` 的第一段 → `pipeline.yaml:hospitals`）也才注入得到（round34 E4 / round39 E4b）。
注意：扫描根停在**院区**那一层（`medrec run …/测试/安吉`）时 `来源目录` 只有病例目录名一段，
第一段不是院区名 ⇒ `标准化就诊医院` 仍为空（round39 实测，见 `workspace/round39/disco_ab.out`）。
