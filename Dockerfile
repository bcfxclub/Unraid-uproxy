FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UPX_HOST_ROOT=/host \
    UPX_STATE_DIR=/config \
    UPX_USER="" \
    UPX_PASSWORD=""

ENV PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        docker.io \
        iputils-ping \
        tar \
    && rm -rf /var/lib/apt/lists/*

# docker 命令行：仅用于 Unraid 6.x 老系统的「下发后重启 dockerd」路径（让 daemon.json
# 生效）。出口本身（sing-box / WARP）已在进程内运行，不再依赖它——所以这里缺 CLI
# 也只是 6.x 重启 dockerd 不可用，不再影响代理出口。
# 先按官方静态二进制装；装不上再回退 apt；最后强制自检，缺 CLI 就让构建直接失败，
# 绝不推一个「看起来成功、实际残次」的镜像。
ARG DOCKER_CLI_VERSION=27.5.1
RUN set -eux; \
    if ! command -v docker >/dev/null 2>&1; then \
        curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz && \
        tar -xzf /tmp/docker.tgz -C /tmp && \
        install -m 0755 /tmp/docker/docker /usr/local/bin/docker && \
        rm -rf /tmp/docker /tmp/docker.tgz; \
    fi; \
    command -v docker; \
    docker --version

# sing-box 二进制：uproxy 用它在本容器内**直接作为进程**跑出口（不再 docker-in-docker）。
# 选 1.11.x：支持 action:reject（分流规则）、wireguard outbound（WARP）、mixed 入站（HTTP+SOCKS）。
ARG SING_BOX_VERSION=1.11.8
RUN set -eux; \
    arch=$(uname -m); \
    case "$arch" in \
        x86_64) sb_arch=amd64 ;; \
        aarch64) sb_arch=arm64 ;; \
        armv7l) sb_arch=armv7 ;; \
        *) sb_arch=amd64 ;; \
    esac; \
    curl -fsSL "https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-${sb_arch}.tar.gz" -o /tmp/sb.tgz \
    && tar -xzf /tmp/sb.tgz -C /tmp \
    && install -m 0755 /tmp/sing-box-*/sing-box /usr/local/bin/sing-box \
    && rm -rf /tmp/sing-box-* /tmp/sb.tgz; \
    sing-box version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

EXPOSE 8765
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
