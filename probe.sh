#!/bin/bash
# SPDX-License-Identifier: MIT
#
# probe.sh —— 换镜像/重启期间量「停机了多久」。
#
# 为什么需要它：容器一重建就换 IP，所以每轮都要重新 `docker inspect` 取 IP；
# 直接用服务名或旧 IP 会得到一串假失败。另外要把两类失败分开看：
#   000 = 容器不在（被删/正在重建）→ 这段是「容器空缺」
#   5xx = 容器在但应用没就绪（Misskey 启动期 /healthz 会返 503）→ 这段是「应用启动」
# 本脚本把两段分开统计，输出的就是可以直接写进变更记录的数字。
#
# 用法：
#   ./probe.sh <容器名> [端口] [健康路径] [间隔秒] [总时长秒]
# 例：
#   ./probe.sh sharkey-web 3000 /healthz 2 600
#   Ctrl-C 随时结束，会打印汇总。
set -uo pipefail

C="${1:?用法: probe.sh <容器名> [端口] [健康路径] [间隔] [总时长]}"
PORT="${2:-3000}"
PATH_="${3:-/healthz}"
INTERVAL="${4:-2}"
DURATION="${5:-600}"
LOG="${LOG:-/tmp/probe-$(date +%Y%m%d-%H%M%S).log}"

: > "$LOG"
printf 'start %s  container=%s port=%s path=%s interval=%ss\n' "$(date '+%F %T')" "$C" "$PORT" "$PATH_" "$INTERVAL" >> "$LOG"

summary() {
  echo
  echo "== 汇总（原始记录：$LOG）"
  awk '{print $3}' "$LOG" 2>/dev/null | grep -E '^[0-9]{3}$|NOCONTAINER' | uniq -c | awk '{printf "  %-14s x%s\n", $2, $1}'
  local first_bad
  first_bad=$(grep -nE ' (000|NOCONTAINER|[45][0-9][0-9])$' "$LOG" | head -1)
  if [ -z "$first_bad" ]; then
    echo "  全程 200 —— 没有可测到的停机窗口"
    return
  fi
  local bad_lines bad_gap
  bad_lines=$(grep -cE ' (000|NOCONTAINER|[45][0-9][0-9])$' "$LOG")
  bad_gap=$(( bad_lines * INTERVAL ))
  echo "  $(echo "$first_bad" | cut -d: -f2-)"
  echo "  失败采样 ${bad_lines} 次 × ${INTERVAL}s ≈ **${bad_gap}s 不可用**"
  grep -E ' (000|NOCONTAINER)$' "$LOG" | head -1 | awk '{print "  容器空缺起点：" $1" "$2}'
  grep -E ' 5[0-9][0-9]$' "$LOG" | head -1 | awk '{print "  应用未就绪起点：" $1" "$2}'
  grep -E ' 200$' "$LOG" | awk -v start="$(grep -E ' (000|NOCONTAINER|5[0-9][0-9])$' "$LOG" | head -1 | cut -d' ' -f1-2)" '$0 > start' | head -1 | awk '{print "  恢复 200：" $1" "$2}'
}
trap 'summary; exit 0' INT TERM

end=$(( $(date +%s) + DURATION ))
while [ "$(date +%s)" -lt "$end" ]; do
  IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$C" 2>/dev/null | awk '{print $1}')
  if [ -n "$IP" ]; then
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$IP:$PORT$PATH_" 2>/dev/null)
    [ -z "$code" ] && code=000
  else
    code=NOCONTAINER
  fi
  printf '%s %s\n' "$(date '+%F %T')" "$code" >> "$LOG"
  sleep "$INTERVAL"
done
summary
