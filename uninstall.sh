#!/bin/bash
# 卸载 uproxy
set -uo pipefail

BASE="${UPX_BASE:-/mnt/user/appdata/uproxy}"

echo "!! 卸载前请先做两件事，否则 Unraid 重启后配置会对不上："
echo "   1. 在 WebUI「备份与还原」页点「移除开机重放块」"
echo "   2. 把 rc.docker / daemon.json / proxy.cfg 还原到原始状态"
echo
read -r -p "确认已经做完？(y/N) " ans
[ "$ans" = "y" ] || { echo "已取消"; exit 1; }

echo "==> 停止并移除容器"
docker rm -f uproxy uproxy-warp uproxy-singbox >/dev/null 2>&1 || true
docker rmi uproxy:latest >/dev/null 2>&1 || true

echo "==> 移除网络"
docker network rm uproxy-net >/dev/null 2>&1 || true

echo "==> 移除模板"
rm -f /boot/config/plugins/dockerMan/templates-user/my-uproxy.xml

echo "==> 清理文件（备份目录保留）"
rm -rf "${BASE}/src"
rm -rf "${BASE}/src/.[!.]*" 2>/dev/null || true

echo
echo "完成。备份仍保留在 ${BASE}/state/backups，确认无误后可自行删除。"
