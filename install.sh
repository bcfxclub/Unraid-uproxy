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

echo "==> 清理并拷贝源码（先清空再拷贝，确保干净重建、移除已删除的旧文件）"
rm -rf "$SRC"
mkdir -p "$SRC"
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

echo "==> 处理登录密码"
PW_FILE="$STATE/.deploy_pw"   # 部署时生成的密码备份，便于重部署保持一致
if [ -n "${UPX_PASSWORD:-}" ]; then
  : # 显式指定，优先使用
elif [ -n "${UPX_TOKEN:-}" ]; then
  UPX_PASSWORD="$UPX_TOKEN"
  echo "    沿用已设置的 UPX_TOKEN 作为密码"
elif [ -f "$STATE/auth.json" ]; then
  # 已在界面改过密码：auth.json 优先级最高，env 传空也不影响，跳过生成避免误导
  echo "    检测到已设置的密码（auth.json），重部署沿用，不重新生成"
  UPX_PASSWORD=""
elif [ -f "$PW_FILE" ]; then
  UPX_PASSWORD="$(cat "$PW_FILE")"
  echo "    沿用上次部署生成的密码（重部署保持一致）"
else
  UPX_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
  echo "    已自动生成密码： $UPX_PASSWORD"
  echo "    （请记下来，首次登录需要用到）"
fi
# 备份本次实际使用的密码（仅当为非空明文时），供下次重部署复用，避免密码被重置
if [ -n "$UPX_PASSWORD" ]; then
  printf '%s' "$UPX_PASSWORD" > "$PW_FILE"
  chmod 600 "$PW_FILE"
fi
# 同时把密码作为旧式直连口令下发，老书签 ?token= 仍能继续使用。
UPX_TOKEN="$UPX_PASSWORD"

echo "==> 启动容器"
docker rm -f uproxy >/dev/null 2>&1 || true
docker run -d \
  --name uproxy \
  --restart unless-stopped \
  -p "${PORT}:8765" \
  -e "UPX_HOST_ROOT=/host" \
  -e "UPX_STATE_DIR=/config" \
  -e "TZ=Asia/Shanghai" \
  -e "UPX_USER=admin" \
  -e "UPX_PASSWORD=${UPX_PASSWORD}" \
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
echo "    在浏览器打开："
echo "      http://${IP}:${PORT}/"
echo "    用户名： admin"
echo "    密码：   ${UPX_PASSWORD}"
echo "    （登录一次后浏览器会自动记住，无需再次输入）"
echo
echo "下一步：先在 WebUI「下发」页点「生成变更预览」核对 diff，确认无误再点「应用变更」。"
echo "rc.docker 的行格式随 Unraid 版本变化，首次使用请务必人工核对一次。"
