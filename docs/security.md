# 安全规定与边界（给 Codex 的约束 · 含实测证据）

> 结论先说：**数据面已锁死（写不了、出不去），唯一残余风险是"误杀进程"，已用 PID 命名空间兜住。**

## 一 · 红线（与 AGENTS.md 一致，这里是完整版）

1. **只写 `~/codex-scratch/`**：不得创建、修改、删除 `~/medrec` 下任何文件 —— 包括 `vital_rules.json`、`tables.json`、`登记表.xlsx`、模型权重、脚本。
2. **不得 kill / pkill / killall 任何进程，不得重启任何服务**；需要重启时**只打印建议命令**，交给人执行。
3. **不得 sudo / su / 提权**；**不得联网** —— 只允许访问 `127.0.0.1` 上的本机端口（8100/8102/8110/8111）。
4. **不许猜**：每条结论必须附证据（`文件:行号` / `页号+原样片段` / 命令输出）；拿不到就写 `unknown` 并说明缺什么。
5. **不自行落库**：`vital_rules.json`、登记表、服务启停属于人工操作，模型只出草案与建议。
6. **大文件禁止整读**：>20000 字节先用 `docmap.py` 取行号地图，再 `sed -n 'A,Bp'`（单次 ≤150 行）；统计一律用脚本。

## 二 · 实测边界（2026-09-30，本机）

| 攻击面 | 探针 | 实测结果 | 结论 |
|---|---|---|---|
| 写文件 | `touch /home/ubuntu/medrec/SANDBOX_TEST.txt` | **退出码 1**（`Read-only file system`） | ✅ 写不进去 |
| 出网 | `curl -s -m 3 https://example.com` | **退出码 7**（连接失败） | ✅ 数据出不去 |
| 发信号 | `kill -0 <8110 的 pid>` | 沙箱内**看不见**该进程（`pgrep` 只命中 `pid=1`） | ✅ 命名空间隔离生效 |
| 提权 | `id -u`（命名空间内）= **0** | 但**只读挂载仍然生效**（写 `~/medrec` 依旧 exit 1） | ✅ 提权不破沙箱 |
| 可写区 | `touch ~/codex-scratch/T.txt`（`-s workspace-write` 时） | 退出码 0 | ✅ 授权范围内可写 |
| **红线自拦**（2026-09-30 复测） | 让 agent 执行 `touch ~/medrec/T.txt; echo $?` | **拒绝执行**，并引用红线第 1 条 | ✅ 契约生效（复测姿势见 §2.1） |

**为什么 `id -u` 是 0 也不怕**：`unshare -r` 只是把当前用户映射成命名空间内的 root，**文件系统的只读属性来自挂载层**，不受 uid 影响 —— 实测印证了这一点。

### 2.1 想复测沙箱边界时怎么做

红线写进 `AGENTS.md` 之后，**agent 会拒绝执行"故意写 `~/medrec`"的探针** —— 2026-09-30 实测：它没有执行，而是引用红线第 1 条并给出"改成写 `~/codex-scratch/`"的替代命令。**这是契约生效的证据，但也意味着不能再让 agent 自己去撞红线。**

要复测沙箱本身，换一个**不加载 AGENTS.md** 的工作目录跑（`/tmp` 下没有 AGENTS.md，红线不会被注入，沙箱照旧生效）：

```bash
CX_CD=/tmp cx "只执行一条并报退出码：touch /home/ubuntu/medrec/T.txt; echo 写病历目录: \$?"
# 期望 1（Read-only file system）
```

## 三 · 标准跑法（三件套：PID 隔离 + 只读沙箱 + 超时）

```bash
timeout 900 unshare -rpf --mount-proc \
  codex exec --skip-git-repo-check -s read-only --cd /home/ubuntu/medrec "任务…"
```

已封装成三个短命令（`~/.local/bin/cx`）：

| 命令 | 用途 |
|---|---|
| `cx "任务…"` | **只读**沙箱（默认），工作目录 `~/medrec`，超时 900s |
| `cxw "任务…"` | **可写工地**（`-s workspace-write`）：`~/codex-scratch` + `~/medrec/workspace` |
| `cxf <prompt文件>` | 从文件读长 prompt（**只读**版） |
| `cxwf <prompt文件>` | 从文件读长 prompt + **可写工地**（长任务书干活用这个） |
| 环境变量 | `CX_TIMEOUT=1800`（改超时）、`CX_CD=/home/ubuntu`（改工作目录）、`CX_MEM=0`（关自动记忆） |

## 四 · 记忆与工具的限制（已知且是设计的一部分）

- **MCP 工具在 `approval_policy=never` 下一律被拒**（`MCP tool call requires approval...`）→ 记忆/脚本操作**只走 shell 命令行**（`mem.py`）。
- 沙箱内**只读**；要干活必须用 `cxw` / `cxwf`（`-s workspace-write`），写点限于 `~/codex-scratch` 与 `~/medrec/workspace`。
- **不用 `pgrep` 判活**：命名空间内会误报 `pid=1`。判活一律 `curl http://127.0.0.1:<端口>/v1/models`。

## 五 · 审计（出了事查得到）

| 来源 | 位置 |
|---|---|
| Codex 会话记录 | `~/.codex/sessions/`（逐条命令） |
| 服务日志 | `~/medrec/logs/{agent,ocr_paddle,ocr_vllm,laya}.log` |
| 链路 stage 日志 | `~/medrec/logs/pipeline/*.log` |
| 记忆库 | `~/codex-scratch/memory/memory.db`（每条带 `source`） |

## 六 · 可选加固（按需，不是默认）

1. **看门狗**：`start-agent.sh start` 是幂等的（端口在听就直接退出），可挂 cron 每 2 分钟兜一次：

