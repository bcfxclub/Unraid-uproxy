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
    """读 Unraid 系统版本，能读出来就尽量读出来。

    主来源 /etc/unraid-version（含 VERSION="7.0.0"）；兜底读 /etc/os-release
    （PRETTY_NAME="Unraid 7.0.0" 或 VERSION_ID）。两者都要 /etc 挂进容器，
    所以模板里 /etc 必须挂载（Required=true）。多路径尝试，任一命中即返回。
    """
    unraid_files = [S.hpath("etc/unraid-version"), "/etc/unraid-version"]
    osrelease_files = [S.hpath("etc/os-release"), "/etc/os-release"]
    # 1) 优先 unraid-version：VERSION="7.0.0"
    for p in unraid_files:
        txt = host.read(p)
        m = re.search(r'(?im)^\s*VERSION\s*=\s*"?([^"\s#]+)"?', txt)
        if m:
            return m.group(1)
    # 2) 兜底 os-release：PRETTY_NAME="Unraid 7.0.0" 优先，其次 VERSION_ID
    for p in osrelease_files:
        txt = host.read(p)
        m = re.search(r'(?im)^\s*PRETTY_NAME\s*=\s*"?(Unraid[^\"#]*)"?', txt)
        if m:
            mm = re.search(r"(\d+\.\d+(?:\.\d+)?)", m.group(1))
            if mm:
                return mm.group(1)
        m = re.search(r'(?im)^\s*VERSION_ID\s*=\s*"?([^"\s#]+)"?', txt)
        if m:
            return m.group(1)
    return ""


def docker_server_version() -> str:
    r = host.docker("version", "--format", "{{.Server.Version}}", timeout=20)
    return r["out"] if r["rc"] == 0 else ""


def docker_health() -> dict:
    """docker 到底通不通，拆成「命令在不在 / sock 挂没挂 / 守护进程能不能连」三件事。

    以前体检只显示一个「未识别」，用户看不出是镜像缺 CLI 还是 sock 没挂。
    """
    from . import outlets  # 延迟导入：outlets 依赖链更重，且避免与 probe 形成循环

    d = outlets.docker_diag()
    if d["daemon"]:
        d["state"] = "ok"
    elif d["cli"] and d["sock"]:
        d["state"] = "warn"
    else:
        d["state"] = "bad"
    return d


def snapshot() -> dict:
    cfg = st.load()
    go = host.read(S.resolve("go_file"))
    return {
        "host_root": S.HOST_ROOT,
        "paths": {k: _stat(S.resolve(k)) for k in KEYS},
        "unraid": unraid_version(),
        "docker_version": docker_server_version(),
        "docker": docker_health(),
        "dockerd_proxy": dockerd_proxy_line(),
        "mirrors": effective_mirrors(),
        "ca_proxy": host.read(S.resolve("ca_proxy")).strip(),
        "system_env": host.read(S.resolve("profile_env")).strip(),
        "persist_block": persist.BEGIN in go,
        "outlet_url": targets.outlet_url(cfg),
        "ca_url": targets.ca_proxy_url(cfg),
    }
