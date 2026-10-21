# conda 代理报错：排查与修复（Ubuntu 服务器）

> 报错原文：
> `ProxyError: Conda cannot proceed due to an error in your proxy configuration.`
> Conda 自己给的三个排查点：`~/.netrc`、以 `_PROXY` 结尾的环境变量、系统级代理设置。
> 本文件按"**先确认网络走哪条路，再改配置**"的顺序组织，不要一上来就乱清变量。

## 0. 关键区分（先做）

| 位置 | 说明 |
|---|---|
| **Ubuntu 服务器**（目标机） | 本文件全部操作都在这里。它通常在内网，外网要么走代理、要么走镜像 |
| Windows 开发机 | 与服务器无关；本机命令改不了服务器的配置 |

另有一条最容易误判的事实：**`sudo` 会清空所有 `*_proxy` 环境变量**（除非 `sudo -E`）。
所以"我 shell 里明明设了代理"和"systemd / sudo 跑的时候没有代理"**可以同时成立**。

---

## 1. 一条命令看结论（先把诊断跑完）

```bash
curl -s -o /dev/null -w "直连清华源 %{http_code} %{time_total}s\n" -m 8 \
  https://pypi.tuna.tsinghua.edu.cn/simple/
curl -s -o /dev/null -w "直连 bcebos  %{http_code} %{time_total}s\n" -m 8 \
  https://paddle-whl.cdn.bcebos.com/
```

- **直连返回 200** → 走情形 A（清代理）
- **直连失败，但 `curl -x <你的代理>` 返回 200** → 走情形 B（统一代理）
- **两条都失败，且你单位有内网镜像** → 走情形 C（全量镜像，不碰代理）
- **两条都失败，且完全没有外网** → 走情形 D（离线安装）

---

## 情形 A：直连能通，是代理变量/配置在捣乱

```bash
# ① 看一眼有什么
env | grep -i proxy
conda config --show proxy_servers
cat ~/.condarc 2>/dev/null

# ② 清掉进程级代理（只对当前 shell 生效，安全）
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
# 注意大小写：conda 同时读 http_proxy 与 HTTP_PROXY

# ③ 清掉 conda 自己的代理配置（写进 ~/.condarc 的才持久）
conda config --remove-key proxy_servers 2>/dev/null || true

# ④ ★ 最容易被忽略的一步：~/.netrc
#    conda 会把 .netrc 当代理凭据读，文件损坏/权限不对就报 ProxyError
ls -l ~/.netrc 2>/dev/null && mv ~/.netrc ~/.netrc.bak

# ⑤ 重试
conda create -n vl-env python=3.12 -y
```

**判定依据**：第 ④ 步执行后 conda 就好了 → 就是 `.netrc` 的问题（很常见）。
`.netrc` 正常要求权限 `0600`；如果里面残留了机器名/过期凭据，直接改名最省事。

---

## 情形 B：必须走代理（代理本身是好的）

把代理**统一**配到三处，缺一处就会出现"有的命令能过、有的不能"：

```bash
# ① 当前 shell
export http_proxy=http://USER:PASS@PROXY_HOST:PORT
export https_proxy=$http_proxy
export no_proxy=localhost,127.0.0.1,::1

# ② conda 持久配置（不要手写 ~/.condarc，用命令）
conda config --set proxy_servers.http  "$http_proxy"
conda config --set proxy_servers.https "$http_proxy"

# ③ pip（★ 最容易漏：conda 通了 pip 还是不通）
mkdir -p ~/.config/pip
cat > ~/.config/pip/pip.conf <<EOF
[global]
proxy = $http_proxy
timeout = 120
EOF

# ④ sudo / systemd 场景（那些不继承你的 shell 环境）
#    sudo 用 -E 保留环境，或写进 /etc/environment
```

**代理含特殊字符必须 URL 编码**：密码里的 `@` → `%40`、`#` → `%23`、`:` → `%3A`。
这是"代理地址看起来对、但认证永远失败"的第一大原因。

**要点**：`no_proxy` 一定带上 `localhost,127.0.0.1`，否则后面连本机 8100 服务也会被塞进代理。

