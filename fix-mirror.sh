#!/bin/bash
# 修复 Unraid 上失效的 Docker 镜像源
#
# 典型症状：docker build / docker pull 报
#   failed to resolve source metadata for docker.io/library/...:
#   dial tcp: lookup <镜像源域名> on 223.5.5.5:53: no such host
# 原因：daemon.json 里配的 registry-mirror 域名已下线/被污染。
# 本脚本实测候选源，挑能通的写回 daemon.json，然后重启 dockerd。
#
# 用法： bash fix-mirror.sh

set -uo pipefail

DAEMON=/etc/docker/daemon.json
POOL=(
  docker.1ms.run
  docker.m.daocloud.io
  docker.xuanyuan.me
  docker.nju.edu.cn
  hub-mirror.c.163.com
  docker.mirrors.ustc.edu.cn
  dockerhub.icu
  docker.1panel.live
  hub.rat.dev
  dockerpull.org
)

echo "==> 当前配置"
if [ -f "$DAEMON" ]; then
  cat "$DAEMON"
  echo
else
  echo "  (无 $DAEMON，将新建)"
fi

echo "==> 实测候选镜像源（每个最多 8 秒）"
OK=()
for h in "${POOL[@]}"; do
  code=$(curl -sS -o /dev/null -m 8 -w '%{http_code}' "https://$h/v2/" 2>/dev/null || echo 000)
  # /v2/ 返回 200 或 401 都算活的镜像仓库
  if [ "$code" = "200" ] || [ "$code" = "401" ]; then
    printf "  [可用] %-28s HTTP %s\n" "$h" "$code"
    OK+=("https://$h")
  else
    printf "  [不可用] %-26s HTTP %s\n" "$h" "$code"
  fi
done

if [ ${#OK[@]} -eq 0 ]; then
  echo
  echo "!! 一个能用的镜像源都没有。"
  echo "   说明这条线路到所有公共镜像站都不通。改用代理出口："
  echo "     1) 在 Unraid「Settings → Docker」停掉 Docker 服务"
  echo "     2) 或直接给 dockerd 注入代理（见 README 的『dockerd 目标』）"
  echo "   本脚本未修改任何文件。"
  exit 1
fi

echo
echo "==> 写入 ${#OK[@]} 个可用源"
[ -f "$DAEMON" ] && cp "$DAEMON" "${DAEMON}.bak.uproxy" && echo "    已备份 -> ${DAEMON}.bak.uproxy"

if command -v python3 >/dev/null 2>&1; then
  MIRRORS="$(printf '%s\n' "${OK[@]}")" python3 - "$DAEMON" <<'PY'
import json, os, sys
p = sys.argv[1]
try:
    txt = open(p).read().strip()
    d = json.loads(txt) if txt else {}
except Exception:
    d = {}
d["registry-mirrors"] = [m for m in os.environ["MIRRORS"].split("\n") if m]
open(p, "w").write(json.dumps(d, indent=2) + "\n")
PY
else
  # 无 python3：整体覆写（原文件已备份）
  {
    echo '{'
    echo '  "registry-mirrors": ['
    n=${#OK[@]}
    for ((i = 0; i < n; i++)); do
      if [ $((i + 1)) -eq "$n" ]; then echo "    \"${OK[$i]}\""
      else echo "    \"${OK[$i]}\","
      fi
    done
    echo '  ]'
    echo '}'
  } > "$DAEMON"
fi

cat "$DAEMON"
echo
echo "==> 重启 dockerd"
if [ -x /etc/rc.d/rc.docker ]; then
  /etc/rc.d/rc.docker restart
elif command -v rc.docker >/dev/null 2>&1; then
  rc.docker restart
else
  echo "  未找到 rc.docker，请在 WebGUI「Settings → Docker」手动重启 Docker 服务"
fi

echo
echo "==> 完成。验证一下："
echo "    docker pull python:3.12-slim"
echo
echo "注意：/etc/docker/daemon.json 在内存中，重启 Unraid 后会丢失。"
echo "      uproxy 装好后，在它的「下发」页勾选 mirror 目标并启用开机重放即可持久化。"
