# medrec 产线调用网页（round37 · 防呆版）

网页 = 壳，后台 = 命令。本进程**不调 OCR、不调 Strata、不解析 PDF**，只做三件事：
拼命令起进程（一律 `/home/ubuntu/medrec/bin/medrec`）、读产物文件、显示文本（PII 服务端打码）。

## 起

    cd /home/ubuntu/medrec
    /home/ubuntu/anaconda3/envs/vl-env/bin/python /home/ubuntu/medrec/webui/server.py

    # 后台常驻（关掉终端也不死；网页重启不影响正在跑的批次）
    setsid nohup /home/ubuntu/anaconda3/envs/vl-env/bin/python \
      /home/ubuntu/medrec/webui/server.py \
      > /home/ubuntu/medrec/workspace/round37/server_start.log 2>&1 < /dev/null &

浏览器打开 http://127.0.0.1:8090

依赖：**无第三方库**。只用 Python 标准库（`http.server.ThreadingHTTPServer`、
`subprocess`、`zipfile`、`csv`、`json`）。没有 npm / 打包工具 / CDN / 前端框架。
Python 必须是 `/home/ubuntu/anaconda3/envs/vl-env/bin/python`（PATH 里 `fsl` 会抢 `python`）。

## 停

    Ctrl-C                              # 前台跑时
    kill <pid>                          # 启动时打印的 pid；也可 ss -ltnp | grep 8090 找

停网页**不会**停正在跑的批次：跑批是 `setsid` 独立会话（SID=PGID=子进程 pid），
不继承网页的 stdout。要停跑批用 `bash workspace/conc/stop_gpu0_hard.sh` 那类脚本，
本网页不提供任何停服务 / 停跑批按钮（F3）。

## 端口与绑定（F5）

默认 `--host 127.0.0.1 --port 8090`。`--host` 给非环回地址（`0.0.0.0` 等）
⇒ 打印错误并 `exit 2`，不启动。允许值只有 `127.0.0.1` / `localhost` / `::1`。

    /home/ubuntu/anaconda3/envs/vl-env/bin/python webui/server.py --port 8091

## 接口（只读为主，没有危险项）

    GET  /api/status          服务存活(8110/8080) · 负载 · columns 版本 · 草案数 · 锁状态
    GET  /api/batches         历史批次表（行×列 · 命中 · warnings · 稳态 s/例）
    GET  /api/job?batch=B     进度（读文件，不读进程内存）+ 打码后的日志尾部
    GET  /api/check?batch=B   跑 `bin/medrec check B`（只读），回 FAIL/WARN + 原始输出尾部
    GET  /api/stages?batch=B  跑 `bin/medrec stages B`（只读）
    GET  /api/preview?batch=B 宽表预览（PII 打码）
    GET  /api/raw?batch=B&row=N&col=C  本地查看原文：只回浏览器，不落盘；审计记 时间/列名/行号
    GET  /api/wide.csv?batch=B      打码后的 wide.csv 下载
    GET  /api/audit           审计尾巴（排错用）
    POST /api/upload          multipart 流式落地 run/wide/_ingest/.tmp/<upload_id>/
    POST /api/plan            归一预览 + G1–G6 守卫（不搬文件、不起进程）
    POST /api/start           过守卫 → 拿单飞锁 → setsid nohup 起 `bin/medrec run`

**没有**这些接口（F3）：改配置、删批次、启停服务、`--force`、任意命令行参数输入框。
试探 `/api/config`、`/api/delete_batch`、`/api/service`、`/api/force` ⇒ 404。

## 文件落点

    run/wide/_ingest/.tmp/<upload_id>/   上传暂存（.zip 先落 _zips/，过 G5 后解压归一）
    run/wide/_ingest/<batch>/            起跑时 os.replace 落地 = 实际跑批的源根
    run/wide/_ingest/_audit.jsonl        审计（append-only，权限 600，不含 PII 值）
    run/wide/<batch>/_webui/run.log      跑批 stdout（原始，含姓名 ⇒ 是证据，网页显示时才打码）
    run/wide/_health/run.lock            单飞锁 {pid,batch,started_at,input_fingerprint,command}

## 排错

- **「正在跑 X，已运行 N 分钟」（409）**：F1 单飞锁。`cat run/wide/_health/run.lock` 看 pid；
  `curl -s http://127.0.0.1:8090/api/status | grep -A3 lock` 看 `alive`。
  进程已死留下的**陈旧锁**会在下次起跑时被改名留证（`run.lock.stale-<epoch>`），不删除，
  然后自动接管 —— 不用手工清锁。
- **锁只拦网页发起的跑批**：手敲 `bin/medrec run` 不拿锁。页面会在提交时给黄条
  「检测到网页之外在跑的批」（扫 `/proc` 里带 `--batch` 的 `medrec_cli.py run`）。
  两批抢 GPU/Strata 会让稳态 s/例 完全不可比（round36 实测）。
- **8090 被占**：`ss -ltnp | grep 8090` 找 pid。要么换 `--port`，要么 `kill` 掉旧网页进程
  （旧网页进程被 kill 不影响它之前起的跑批）。
- **起批被拒（不是 409）**：看返回 JSON 的 `errors[]`，常见是
  G1 源根形状（必须 `<源根>/<院区>/<病例目录>/*.pdf` 深度 3；选了叶子目录必被拒 ——
  round33 的 `case_id == '.'` 撞名坑）、G2 空间不足（需 输入×3）、G3 服务不活、
  G5 压缩包三检、G6 `--steps` 缺上游哨兵（否则 `run_steps.wait_done_marker()` 每例白等 900 s）。
- **OCR 服务看着活着但很慢 / 在 CPU 上跑**：`bash scripts/10-start-ocr.sh check`
  （判据 = 请求期间 GPU sm；rc≠0 别上产线）。本网页**绝不重启服务**。
- **进度不动**：进度只靠读 `run/wide/<batch>/_pipeline_logs/<case>__<stage>.log` 的 mtime
  和 `_medrec/RUN_META.json` 是否存在（T5）。网页重启不影响读数。
- 例数 < 11 时**稳态 s/例 显示为空**：产线口径是 keep=N−10，样本不够就不猜。

## 已知问题（网页侧兜底，产线侧未修）

`bin/medrec run` 把含患者姓名的 case 目录名原样打进 stdout
（round36 实测 247 处/趟；r37_a 实测 95 处/趟、5 个姓名）。
网页显示日志尾部时过 `pii.scrub_text()` 打码，但
`run/wide/<batch>/_webui/run.log` 与 `_pipeline_logs/` 里的**原始日志未改**（它也是证据）。
G4 会在提交时给黄条提示。彻底修需要改产线（`bin/`、`src/wide/`），本轮禁写。
