#!/bin/bash
# 卸载 uproxy（基于 v2rayA 的方案）
set -uo pipefail

BASE="${UPX_BASE:-/mnt/user/appdata/uproxy}"

echo "!! 卸载前请先确认："
echo "   1. 已停止 v2rayA 透明代理（WebUI 点停止），避免宿主机流量仍被劫持导致断网"
echo "   2. 已在 v2rayA WebUI 导出并备份订阅/配置（如需保留）"
echo
read -r -p "确认继续卸载？(y/N) " ans
[ "$ans" = "y" ] || { echo "已取消"; exit 1; }

echo "==> 停止并移除容器"
docker rm -f Unraid-uproxy >/dev/null 2>&1 || true
docker rmi uproxy:latest ghcr.io/bcfxclub/unraid-uproxy:latest >/dev/null 2>&1 || true

echo "==> 移除模板"
rm -f /boot/config/plugins/dockerMan/templates-user/my-uproxy.xml

echo "==> 清理源码目录（备份目录保留）"
rm -rf "${BASE}/src"
rm -rf "${BASE}/src/.[!.]*" 2>/dev/null || true

echo
echo "完成。v2rayA 配置仍在宿主 /etc/v2raya（容器卷），如需彻底清除请手动删除该目录。"
echo "备份仍保留在 ${BASE}/state/backups，确认无误后可自行删除。"
