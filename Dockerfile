# uproxy · 基于 v2rayA 的 Unraid 出网加速
# 代理引擎与 WebUI 完全由 v2rayA 提供（自带 xray 内核），uproxy 不再维护独立引擎。
# uproxy 仅做 Unraid 场景化部署与辅助（脚本 + 容器模板 + 文档）。
#
# 基础镜像锁定 v2rayA v2.5.10（官方 ghcr.io/v2raya/v2raya），避免 latest 漂移。
# v2rayA 镜像自带 v2raya_core（Xray 系），默认监听 0.0.0.0:2017。
FROM ghcr.io/v2raya/v2raya:v2.5.10

ENV V2RAYA_ADDRESS=0.0.0.0:2017 \
    V2RAYA_NFTABLES_SUPPORT=off \
    TZ=Asia/Shanghai

# Unraid 场景化辅助脚本：可在宿主 Unraid 终端执行，或容器内 /usr/local/bin 下可用。
COPY fix-mirror.sh /usr/local/bin/fix-mirror.sh
COPY unraid/my-uproxy.xml /opt/uproxy/my-uproxy.xml
RUN chmod +x /usr/local/bin/fix-mirror.sh

# v2rayA 配置持久化目录（运行时由宿主机卷挂载 /etc/v2raya）
VOLUME ["/etc/v2raya"]

EXPOSE 2017 20170 20171 20172

# 沿用 v2rayA 官方镜像自带入口（启动 v2rayA 服务，监听 V2RAYA_ADDRESS）