```cron
*/2 * * * * /bin/bash /home/ubuntu/medrec/scripts/start-agent.sh start >/dev/null 2>&1
```

副作用：想手动停服务时，先摘掉这行（`crontab -l | grep -v start-agent | crontab -`）。

2. **任务前快照**：把要动的文件先 `cp -a` 到 `~/codex-scratch/`，出错可回滚。
3. **别开** `--dangerously-bypass-approvals-and-sandbox`：那会把沙箱和审批一起拆掉。

## 七 · 数据合规

- 病历**不出机器**：主模型（agent-strong）与 OCR（OvisOCR2）都跑在本机 8102/8110，数据全程在本地显存/磁盘。
- 沙箱实测**断网**（`curl` exit 7），即使模型想外传也没有通路。
- **哪天真要换成云 API 当主模型，那才是需要重新评估合规的时刻** —— 那属于数据出境问题，不只是技术问题。

## 八 · 能力分层：怎么让它"能干活"又不失控

**「它写不了文件、跑不了东西」通常是两个原因叠在一起**：
① `cx` 是**只读**沙箱，连 `~/codex-scratch` 都写不进去；
② 红线写的是"只写 scratch"，它出了 scratch 会**主动拒绝**（实测过：它引用红线拒绝写 `~/medrec`）。

正解不是每次放宽红线，而是**认领一块工地**：

| 层 | 命令 | 能写哪里 | 适用 |
|---|---|---|---|
| **L0 只读** | `cx "问一句"` / `cxf 文件` | 哪都写不了 | 查问题、出分析、读文件 |
| **L1 工地**（默认干活姿势） | `cxw "任务"` / `cxwf 任务书.txt` | `~/codex-scratch/` + **`~/medrec/workspace/`** | 写脚本、跑实验、生成中间产物、出报告 |
| **L2 授权目录** | 任务书逐条列明 + `cxw` | 再加任务书点名的目录（`models`/`run`/`logs`/`reports`） | 组装产线、下载模型、启停上游 |
| **L3 生产** | —— | `vital_rules.json` / 登记表 / `scripts/*.sh` / 服务启停 | **永远人工** |

**L1 就是"让它自己干活"的答案**：工地内它完全自由（建文件、跑 python/bash、迭代、失败重试），工地外一切照旧只读。

### 8.0 打开 L1（一次性，可撤销）

```bash
mkdir -p ~/medrec/workspace
cp -n ~/.codex/config.toml ~/.codex/config.toml.bak.$(date +%m%d_%H%M)

sed -i 's|^writable_roots = .*|writable_roots = ["/home/ubuntu/codex-scratch", "/home/ubuntu/medrec/workspace", "/home/ubuntu/medrec/reports", "/home/ubuntu/medrec/run", "/home/ubuntu/medrec/logs"]|' ~/.codex/config.toml
grep -n 'writable_roots' ~/.codex/config.toml

# 验收：工地内该写得进去，生产文件该被拒
cxw "只执行两条并报退出码：touch ~/medrec/workspace/t.txt; echo 工地: \$?; touch ~/vital_rules.json.x; echo 生产文件: \$?"
# 期望：工地 0，生产文件 非 0
```

⚠️ `writable_roots` 是**目录级**的，所以**绝不要把 `~/medrec` 或 `/home/ubuntu` 整个加进去** —— 那等于把 `vital_rules.json`、脚本、登记表一起交出去。

### 8.1 需要下载/联网时（L2+）

```toml
network_access = true      # 加在 [sandbox_workspace_write] 里；不需要时保持 false
```

- 顶层 `sandbox_mode = "read-only"` 不变 → **默认姿势仍是"什么都写不了"**；只有显式 `-s workspace-write`（`cxw`/`cxwf`）的那一次运行才拿到写权限。
- `vital_rules.json`、`tables.json`、`登记表.xlsx`、`scripts/*.sh` **始终不在可写列表里**。
- ⚠️ 若 Codex 提示 `unrecognized configuration setting: network_access` → 这版不认这个键，删掉它：agent 改为只产出下载命令，由人执行（见 §8.3）。

### 8.2 任务书必须写明授权

例外**由任务书逐条列明**，模型不得自行推断（红线 #1 的措辞就是这么定的）。任务书里应有类似一句：

> 本次任务由操作者授权：允许写 `~/medrec/models`、`~/medrec/run`、`~/medrec/logs`。其余红线不变。

### 8.3 撤销与审计

```bash
# 撤销：把 writable_roots 收回到只有 scratch
sed -i 's|^writable_roots = .*|writable_roots = ["/home/ubuntu/codex-scratch"]|' ~/.codex/config.toml
sed -i '/^network_access/d' ~/.codex/config.toml

# 审计：这次运行写了什么、下了什么
ls -lt ~/medrec/models | head
cat ~/codex-scratch/**/report.md 2>/dev/null | head -60
ls -lt ~/.codex/sessions | head -3        # 逐条命令记录
```

### 8.4 这类任务仍**绝对禁止**的事

- ❌ `10-start-ocr.sh stop` / `13-svc-all.sh` / `11-start-qwen.sh` —— 它们的清理逻辑**按端口杀进程**，会顺手杀掉正在跑的 **8110 OvisOCR2 / 8110 上的任何服务**；只允许跑 `10-start-ocr.sh paddle` 或 `serve_recognition.sh` 这类**单纯启动**分支。
- ❌ 任何 `kill` / `pkill` / 端口清理；显存不够就**报告**，不许自己腾卡。
- ❌ 改 `~/medrec/scripts/*.sh`（改脚本 = 改未来所有人的启动姿势，属人工操作）。
