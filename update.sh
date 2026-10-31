#!/bin/bash
# uproxy 一键更新：拉取最新代码并重新部署
#
# 用法（在 Unraid 终端、本仓库目录下执行）：
#   ./update.sh
#
# 做了什么：
#   1. git pull --ff-only origin main  拉取最新代码（不会覆盖你的本地改动）
#   2. ./install.sh                    覆盖源码 + 重建镜像 + 重启容器
#
# 密码安全：install.sh 会自动沿用已有密码（界面改过的 auth.json，或首次部署时
#           备份在 state/.deploy_pw 的密码），重部署不会把密码重置成新的。
#
# 可选：想让 Unraid 定时自动更新，可在「用户脚本(User Scripts)」里加一个
#       每周/每天执行的脚本，内容就是上面两行（注意要用本仓库的绝对路径）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> 拉取最新代码（origin/main）"
if ! git pull --ff-only origin main; then
  echo
  echo "!! git pull 失败。"
  echo "   可能原因："
  echo "     - 本地有未提交的改动（仓库目录应只放源码，配置都在 state 里，一般不会有）"
  echo "     - Unraid 无法访问 GitHub（请确认外网通畅，或先让代理出口生效后再试）"
  echo "   解决后可重新运行 ./update.sh"
  exit 1
fi

echo
echo "==> 重新部署（install.sh 会保留密码并重建镜像）"
./install.sh

echo
echo "==> 更新完成。刷新浏览器 http://<Unraid-IP>:8765/ 即可看到新版本。"