---

## 情形 C：内网机器，走国内镜像（推荐，成功率最高）

很多内网服务器其实**不需要代理**，只是 conda 默认去 `repo.anaconda.com` 拿不到。
换成镜像即可：

```bash
# ① conda 换清华源（写入 ~/.condarc）
cat > ~/.condarc <<'EOF'
channels:
  - defaults
show_channel_urls: true
default_channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r
custom_channels:
  conda-forge: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud
  pytorch: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud
ssl_verify: true
EOF

# ② 清代理残留（两者互斥，有代理就别做这步）
conda config --remove-key proxy_servers 2>/dev/null || true
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

# ③ 重试
conda clean -i -y          # 清索引缓存，避免拿旧的失败索引重试
conda create -n vl-env python=3.12 -y
```

pip 侧同步配（后面装 paddle 会用到）：

```bash
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 情形 D：完全离线（一条网都出不去）

把"联网"这一步整体搬到能上网的机器上做，产物拷进来。**conda 侧的"创建环境"其实不需要外网**，只要提前备好 wheel：

```bash
# ---- 在能上网的机器上（架构/版本要对齐：python 3.12 + linux_x86_64）----
pip download -d ./wheels -i https://pypi.tuna.tsinghua.edu.cn/simple \
  paddleocr==3.7.0 paddlex==3.7.2
# paddlepaddle-gpu 用官方轮子直链下载
wget -P ./wheels https://paddle-whl.cdn.bcebos.com/stable/cu129/paddlepaddle-gpu/paddlepaddle_gpu-3.2.1-cp312-cp312-linux_x86_64.whl
# 模型权重也提前下好（见 server-recognition-build.md 第 3 步）
```

```bash
# ---- 在服务器上：先在本地把环境建出来（不需要网）----
conda create -n vl-env python=3.12 -y --offline        # 若本地包缓存里已有 python 3.12
# 若没有缓存，改用系统 python + venv（完全不依赖 conda 源）：
python3.12 -m venv ~/medrec/venvs/vl-env
source ~/medrec/venvs/vl-env/bin/activate

# ---- 从本地目录安装（--no-index 断网安装）----
pip install --no-index --find-links=./wheels \
  paddlepaddle_gpu-3.2.1-cp312-cp312-linux_x86_64.whl
pip install --no-index --find-links=./wheels paddleocr==3.7.0 paddlex==3.7.2

# 验证
python -c "import paddle; print(paddle.__version__, paddle.device.cuda.device_count())"
```

**取舍**：情形 D 最麻烦但最可控。如果服务器能连**任意一个**内网 PyPI 镜像，
用情形 C 把 index-url 指过去，比纯离线省事得多。

---

## 2. 修好之后的验收（三行）

```bash
conda activate vl-env
python -c "import paddle; print('paddle', paddle.__version__, 'gpus', paddle.device.cuda.device_count())"
# 期望: paddle 3.2.1 gpus 4（或按 CUDA_VISIBLE_DEVICES 过滤后的数量）

curl -s -o /dev/null -w "pypi %{http_code}\n" -m 8 https://pypi.tuna.tsinghua.edu.cn/simple/
```

然后接着走 `server-recognition-build.md` 的第 3 步（下模型）与第 5 步（起服务）。

---

## 3. 一句话速查

| 现象 | 最可能的原因 | 一句话处置 |
|---|---|---|
| 报 ProxyError，但 `curl` 直连 200 | `.netrc` 或残留代理变量 | `mv ~/.netrc ~/.netrc.bak` + `unset *_proxy` |
| conda 能装、pip 不行 | pip 没配代理 | 写 `~/.config/pip/pip.conf` |
| shell 里能装、sudo/systemd 不行 | 环境变量不继承 | `sudo -E` 或写 `/etc/environment` |
| 代理认证永远失败 | 密码里有 `@ # :` 没编码 | 做 URL 编码 |
| 换了镜像仍失败 | 旧索引缓存 | `conda clean -i -y` |
| 一条外网都出不去 | 内网隔离 | 走情形 D（离线 wheel） |
