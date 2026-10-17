#!/bin/bash
# SPDX-License-Identifier: MIT
#
# verify.sh —— 换镜像之后的验收单。逐项打印 PASS/FAIL，任何一项失败退出码非 0。
#
# 用法：
#   ./verify.sh <web容器名> [db容器名] [期望迁移条数]
# 例：
#   ./verify.sh sharkey-web sharkey_db 420
#
# 判据说明（为什么这么查）：
#   * 一律走**容器 IP** 直连应用端口，绕开反代/CDN（它们有白名单或缓存，会把故障测成正常）
#   * 健康检查在应用启动期会返回 503，所以不能只看一次，要等到 200
#   * DB 迁移条数是「这次到底跑没跑」的硬证据，比日志里的文字可靠
set -uo pipefail

WEB="${1:?用法: verify.sh <web容器名> [db容器名] [期望迁移条数]}"
DB="${2:-}"
EXPECT="${3:-}"
PORT="${PORT:-3000}"
HEALTH_PATH="${HEALTH_PATH:-/healthz}"
TIMEOUT="${TIMEOUT:-120}"
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  PASS  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL+1)); }

say "== 验收 $WEB（端口 $PORT，健康路径 $HEALTH_PATH）"

# 1) 容器存在且镜像/健康状态
if ! docker inspect "$WEB" >/dev/null 2>&1; then
  bad "容器 $WEB 不存在"
  say "结果：FAIL"; exit 1
fi
img=$(docker inspect -f '{{.Config.Image}}' "$WEB")
imgid=$(docker inspect -f '{{.Image}}' "$WEB")
started=$(docker inspect -f '{{.State.StartedAt}}' "$WEB")
health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$WEB")
say "  镜像 $img ($(echo "$imgid" | cut -c8-19))  启动于 $started  健康 $health"
[ "$health" = "unhealthy" ] && bad "容器健康状态 unhealthy（docker inspect .State.Health）"
[ "$health" = "starting" ] && say "  注意：健康检查仍在 starting（start_period 内），以下按端口实测"

# 2) 端口可达（等启动完成）
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$WEB" | awk '{print $1}')
if [ -z "$IP" ]; then bad "容器没有 IP（未加入网络？）"; else
  say "  容器 IP $IP"
  code=000; t0=$(date +%s)
  while [ $(( $(date +%s) - t0 )) -lt "$TIMEOUT" ]; do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$IP:$PORT$HEALTH_PATH" 2>/dev/null)
    [ "$code" = "200" ] && break
    sleep 2
  done
  took=$(( $(date +%s) - t0 ))
  if [ "$code" = "200" ]; then ok "$HEALTH_PATH 200（等了 ${took}s）"; else bad "$HEALTH_PATH 最终返回 $code（等了 ${took}s）"; fi
fi

# 3) 应用版本（Misskey 系：POST /api/meta）
if [ -n "$IP" ]; then
  ver=$(curl -s -m 15 -X POST -H 'Content-Type: application/json' -d '{}' "http://$IP:$PORT/api/meta" 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version",""))' 2>/dev/null)
  if [ -n "$ver" ]; then ok "应用版本 $ver"; else bad "取不到 /api/meta 的 version（非 Misskey 系？）"; fi
fi

# 4) 迁移条数（硬证据）
if [ -n "$DB" ]; then
  n=$(docker exec "$DB" sh -c 'psql -q -tA -U $POSTGRES_USER -d $POSTGRES_DB -c "select count(*) from migrations"' 2>/dev/null | tr -d '[:space:]')
  last=$(docker exec "$DB" sh -c 'psql -q -tA -U $POSTGRES_USER -d $POSTGRES_DB -c "select name from migrations order by timestamp desc limit 1"' 2>/dev/null | tr -d '\r')
  if [ -z "$n" ]; then bad "查不到 migrations 表"; else
    say "  已应用迁移 $n 条，末条 $last"
    if [ -n "$EXPECT" ] && [ "$n" != "$EXPECT" ]; then bad "迁移条数 $n ≠ 期望 $EXPECT"; else ok "迁移条数与期望一致"; fi
  fi
fi

# 5) 日志里的错误（只看最后 200 行，噪声类关键字在此列出便于人工过滤）
if docker inspect "$WEB" >/dev/null 2>&1; then
  errs=$(docker logs --tail 200 "$WEB" 2>&1 | grep -icE '\bERROR\b|unhandled|ECONNREFUSED|permission denied' || true)
  say "  近 200 行日志中疑似错误行：$errs"
  if [ "${errs:-0}" -gt 20 ]; then bad "错误行偏多（$errs），请看 docker logs --tail 200 $WEB"; else ok "错误行数量在噪声范围"; fi
fi

if [ "$FAIL" -eq 0 ]; then say "结果：PASS（$WEB）"; exit 0; else say "结果：FAIL（$FAIL 项）"; exit 1; fi
