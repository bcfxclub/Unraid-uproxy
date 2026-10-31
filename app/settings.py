"""路径解析：容器挂载点与真实宿主机路径的映射。

所有宿主机路径都可通过环境变量覆盖，便于在非 Unraid 环境做冒烟测试。
"""
from __future__ import annotations

import os
from pathlib import Path


def _env(key: str, default: str) -> str:
    v = os.environ.get(key)
    return v if v else default


HOST_ROOT = _env("UPX_HOST_ROOT", "/host").rstrip("/")
STATE_DIR = Path(_env("UPX_STATE_DIR", "/config"))
BACKUP_DIR = STATE_DIR / "backups"
STATE_FILE = STATE_DIR / "state.json"
MIRROR_CACHE = STATE_DIR / "mirror-cache.json"
MAX_BACKUPS = 40

DOCKER_SOCK = _env("UPX_DOCKER_SOCK", "/var/run/docker.sock")


def hpath(*rel: str) -> str:
    return "/".join([HOST_ROOT] + [p.strip("/") for p in rel])


CANDIDATES: dict[str, list[str]] = {
    "rc_docker": [
        hpath("usr/local/etc/rc.d/rc.docker"),
        "/usr/local/etc/rc.d/rc.docker",
    ],
    "daemon_json": [
        hpath("etc/docker/daemon.json"),
        "/etc/docker/daemon.json",
    ],
    "ca_proxy": [
        hpath("boot/config/plugins/community.applications/proxy.cfg"),
        hpath("boot/config/community.applications/proxy.cfg"),
        hpath("config/plugins/community.applications/proxy.cfg"),
        hpath("config/community.applications/proxy.cfg"),
        "/boot/config/plugins/community.applications/proxy.cfg",
        "/boot/config/community.applications/proxy.cfg",
        "/config/plugins/community.applications/proxy.cfg",
        "/config/community.applications/proxy.cfg",
    ],
    "go_file": [
        hpath("boot/config/go"),
        "/boot/config/go",
    ],
    "profile_env": [
        hpath("etc/profile.d/uproxy.sh"),
        "/etc/profile.d/uproxy.sh",
    ],
}

OVERRIDE_ENV: dict[str, str] = {
    "rc_docker": "UPX_RC_DOCKER",
    "daemon_json": "UPX_DAEMON_JSON",
    "ca_proxy": "UPX_CA_PROXY",
    "go_file": "UPX_GO_FILE",
    "profile_env": "UPX_PROFILE_ENV",
}


def resolve(key: str) -> str:
    """返回该目标的真实可写路径：显式覆盖 > 已存在 > 父目录已存在 > 首选。"""
    ov = os.environ.get(OVERRIDE_ENV[key], "").strip()
    if ov:
        return ov
    cands = CANDIDATES[key]
    for c in cands:
        if os.path.exists(c):
            return c
    for c in cands:
        if os.path.isdir(os.path.dirname(c)):
            return c
    return cands[0]


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
