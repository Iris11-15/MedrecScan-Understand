# medrec F5g 升级包 · 从「长表」到「495 列宽表」（累积取代 F5 / F5b / F5c / F5d / F5e / F5f）

> 本包把你在 `数据条目.xlsx` 里定义的 **499 列（去重后 495 列）目标表** 落成可执行的东西。

---

## 0. 这一版解决什么

| 问题（你提的） | 这一版怎么做 |
|---|---|
| 输出是长表不是宽表 | 新增归并层 `f4_table.py`：长表 → **一患者一行 × 495 列** |
| `姓名/性别/年龄/住院号` 重复 8 次 | 归并层按你同意的优先级取唯一值（入院记录 > 首次病程 > 日常病程 > 页眉页脚） |
| 无医院名称 | 495 列里确实没有这一列 —— 要看第一张表能不能拿到；拿不到就是真的没有（你说不新增列） |
| 诊断重复、容易误判 | `诊断` 拆成 `诊断-入院 / 诊断-既往 / 诊断-出院` 三张表；`出院其它诊断` 这类多值列**拼接而非丢成一条** |
| 应该输出是/否 | 93 个「是否/既往/出院是否」列统一走三态归一（`否认/无/阴性` → 否，`有/阳性` → 是） |
| 化验要正常值范围与时间 | schema 拆出 `单位 / 参考范围 / 异常 / 时间` 四个**可选**字段 |
| 检查描述要同时提取 | schema 拆出 `所见` 与 `结论` |
| 长期医嘱/出院带药要剂量 | schema 拆出 `剂量 / 频次 / 途径 / 时间` |
| 主诉与查体分开 | `类别` enum 里加 `主诉`；`查体` 独立成表 |
| 抽出来的东西无处安放 | **v3 提示词把 495 个列名提前交给模型**，要求 `项目` 原样选用 |

---

## 1. 停服（需要停的只有批处理）

```bash
pgrep -af 'batch_run.sh|structure.py|layered.py|f4_table.py'
```

有输出就在**它自己的终端**按 `Ctrl-C`。

| 端口 | 是什么 | 动不动 |
|---|---|---|
| `8102` | 你刚起的量化模型（试跑用） | 见第 2 节 |
| `8101` | F3 小模型 | **不动** |
| `8110` | PaddleOCR-VL | **绝对不碰** |
| `8000/8001` | v14 两个副本 | **绝对不碰** |

```bash
# ★ 永远不要执行：会把 v14 和 OCR 一起杀掉
#   pkill -f 'vllm serve'
```

---

## 2. 把量化模型正式换到 8101（可选，但建议）

如果 8102 试跑通过，就正式换：

```bash
kill 454348                                    # 停掉占 GPU3 的那个 bf16
kill <8102 那个 pid>                            # 停掉试跑用的
# 然后用 F4 笔记里那条命令，端口改回 8101、CUDA_VISIBLE_DEVICES=2
```

**换完必须验三条**：

```bash
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader   # GPU2 有占用、GPU3 归零
curl -s -m 5 http://127.0.0.1:8101/v1/models                     # 仍回 qwen-small
curl -s -m 60 http://127.0.0.1:8101/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-small","messages":[{"role":"user","content":"回复OK"}],"max_tokens":32}'
#   必须无 <think>、finish_reason: stop
```

---

## 3. 解压 + 安装（先全量校验，再写入）

```bash
mkdir -p ~/medrec-f5 && cd ~/medrec-f5
unzip -o ~/medrec-f5-20260923.zip
bash install.sh
```

期望结尾：

```
  ✓ 8 个文件全部就位且 sha256 一致
== 完成：新增/替换 N 个，跳过 M 个，备份 K 份
```

### 自检

