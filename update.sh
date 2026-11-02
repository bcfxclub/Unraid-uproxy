#!/bin/bash
# uproxy 一键更新：拉取最新代码并重新部署（基于 v2rayA 的方案）
#
# 用法（在 Unraid 终端、本仓库目录下执行）：
#   ./update.sh
#
# 做了什么：
#   1. git pull --ff-only origin main  拉取最新代码
#   2. ./install.sh                    重新构建/拉取镜像 + 重启容器
#
# 说明：代理引擎与 WebUI 由 v2rayA 提供，更新主要更新 uProxy 的部署脚本/模板/镜像层。
#       v2rayA 自身的订阅与节点配置持久化在宿主 /etc/v2raya，更新容器不会丢失。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> 拉取最新代码（origin/main）"
if ! git pull --ff-only origin main; then
  echo
  echo "!! git pull 失败。"
  echo "   可能原因："
  echo "     - 本地有未提交的改动（仓库目录应只放源码，配置都在 state 里，一般不会有）"
  echo "     - Unraid 无法访问 GitHub（请确认外网通畅，或先让 v2rayA 代理出口生效后再试）"
  echo "   解决后可重新运行 ./update.sh"
  exit 1
fi

echo
echo "==> 重新部署（install.sh 会重建镜像并重启容器）"
./install.sh

echo
echo "==> 更新完成。刷新浏览器 http://<Unraid-IP>:2017/ 即可看到新版本（v2rayA WebUI）。"
