# medrec CLI —— 病历识别/结构化流水线

```
PDF/图片 -> 拆页 -> 分层识别 -> 适配 -> 编译 -> 小模型结构化
            pdf2png   recognize   pages   compile   structure
```

串行、逐文件、**可断点续跑**；每个阶段落盘，所以改提示词只需重跑最后一段，
不用重烧识别。

## 一、安装（三步）

```bash
# ① 解压到 ~/medrec/src（所有脚本必须同目录：compile.py 会 import 同目录的三个模块）
mkdir -p ~/medrec/src
cd ~/medrec/src && python3 -m zipfile -e ~/medrec-f3-batch.zip .
chmod +x medrec batch_run.sh

# ② 装成命令（可选，但强烈建议）—— 以后直接敲 medrec
grep -q 'medrec/src' ~/.bashrc || echo 'export PATH="$HOME/medrec/src:$PATH"' >> ~/.bashrc
source ~/.bashrc

# ③ 自检（不跑数据，秒级）
medrec doctor
```

`medrec doctor` 会逐项告诉你缺什么，例如：

```
1) 解释器        ✓ /home/ubuntu/anaconda3/envs/vl-env/bin/python
2) 同目录脚本    ✓ layered.py  ✓ pdf2png.py  ✓ compile.py  ✓ order.py …
3) 依赖          ✓ pypdfium2   ✓ requests    ✓ paddlex / paddleocr
4) 表格提示词补丁 ✓ 已打补丁（命中 1 处）
5) 两个服务      ✓ OCR vLLM  ✓ 结构化 LLM
6) 显存          逐卡列出
== 就绪：可以直接 medrec run <输入目录>
```

**没到「就绪」就不要往下跑** —— 每一步的缺项它都会指出来。

## 二、五个子命令

| 子命令 | 干什么 | 例子 |
|---|---|---|
| `doctor` | 查环境与前置文件（不跑数据） | `medrec doctor` |
| `env` | 打印当前解释器/端口/路径 | `medrec env` |
| `pdf2png` | PDF → 逐页 PNG | `medrec pdf2png pdfs/ --out $W/pages --max-pages 50` |
| `recognize` | 分层识别（自动带 vLLM 参数与显存上限） | `medrec recognize $W/pages/病案号 --out $W/raw/病案号` |
| `pages` | 适配成 F2 页数组（默认 `--merge`） | `medrec pages $W/raw/病案号 --out $W/json/病案号` |
| `compile` | F2 编译 | `medrec compile $W/json/病案号/pages.json --out $W/compiled/病案号` |
| `structure` | F3 结构化 + 清理无关 | `medrec structure $W/compiled/病案号 --dry-run` |
| `run` | 五步一条命令 + 续跑 | `medrec run /path/to/pdfs` |

参数**原样透传**给底层脚本（所以 `medrec structure --help` 就是 `structure.py --help`）。

`recognize` 帮你补的默认值（可用同名参数覆盖）：

```
--vl-backend vllm --vl-url http://127.0.0.1:8110 --no-vl-autostart --vl-limit 80
FLAGS_initial_gpu_memory_in_mb=4096  FLAGS_fraction_of_gpu_memory_to_use=0.45
```

## 三、典型用法

```bash
W=~/medrec/jobs/b1
PY_DIR=/path/to/pdfs              # 换成你的真实路径

# 一键（推荐先这个）
medrec run "$PY_DIR"

# 只跑一份、只取前 3 页（试参数用）
medrec pdf2png "$PY_DIR"/某一份.pdf --out $W/pages --max-pages 3
medrec recognize $W/pages/某一份 --out $W/raw/某一份
medrec pages     $W/raw/某一份 --out $W/json/某一份
medrec compile   $W/json/某一份/pages.json --out $W/compiled/某一份
medrec structure $W/compiled/某一份 --dry-run          # 先看不发请求
medrec structure $W/compiled/某一份                     # 真跑

# 改了提示词/schema 之后：只重跑结构化
medrec structure $W/compiled/某一份 --schema my_schema.json --system prompt.txt --resume
```

产物布局（`WORK` 默认 `~/medrec/jobs/batch_<时间戳>/`）：

```
pages/<名字>/page_0001.png        阶段1 拆页
raw/<名字>/*_layered.json         阶段2 识别（+ .done 标记）
json/<名字>/pages.json            阶段3 页数组
compiled/<名字>/                  阶段4 五件套（compiled.md/json、sections/tables.json）
compiled/<名字>/structured.json   阶段5 结构化产物 ← 存在即视为已完成
```

## 四、可覆盖的配置

```bash
MEDREC_PY=/path/to/python    # 换解释器（默认 vl-env）
VLLM_URL=http://127.0.0.1:8110
LLM_URL=http://127.0.0.1:8101
LLM_MODEL=qwen-small
VL_LIMIT=80
WORK=/home/ubuntu/medrec/jobs/b1
SKIP_STAGE1=1                # 输入已是图片，跳过拆页（只对 run 有效）
```

## 五、三个必须知道的点

**1. `structure.py` 内置的 schema / 提示词只是占位默认。**
形状是 `{相关, 类型, 理由, 条目[{字段,值,原文}]}`。上线前换成你们的目标字段表：
`medrec structure <目录> --schema my_schema.json --system prompt.txt`。

**2. 表格不会漏。**
`compiled.json` 里表格块的 `content` 是空的（矩阵在 `tables.json`），`structure.py`
会把 `header` + `rows` 拼成网格一起送，并附上 `abnormal`（异常项）。

**3. 质量闸门。**
`compile_report.json` 的 `alerts` 里有「不要进入 F3」（缺失率 > 5%）时，
`structure.py` 默认**拒绝执行**（exit 3）；确要认真跑加 `--force`。

## 六、验证清单

| 阶段 | 怎么确认对了 |
|---|---|
| 拆页 | `pages/<名字>/` 里张数 = PDF 页数 |
| 识别 | `raw/<名字>/*_layered.json` 存在，`summary.blocks` > 0 |
| 适配 | `pages.json` 是**数组**，每页有 `parsing_res_list` / `width` / `height` |
| 编译 | `compiled/` 五件套齐全，`missing_ratio` 低于阈值 |
| 结构化 | `structured.json` 的 `stats.failed == 0`，`merged.条目` 有内容 |

> 续跑判据用 `stats.failed == 0` 而不是"文件存在"：全失败时 `structure.py` 会删掉
> 自己的输出，但**部分失败**的文件仍然存在。

## 七、已知限制 / 未验证项

- **未在真机跑过完整批次**：`batch_run.sh` 与 `medrec` 只做过 `bash -n` 与分支冒烟；
  `pdf2png.py` 的真实拆页（需 pypdfium2）与真实 LLM 调用还没在服务器上验过。
- **已验证**：`to_pages.py` 适配后跑真 `compile.py`，结果与参考产物逐项一致
  （4 栏、跨栏块 [0,2,3]、缺失 3/9.7%、汉字 2212、`compiled.md` 7555 B 完全相同）。
- `layered.py` 产物名用 `f.stem`：同名文件会互相覆盖 → 流水线按 `<名字>/` 分目录规避。
- `structure.py` 的 `response_format: json_schema` 在这台机器的 vLLM 0.30 上是否被接受
  **未验证**；报 400 就用 `--no-json-schema` 退回提示词约束。
- 识别侧建议保留 `FLAGS_fraction_of_gpu_memory_to_use=0.45`（卡 2 上还有 6.3 G 的 OCR
  vLLM）。没有 webui 进程后可以上调到 0.55~0.65，但先按 0.45 跑通再调。
