#!/bin/bash
# uproxy（基于 v2rayA 的 Unraid 出网加速）一键部署
#
# 代理引擎与 WebUI 由 v2rayA 提供（自带 xray 内核），uproxy 仅做 Unraid 场景化部署。
# 透明代理（iptables redirect）接管宿主机 + Docker 容器出网，覆盖 Docker 拉镜像 /
# CA 商店 / GitHub 更新加速等全部场景，并靠 v2rayA 的 DNS 防污染（DoH / 经代理转发）
# 绕过节点服务器域名在大陆网络下解析不出的死结（此前 sing-box 方案可用数为 0 的根因）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${UPX_BASE:-/mnt/user/appdata/uproxy}"
STATE="$BASE/state"
TPL=/boot/config/plugins/dockerMan/templates-user
PORT="${UPX_WEB_PORT:-2017}"

echo "==> 准备目录"
mkdir -p "$STATE" "$TPL"

echo "==> 安装 Unraid 容器模板"
cp "$HERE/unraid/my-uproxy.xml" "$TPL/my-uproxy.xml" 2>/dev/null || true

echo "==> 获取镜像（优先 CI 构建的 bcfxclub 镜像，拉不到再用 v2rayA 官方镜像兜底）"
IMAGE="ghcr.io/bcfxclub/unraid-uproxy:2.0.1"
if ! docker pull "$IMAGE" 2>/dev/null; then
  echo "   bcfxclub 镜像拉取失败，改用 v2rayA 官方镜像 ghcr.io/v2raya/v2raya:v2.5.10 ..."
  IMAGE="ghcr.io/v2raya/v2raya:v2.5.10"
  docker pull "$IMAGE" 2>/dev/null || {
    echo "   官方 ghcr 拉取失败，尝试 Docker Hub 镜像 mzz2017/v2raya:v2.5.10 ..."
    IMAGE="mzz2017/v2raya:v2.5.10"
    docker pull "$IMAGE" 2>/dev/null || { echo "镜像拉取失败，请检查网络后重试"; exit 1; }
  }
fi

echo "==> 启动容器"
docker rm -f Unraid-uproxy >/dev/null 2>&1 || true

RUN_TPL=/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/run-template
OK=0
if [ -x "$RUN_TPL" ] && [ -f "$TPL/my-uproxy.xml" ]; then
  if "$RUN_TPL" "$TPL/my-uproxy.xml"; then
    OK=1
  fi
fi
if [ "$OK" != "1" ]; then
  echo "    未检测到 Unraid 模板引擎或模板创建失败，退回直接 docker run"
  docker run -d \
    --name Unraid-uproxy \
    --restart unless-stopped \
    --network host \
    --privileged \
    -e "V2RAYA_ADDRESS=0.0.0.0:2017" \
    -e "V2RAYA_NFTABLES_SUPPORT=off" \
    -e "TZ=Asia/Shanghai" \
    -v /lib/modules:/lib/modules:ro \
    -v /etc/resolv.conf:/etc/resolv.conf \
    -v /etc/v2raya:/etc/v2raya \
    -v /boot/config:/host/boot/config \
    -v /etc/docker:/host/etc/docker \
    -v /usr/local/etc/rc.d:/host/usr/local/etc/rc.d \
    -v /etc/profile.d:/host/etc/profile.d \
    -v /etc/wgetrc:/host/etc/wgetrc \
    -v /etc/unraid-version:/host/etc/unraid-version:ro \
    -v /etc/os-release:/host/etc/os-release:ro \
    -v "${STATE}:/config" \
    "$IMAGE"
fi

echo
echo "==> 完成"
IP="$(hostname -i 2>/dev/null | awk '{print $1}')"
echo "    浏览器打开： http://${IP}:2017/"
echo "    （v2rayA 首次访问会让你设置管理员密码；第一个注册的人即为管理员）"
echo "    在 Unraid 的 Docker 页面，Unraid-uproxy 卡片上会出现「WebUI」按钮，直接点击即可访问。"

echo
echo "==> 注册 Unraid 开机自启（阵列启动后自动把容器拉起）"
if [ -w /boot/config/go ] 2>/dev/null; then
  touch "/boot/config/plugins/uproxy-autostart.sh" 2>/dev/null || true
  cat > "/boot/config/plugins/uproxy-autostart.sh" <<'AUTOSTART'
#!/bin/sh
# uproxy (v2rayA) autostart — 阵列启动后自动拉起容器
if command -v docker >/dev/null 2>&1; then
  sleep 30
  docker start Unraid-uproxy >/dev/null 2>&1 || true
fi
AUTOSTART
  chmod +x "/boot/config/plugins/uproxy-autostart.sh" 2>/dev/null || true
  grep -q "uproxy-autostart.sh" /boot/config/go 2>/dev/null || \
    echo '/boot/config/plugins/uproxy-autostart.sh &' >> /boot/config/go
  echo "    已写入 /boot/config/go，阵列重启后自动启动 Unraid-uproxy 容器。"
else
  echo "    （未检测到 /boot/config/go，跳过开机自启；如需可手动 docker start Unraid-uproxy）"
fi

echo
echo "下一步（在 v2rayA WebUI 里）："
echo "    1. 设置密码后，导入你的订阅 URL（『订阅』→『添加』→ 粘贴链接）"
echo "    2. 设置 → 透明代理：开启，实现方式选 redirect，分流模式选「绕过大陆(Bypass CN)」"
echo "    3. 设置 → 防止 DNS 污染：选 DoH 或 经代理转发（这是解决节点域名解析不出、可用数 0 的关键）"
echo "    4. 选一个节点，点启动，宿主机 + Docker 容器出网即走代理"
echo
echo "    不想用透明代理时，可关掉它，改为把 Unraid 的 Docker/商店/GitHub 指向"
echo "    v2rayA 系统代理端口 http://<IP>:20171（HTTP）或 socks5://<IP>:20170。"