```bash
PY=/home/ubuntu/anaconda3/envs/vl-env/bin/python
"$PY" -m py_compile ~/medrec/src/f4_table.py ~/medrec/src/structure.py \
      ~/medrec/src/to_xlsx.py && echo PY-SYNTAX-OK
bash -n ~/medrec/src/batch_run.sh && echo BASH-SYNTAX-OK
"$PY" -m json.tool ~/medrec/src/f3_schema.json > /dev/null && echo SCHEMA-OK
"$PY" -m json.tool ~/medrec/src/f4_columns.json > /dev/null && echo COLUMNS-OK
"$PY" -m json.tool ~/medrec/src/f4_alias.json > /dev/null && echo ALIAS-OK
grep -c '类别' ~/medrec/src/f3_system.txt          # 应 > 0
wc -c ~/medrec/src/f3_system.txt                   # 应约 19500
```

---

## 4. 单文件端到端（别跳）

**先 dry-run，确认三行来源配套**：

```bash
PC=~/medrec/jobs/batch_0923_1328/compiled/202424089790叶文达
"$PY" ~/medrec/src/structure.py "$PC" --dry-run --force | head -8
```

必须是：

```
文本来源 : sections_text.json（F2 渲染好的章节文本）
提示词   : /home/ubuntu/medrec/src/f3_system.txt（8966 字，含页眉黑名单）
schema   : /home/ubuntu/medrec/src/f3_schema.json
```

**再真跑**（v3 提示词里带了 495 个列名，system 约 3500 token，靠前缀缓存摊薄）：

```bash
"$PY" ~/medrec/src/structure.py "$PC" \
  --url http://127.0.0.1:8101 --model qwen-small \
  --schema ~/medrec/src/f3_schema.json \
  --system ~/medrec/src/f3_system.txt \
  --max-tokens 4096 --force
```

**再归并成宽表**：

```bash
"$PY" ~/medrec/src/f4_table.py "$PC" --out /tmp/f5out
ls -l /tmp/f5out/
```

打开 `202424089790.xlsx`，重点看三张表：

| 表 | 看什么 |
|---|---|
| 各宽表 | **填了多少格**（越接近真实越多越好） |
| `待归并` | 有多少条没对上目标列。**这个数字就是 v3 提示词的合规率度量** |
| `出处校验` | 有没有 `✗`（模型编造块引用） |

**`待归并` 的用法**：表里每一行都是「模型写了一个清单外的项目名」。如果某个名字反复出现且确实该进某列，往 `~/medrec/src/f4_alias.json` 的 `映射` 里加一行：

```json
"住院号": "病案号",
```

---

## 5. 整批

```bash
W=~/medrec/jobs/batch_0923_1328
WORK="$W" medrec run ~/test
```

批次末尾会自动跑两趟导出，产物都在 `$W/xlsx/`：

```
$W/xlsx/<住院号>.xlsx      宽表（495 列）+ 待归并/缺失/丢弃
$W/xlsx/汇总.xlsx          整批宽表，行 = 患者，多「住院号/姓名」两列
```

---

## 6. 回滚

```bash
ls -l ~/medrec/src/*.bak.*
cp -a ~/medrec/src/f3_system.txt.bak.XXXX ~/medrec/src/f3_system.txt
```

回滚不需要重启任何服务。

---

## 附录 · 包内 9 个文件

| 文件 | 作用 |
|---|---|
| `f3_system.txt` | **v3 提示词**：内嵌 495 个目标列名 + 三态/拆分/出处规则 + **页眉黑名单**。8966 字 ≈ 3700 token（走前缀缓存，每患者只付一次） |
| `f3_schema.json` | **v3 schema**：`类别` enum 18 项；`单位/参考范围/异常/剂量/频次/途径/时间/所见/结论` 全部**可选**（只输出有值的，省输出 token） |
| `f4_columns.json` | 495 个目标列的分组模板（16 张表） |
| `f4_alias.json` | `项目名 → 目标列名` 别名表（65 条种子）+ 血压拆分规则 |
| `f4_table.py` | **归并层**，长表 → 宽表。分层解析：血压拆分 → 别名 → 精确 → 分组 → 核心名 → 模糊（宁可空着，绝不猜）；产出 per-patient xlsx 与 汇总.xlsx |
| `f4_check.py` | **本包新增的成员**：验收检查（纯 Python、零模型）。引用完整性 / 值可回查 / 填充率 / 待归并 / 待确认 → `验收报告.md` + `抽样回查.xlsx` + `未命中明细.xlsx` |
| `batch_run.sh` | 阶段 5 后**逐文件**出宽表到 `$WORK/xlsx/`；批次末尾追加 `汇总.xlsx`，长表视图另走 `$WORK/xlsx_long/` |
| `to_xlsx.py` | 长表视图导出（便于逐条复核）；新增「旧 schema 无类别」告警 |
| `structure.py` | 去重键改为与 schema 无关；提示词/schema 默认值与数据源**成对选择** |

