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

# docker 命令行：uProxy 靠它操作宿主机的 docker（通过挂载的 /var/run/docker.sock）。
# 有些 Debian 基础镜像里 apt 的 docker.io 只给了 dockerd、不含 CLI，
# 那样容器里就找不到 docker 命令，sing-box / WARP 出口全都起不来。
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

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

EXPOSE 8765
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
