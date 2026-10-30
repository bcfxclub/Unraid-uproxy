"""只读探针：把宿主机当前状态扫一遍，不写任何东西。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from . import host
from . import persist
from . import settings as S
from . import state as st
from . import targets

KEYS = ("rc_docker", "daemon_json", "ca_proxy", "go_file", "profile_env")


def _stat(path: str) -> dict:
    try:
        stt = os.stat(path)
        return {
            "path": path,
            "exists": True,
            "size": stt.st_size,
            "mtime": datetime.fromtimestamp(stt.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except OSError:
        return {"path": path, "exists": False, "size": 0, "mtime": ""}


def dockerd_proxy_line() -> str:
    content = host.read(S.resolve("rc_docker"))
    idx = targets._find_start_line(content.split("\n"))
    if idx < 0:
        return ""
    line = content.split("\n")[idx]
    m = re.search(r"https?_proxy=\S+", line)
    return m.group(0) if m else ""


def effective_mirrors() -> list[str]:
    txt = host.read(S.resolve("daemon_json"))
    if not txt.strip():
        return []
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return []
    return list(data.get("registry-mirrors") or [])


def unraid_version() -> str:
    for p in (S.hpath("etc/unraid-version"), "/etc/unraid-version"):
        txt = host.read(p)
        m = re.search(r'VERSION="?([^"\s]+)"?', txt)
        if m:
            return m.group(1)
    return ""


def docker_server_version() -> str:
    r = host.docker("version", "--format", "{{.Server.Version}}", timeout=20)
    return r["out"] if r["rc"] == 0 else ""


def snapshot() -> dict:
    cfg = st.load()
    go = host.read(S.resolve("go_file"))
    return {
        "host_root": S.HOST_ROOT,
        "paths": {k: _stat(S.resolve(k)) for k in KEYS},
        "unraid": unraid_version(),
        "docker_version": docker_server_version(),
        "dockerd_proxy": dockerd_proxy_line(),
        "mirrors": effective_mirrors(),
        "ca_proxy": host.read(S.resolve("ca_proxy")).strip(),
        "system_env": host.read(S.resolve("profile_env")).strip(),
        "persist_block": persist.BEGIN in go,
        "outlet_url": targets.outlet_url(cfg),
        "ca_url": targets.ca_proxy_url(cfg),
    }