**不在包里、不会被改动**：`medrec`、`compile.py`、`layered.py`、`to_pages.py`、`pdf2png.py`、`vl_service.py`。

## 附录 · 已知遗留

1. **`待归并` 会长期存在** —— 它是工单不是缺陷。v3 提示词要求模型只用清单里的列名，但模型仍会偶尔改写；每跑一批看一次这张表，补几条别名，下一批就更干净。
2. **`数据条目.xlsx` 里的 4 个重复列名**（`病案号`/`入院时间（住院时间）`/`年龄`/`性别` 各出现 2 次）已去重，495 列。
3. **`主诉`、`其他` 没有对应目标列**（499 列里没有），所以它们只出现在长表视图里。
4. **量化后的质量尚未用数据对照**。建议拿本次单文件结果与之前 bf16 的 `202424089790` 比：`出处校验` 的 ✗ 数量、`待归并` 条数、类别错分。


---

# ★ F5b 与 F5c 的改动（你一个都没装，所以这里累积列出）

## F5b · 输出结构与一个静默丢数据的 bug

**① `Sheet1` 照抄你的模板 + 16 张分组表，两套都给**

`f4_table.py` 新增 `--layout`（默认 `both`）：

| 值 | 产出 |
|---|---|
| `both`（默认） | `Sheet1`（495 列，**严格按 `数据条目.xlsx` 的原始列顺序**）+ 16 张分组表 |
| `flat` | 只有 `Sheet1` |
| `sheets` | 只有 16 张分组表 |

去掉的 4 个重名列（`病案号` / `入院时间（住院时间）` / `年龄` / `性别` 各在模板里出现 2 次）
**只保留首次出现**，所以是 495 列而不是 499。
`汇总.xlsx` 的 `Sheet1` 最前面多两列 `住院号` / `姓名`（整批平表必须能认出是谁）。

**② 修了一个会让条目凭空消失的 bug**

`f4_alias.json` 里若某条映射指向**目标表里不存在的列名**，条目会落进一个谁都不输出的 key ——
既不在 `Sheet1` 也不在任何分组表，**却仍被算作「已填」**。

现在 `f4_table.py` 会校验别名目标，不合法的忽略并告警，条目转而进入「待归并」。
实测同一份数据：修复前「内部 58 列 / Sheet1 55 列」，修复后两边都是 55 列。

---

## F5c · 阶段 5 全部失败的真因：输出被截断

`batch_0924_0904` 那次，阶段 5 **每条都报
`ParseError: Expecting ',' delimiter ... （finish_reason=length）`**。
`finish_reason=length` 说明**不是模型乱写，是输出被 `--max-tokens 4096` 砍断**。

**为什么现在才爆**：v3 把 `项目` 换成了 495 个目标列名，而这些列名本身很长
（`住院阿司匹林使用剂量(mg)`、`本次发作前7天内是否有1次早期发作(仅TIA需要填写)`），
每条条目的输出 token 涨了好几倍。5000 字符的块实测输出超过 4096。

**① `batch_run.sh`：新增 `MAX_CHARS`，默认 3000**（原来写死 5000）

```
6300(system) + 3006(最大块) + 4096(输出) = 13402 < 16384   → 余约 3000 token
```

代价：块数 21 → 30。但 system 那 6300 token 走前缀缓存，多出来的主要是各块自己的 prefill。
**可用环境变量覆盖，不用改文件**：

```bash
MAX_CHARS=2500 MAX_TOKENS=6000 WORK=$W medrec run ~/test
```

**② `structure.py`：单个块超过 `max_chars` 时按行再切**

