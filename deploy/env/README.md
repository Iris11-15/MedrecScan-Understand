
# env/ —— 建 conda 环境用的清单（install.sh 第 3 步读这里）

| 文件 | 用途 |
|---|---|
| `install-vl-env.txt` | 产线执行器环境（415 条）。`scripts/config.env` 的 `PY_MAIN` |
| `install-ocr-env.txt` | PaddleX/PaddleOCR 环境（115 条）。`PY_OCR` |
| `install-qwen-env.txt` | vLLM 环境（205 条）。起 8110 的那个 |

这三份由 `python pkg_tool.py envlists <wheels/> env/` 从 assets 包里的
`wheels/freeze-<env>.txt`（源机 `pip freeze` 原样，逐文件 sha256 已验）生成，
**只洗掉 8 条**，每条都有理由：

- `aslprep @ git+https://github.com/pennlinc/aslprep.git@...`、
  `neuromaps @ git+https://github.com/netneurolab/neuromaps@...`（vl-env 2 条）
  —— 上一个项目（神经影像）留在同一个 env 里的，`grep -rniE 'aslprep|neuromaps' src/ scripts/ webui/ bin/`
  在源机 medrec 仓库里 **0 命中**。留着 = 目标机多两次 GitHub 克隆，任一失败整条 `pip install` 非零退出
  ⇒ 环境装成半成品，而报错文字指向 pip 而不是「这两个包不该装」。
- `setuptools==` / `wheel==`（三个 env 各 2 条）—— conda 建 env 时自带，freeze 里的旧 pin 会让
  pip 在装到一半时降级自己。

★ **没有洗掉的**：`paddlepaddle-gpu @ https://paddle-whl.cdn.bcebos.com/stable/cu129/...whl#sha256=...`
（vl-env 第 268 行）。这是 vl-env 的 paddle 来源，只有这个 URL 能装对 CUDA 变体；
删掉它会静默装成别的构建（`paddle.version.cuda()` 实测源机 vl-env = cuda 12.9 / ocr-env = cuda 12.6，
两者不同，不能混用同一个 URL）。⚠ 目标机驱动若带不动 cu129 wheel，需要换 URL —— 见 DELIVERY.md §5。

想完全照源机复现（含那 8 条）就用 `wheels/freeze-<env>.txt`：
`conda run -n <env> pip install -r $MEDREC_ROOT/wheels/freeze-<env>.txt`
