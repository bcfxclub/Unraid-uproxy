#!/bin/bash
# uproxy 一键部署（在 Unraid 终端执行，不依赖 docker compose 插件）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${UPX_BASE:-/mnt/user/appdata/uproxy}"
SRC="$BASE/src"
STATE="$BASE/state"
TPL=/boot/config/plugins/dockerMan/templates-user
PORT="${UPX_WEB_PORT:-8765}"

echo "==> 准备目录"
mkdir -p "$SRC" "$STATE" "$TPL"

echo "==> 拷贝源码"
cp -r "$HERE"/app "$HERE"/web "$HERE"/Dockerfile "$HERE"/requirements.txt "$SRC"/
cp "$HERE"/docker-compose.yml "$SRC"/ 2>/dev/null || true

echo "==> 安装 Unraid 容器模板"
if [ -f "$HERE/unraid/my-uproxy.xml" ]; then
  cp "$HERE/unraid/my-uproxy.xml" "$TPL/my-uproxy.xml"
fi

echo "==> 构建镜像"
docker build -t uproxy:latest "$SRC"

echo "==> 生成访问口令"
if [ -z "${UPX_TOKEN:-}" ]; then
  UPX_TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
  echo "    已自动生成： $UPX_TOKEN"
  echo "    （请记下来，页面与接口需要用到）"
fi

echo "==> 启动容器"
docker rm -f uproxy >/dev/null 2>&1 || true
docker run -d \
  --name uproxy \
  --restart unless-stopped \
  -p "${PORT}:8765" \
  -e "UPX_HOST_ROOT=/host" \
  -e "UPX_STATE_DIR=/config" \
  -e "TZ=Asia/Shanghai" \
  -e "UPX_TOKEN=${UPX_TOKEN}" \
  -v /boot:/host/boot \
  -v /etc/docker:/host/etc/docker \
  -v /usr/local/etc/rc.d:/host/usr/local/etc/rc.d \
  -v /etc/profile.d:/host/etc/profile.d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${STATE}:/config" \
  uproxy:latest

echo
echo "==> 完成"
echo "    访问： http://$(hostname -i 2>/dev/null | awk '{print $1}'):${PORT}/"
echo "    口令： ${UPX_TOKEN}"
echo
echo "下一步：先在 WebUI「下发」页点「生成变更预览」核对 diff，确认无误再点「应用变更」。"
echo "rc.docker 的行格式随 Unraid 版本变化，首次使用请务必人工核对一次。"