`sections_text.json` 里一个片段 = 一个块。若某个块本身就比 `max_chars` 长（一整张大表就是），
不切的话它会独占一个超长 chunk，输出必然被截断。新增 `_split_long()`：
优先按行切，单行仍超限就硬切。实测 5029 字符 -> 20 段（最长 251）；单行 1000 字符 -> 4x300。

**③ `f3_system.txt`：补了一条防重复规则**

病历每一页页眉都重复患者信息，模型会为每一处都生成条目（`姓名`/`性别`/`年龄`/`病案号`...）
—— **归并层最后虽然去重，但 token 已经烧掉了**。新增规则：同一章内已写过的页眉信息，
后面再遇到就跳过。

**④ 修了一句会误导的提示**

`batch_run.sh` 原来**无条件**打印「诊断留在这里：structured_failed.json」，
但那个文件**只在全部块都失败时才生成**。这次是部分失败，所以你按提示去找得到
`FileNotFoundError`。现在会判断存在性，区分「全部失败 / 部分失败」两种指向。

---

# 装完这么验证

```bash
W=~/medrec/jobs/batch_0924_0904      # 或你新建的批次
CUDA_VISIBLE_DEVICES=2 MAX_CHARS=3000 WORK=$W medrec run <只放一个文件的目录>

# 阶段 5 的逐块结果
grep -cE '解析失败' $W/batch.log                       # 期望 0
grep -nE '5/6|\[ok\]|\[!\]' $W/batch.log | tail -5

# 宽表结构
PY=/home/ubuntu/anaconda3/envs/vl-env/bin/python
"$PY" - <<'EOF'
import openpyxl, glob
f = sorted(glob.glob('/home/ubuntu/medrec/jobs/*/xlsx/*.xlsx'))[-1]
wb = openpyxl.load_workbook(f)
print('sheet:', wb.sheetnames)
ws = wb['Sheet1']
print('Sheet1 列数（应为 495）:', ws.max_column)
print('前 8 列:', [c.value for c in ws[1][:8]])
print('填入列数:', sum(1 for c in ws[2] if c.value))
EOF
```

**若仍有 `finish_reason=length`** —— 单块输出还是超预算，按顺序调（都不用改文件）：

1. `MAX_CHARS=2500`
2. `MAX_TOKENS=6000`（当前 server 的 `--max-model-len 16384` 允许到约 7100）
3. 两个一起

**若报 `HTTP 400 ... maximum context length`** —— 反过来了，是请求太长：
把 `MAX_TOKENS` 降到 3000，或把 server 的 `--max-model-len` 提到 20480
（GPU3 上 F3 独占，KV 约 11 GB，装得下）。

---

## F5e · 逐文件立刻出大表（原来是批次末尾才出）「大表」永远看不到的覆盖 bug

`batch_run.sh` 里有**三处写 xlsx，文件名完全相同**（都是 `<住院号>.xlsx`），后写的盖掉先写的：

| 位置 | 谁写的 | 原来写到 | 内容 |
|---|---|---|---|
| 逐文件阶段 6 | `to_xlsx.py` | `$WORK/xlsx/` | 长表视图（`项目|值|出处`） |
| 批次末尾 ① | `f4_table.py` | `$WORK/xlsx/` | **宽表（495 列，主交付物）** |
| 批次末尾 ② | `to_xlsx.py` | `$WORK/xlsx/` | 长表视图 ← **把宽表盖掉了** |

所以批次跑完，`$WORK/xlsx/` 里躺的是**长表**，大表已经被覆盖 ——
这就是「跑起来了但找不到大表」的原因。

**修法：两个目录分开。**

| 目录 | 内容 |
|---|---|
| `$WORK/xlsx/` | **大表（主交付物）**：`<住院号>.xlsx`（`Sheet1` 495 列 + 16 张分组表）、`汇总.xlsx` |
| `$WORK/xlsx_long/` | 长表视图（复核用）+ `待归并` / `缺失` / `丢弃` |

**已经跑完的批次不用重跑** —— 大表可随时从 `structured.json` 重新归并（秒级，零模型）：

