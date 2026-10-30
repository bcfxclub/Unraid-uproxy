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
cp "$HERE"/fix-mirror.sh "$SRC"/ 2>/dev/null || true
cp "$HERE"/docker-compose.yml "$SRC"/ 2>/dev/null || true

echo "==> 安装 Unraid 容器模板"
if [ -f "$HERE/unraid/my-uproxy.xml" ]; then
  cp "$HERE/unraid/my-uproxy.xml" "$TPL/my-uproxy.xml"
fi

echo "==> 构建镜像"
# 基础镜像拉不动是本脚本最常见的失败点：daemon.json 里的 registry-mirror 已失效，
# 表现为 lookup <域名>: no such host。给出明确指引，别让用户对着 docker 报错猜。
if ! docker build -t uproxy:latest "$SRC"; then
  echo
  echo "!! 构建失败。"
  echo "   最常见原因：daemon.json 里配的镜像源域名已失效，基础镜像拉不下来。"
  echo "   （报错形如 lookup xxx on 223.5.5.5:53: no such host）"
  echo
  echo "   修一下镜像源再重跑："
  echo "     bash $(dirname "$0")/fix-mirror.sh"
  echo "     ./install.sh"
  echo
  echo "   若一条公共镜像源都不通，改用代理出口：给 dockerd 配 http_proxy 后重试。"
  exit 1
fi

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
IP="$(hostname -i 2>/dev/null | awk '{print $1}')"
echo "    直接点这个（已带口令，浏览器会自动记住）："
echo "      http://${IP}:${PORT}/?token=${UPX_TOKEN}"
echo "    口令： ${UPX_TOKEN}"
echo "    （不入口令的话，页面上所有操作都会显示「未授权」）"
echo
echo "下一步：先在 WebUI「下发」页点「生成变更预览」核对 diff，确认无误再点「应用变更」。"
echo "rc.docker 的行格式随 Unraid 版本变化，首次使用请务必人工核对一次。"
