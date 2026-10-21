#!/usr/bin/env bash
# health.sh —— 一次打印三个服务的状态（round52 部署包）
# 输出格式与 brief §4.4 样例逐字一致；任一不 OK 则退出码 1。
set -euo pipefail

PY="${MEDREC_PY:-/home/ubuntu/anaconda3/envs/vl-env/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || true)"

P_STRATA="${PORT_STRATA:-8080}"
P_VLLM="${PORT_OCR_VL:-8110}"
P_WEB="${PORT_WEBUI:-8090}"

fails=0

# 从 /v1/models 的 JSON 里取第一个 model id（只取 content，不猜）
model_id() {
  local body="$1"
  printf "%s" "$body" | "$PY" -c "import sys,json
d=json.load(sys.stdin)
x=(d.get(\"data\") or [])
print(x[0].get(\"id\",\"(no id)\") if x else \"(empty)\")" 2>/dev/null || true
}

one() { # $1=port $2=label $3=kind(model|http)
  local port="$1" label="$2" kind="$3" body id code
  if [ "$kind" = model ]; then
    body="$(curl -s --max-time 5 "http://127.0.0.1:${port}/v1/models" || true)"
    if [ -n "$body" ]; then
      id="$(model_id "$body")"
      if [ -n "$id" ]; then
        printf "%s %-8s [OK]  /v1/models → %s\n" "$port" "$label" "$id"
        return 0
      fi
      printf "%s %-8s [FAIL] /v1/models 有响应但解析不出 model id（原始体前 120 字节：%s）\n" \
        "$port" "$label" "$(printf "%s" "$body" | head -c 120)"
    else
      printf "%s %-8s [FAIL] 端口 %s 无响应（服务没起 / 没绑 127.0.0.1）\n" "$port" "$label" "$port"
    fi
  else
    code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${port}/" || true)"
    if [ "$code" = 200 ] || [ "$code" = 302 ]; then
      printf "%s %-8s [OK]  HTTP %s\n" "$port" "$label" "$code"
      return 0
    fi
    printf "%s %-8s [FAIL] HTTP=%s（期望 200；端口 %s 上没有 webui）\n" "$port" "$label" "${code:-000}" "$port"
  fi
  return 1
}

one "$P_STRATA" "Strata" model || fails=$((fails+1))
one "$P_VLLM"   "vLLM"   model || fails=$((fails+1))
one "$P_WEB"    "webui"  http  || fails=$((fails+1))

[ "$fails" = 0 ] || { printf "health: %d 个服务不正常\n" "$fails" >&2; exit 1; }