```bash
PY=/home/ubuntu/anaconda3/envs/vl-env/bin/python
W=~/medrec/jobs/batch_0924_0904
"$PY" ~/medrec/src/f4_table.py "$W" --out "$W/xlsx"
ls -lh "$W/xlsx/"
```

### 装这个包之前必须确认批次已经跑完

**`batch_run.sh` 正在被 bash 执行时不要替换它。** bash 是按字节偏移增量读取脚本的，
中途替换会让它读到错位内容、行为不可预期。

```bash
pgrep -af 'batch_run.sh'     # 有输出 = 还在跑，先等它结束
```


---

# ★ F5d / F5f / F5g 的改动（你一个都没装，所以这里继续累积）

## F5d · `f4_table.py` 的模糊层：别名表挡不住的那些

`f4_alias.json` 只有 62 条别名，模型每批会写出上千种自由名字，差一点点就落进「待归并」。
`f4_table.py` 把映射拆成了七层，**一层没中就往下走一层**：

| 层 | 规则 | 命中后 |
|---|---|---|
| ① 血压拆分 | `124/76` | `收缩压（mmHg）` + `舒张压（mmHg）` |
| ② 别名表 | 精确 | 目标列 |
| ③ 精确 | 归一化后同名 | 目标列 |
| ④ 分组名 | 与 16 张表同名 | 该表 |
| ⑤ 核心名 | 去掉单位/括号后**唯一**命中 | 目标列 |
| ⑥ 前缀/包含 | **唯一**候选 | 目标列（相似度 1.00） |
| ⑥′ 前缀/包含 | **多个**候选 | → `待确认`（0.99，候选全部列出） |
| ⑦ difflib | ≥ 0.82 | 目标列 |

**核心设计：模糊层宁可空着，也绝不猜。** 候选多于一个就写进「待确认」，不填。
填错一个单位（`肌酐` 填进 `肌酐(mg/dL)` 而原文是 μmol/L）比空着危险得多。

实测：62 个真实条目名，模糊层**自动映射正确 37 个，错 0 个**。

> 修过一个把模糊层废掉的 bug：粗筛用了**完整列名**，但 difflib 的
> `ratio = 2M/(len(a)+len(b))` 对「短项 vs 长列名」有结构性上限 ——
> `体温` vs `体温.(℃)` 最多只能得 `2×2/(2+6)=0.5`，全被 0.82 的闸门滤掉了。
> 改成用 `core(名字)`（剥掉单位和括号）粗筛后，命中从 12/62 涨到 37/62。

---

## F5f · 验收检查：`f4_check.py`（零模型，秒级）

批次跑完**先看这个**，不要靠肉眼翻 xlsx。

```bash
PY=/home/ubuntu/anaconda3/envs/vl-env/bin/python
W=~/medrec/jobs/batch_0924_0932
"$PY" ~/medrec/src/f4_check.py "$W" --out "$W/check"
```

| 检查 | 看什么 | 出问题意味着 |
|---|---|---|
| ① 引用完整性 | 条目里的 `出处`（`p12-b4#白细胞`）在 `sections_text.json` 里存不存在 | 模型编造了页码/块号 —— 必须看 |
| ② 值可回查 | `值` 在 `出处` 指向的源块原文里能不能找到 | **比例下降 = 模型开始改写原文**，最危险的信号 |
| ③ 填充率 | 每患者填进 495 列里的多少列 | 低了说明提示词/切块有问题 |
| ④ 待归并 | 哪些项目名没映射上目标列 | **别名表的工单**，不是缺陷 |
| ⑤ 待确认 | 模糊层给了候选但没敢填 | 人工拍板一次，补进 `f4_alias.json` 就自动填 |

输出在 `--out` 目录：`验收报告.md`、`抽样回查.xlsx`、`未命中明细.xlsx`。

### 这一版的基线（16 个患者、13968 条条目）

| 指标 | 值 |
|---|---|
| 引用命中 | **98.5%** |
| 值可回查 | **64.3%**（描述类被概括属正常，看趋势不看单条） |
| 平均填入 | **231.4 / 495 列（46.7%）** |
| 待归并 | 1796 种 |
| 待确认 | 96 种 |

> 修过一个严重的 key-space bug：`f4_check.py` 里**自己另写了一份** `norm()`，
> 那份会把括号也删掉（`入院诊断（初步诊断）` → `入院诊断初步诊断`），
> 与 `f4_table.py` 的键空间不一致 —— 结果**连目标列名本身都被报成「没映射上」**，
> 「待归并」虚高到 2045 种。
> 现在 `f4_check.py` **只调用 `cols.resolve()`**，不再自己实现映射。
> 实测：旧逻辑漏判 **9/9** 个目标列名，新逻辑漏判 **0/9**。

---

## F5g · 省输出 token：页眉黑名单

**输出 token 是整条流水线的瓶颈**（实测约 55 token/s），不是模型速度。
而病历**每一页页眉都重复**患者信息，模型会为每一处都生成条目。

上一批「待归并」的前几名：

| 项目名 | 出现次数 | 在 495 列里？ |
|---|---|---|
| 病区 | 183 | 否 |
| 住院号 | 145 | 否 |
| 床号 | 110 | 否 |
| 出生日期 | 86 | 否 |
| 床位 | 50 | 否 |
| 出生年月 | 44 | 否 |

**618 条** —— 模型花输出 token 写出来，归并层找不到列，最后全落进「待归并」。
纯粹白烧 token，还把真正的工单淹了。

**修法**：`f3_system.txt` 新增一段黑名单，明确列出页眉里**不要输出**的字段：

```
病区 / 住院号 / 床号 / 床位 / 出生日期 / 出生年月 / 职业 / 国籍 / 婚姻
现地址 / 户口地址 / 邮编 / 联系人 / 联系电话
```

（这些列本来就不在 495 列模板里，不输出**不丢任何交付物要的东西**。）

**`f4_check.py` 同步新增 `未命中明细.xlsx`**：那 35% 「值找不到」的条目不再埋在
4000 条抽样里 —— 每患者最多留 400 条、整批最多导 300 条，含 `项目 / 值 / 出处 / 源块原文`，
一眼能分三类：① 值被规范化（`2024年9月14日` → `2024-09-14`，正常）
② 值被概括（长描述压成要点，可接受）③ **值里有原文没有的内容（真问题）**。

---

# 装完照着跑一遍

```bash
# 0) 确认没有批处理正在跑（batch_run.sh 正在被 bash 执行时不要替换它）
pgrep -af 'batch_run.sh'

# 1) 安装
cd ~/medrec/pkg_f5g && bash install.sh ~/medrec/src

# 2) 重跑上一批里失败的块（--resume 只补失败的，成功的读盘不重算）
W=~/medrec/jobs/batch_0924_0932
cd ~/medrec/src
CUDA_VISIBLE_DEVICES=3 MAX_TOKENS=6000 WORK=$W bash batch_run.sh <只放一个文件的目录>

# 3) 验收
PY=/home/ubuntu/anaconda3/envs/vl-env/bin/python
"$PY" ~/medrec/src/f4_check.py "$W" --out "$W/check"
less "$W/check/验收报告.md"
```

**为什么 `MAX_TOKENS=6000`**：`maxItems: 80` × 平均约 55 token/条 ≈ 4400，
超过原来的 `--max-tokens 4096` —— 每个密块都会以
`ParseError: Expecting ',' delimiter ... （finish_reason=length）` 收场。
这是**输出被砍断**，不是模型乱写。`finish_reason=length` 出现一次就该调大。

预算：`6300(system) + 3006(最大块) + 6000(输出) = 15306 < 16384`，余约 1000。
还报 `length` 就 `MAX_CHARS=2500`；反过来报 `maximum context length` 就把
`MAX_TOKENS` 降到 5000，或把 8101 的 `--max-model-len` 提到 20480（GPU3 上 F3 独占，KV 约 11 GB，装得下）。

### 已经跑完的批次不用重跑

宽表和验收都是**零模型、秒级**的，可以随时从 `structured.json` 重算：

```bash
"$PY" ~/medrec/src/f4_table.py "$W" --out "$W/xlsx"
"$PY" ~/medrec/src/f4_check.py "$W" --out "$W/check"
```
