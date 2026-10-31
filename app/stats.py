"""出口流量统计：仿 v2rayA 的实时吞吐面板。

数据来源（按出口类型自适应）：
- 吞吐：用 ``docker stats`` 取活动出口容器的累计收/发字节（RX/TX），
  相邻两次采样相减再除以间隔得到实时速率。对 WARP 和 sing-box 通用。
- 连接数：sing-box 在容器内开了原生 api（见 outlets._sb_config），
  直接查 ``/connections`` 拿活动连接条数；WARP 没有现成接口，返回 None。

没有活动出口时返回 ``running:false``，前端据此显示占位文案。
"""
from __future__ import annotations

import json
import re
import time

from . import host
from . import outlets
from . import state as st

_SB_API = "http://uproxy-singbox:9099"
_SB_TOKEN = "uproxy"

# name -> {rx, tx, t}  用于相邻采样算速率
_prev: dict[str, dict] = {}

_UNIT = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def _parse_size(s: str) -> int:
    s = (s or "").strip()
    if not s:
        return 0
    m = re.match(r"^([\d.]+)\s*([KMGT]?B)$", s, re.I)
    if not m:
        try:
            return int(float(s))
        except Exception:
            return 0
    return int(float(m.group(1)) * _UNIT.get(m.group(2).upper(), 1))


def _docker_netio(name: str):
    r = host.docker("stats", "--no-stream", "--format", "{{.NetIO}}", name, timeout=15)
    if r["rc"] != 0:
        return None
    line = (r["out"] or "").strip()
    parts = [p.strip() for p in line.split("/")]
    if len(parts) != 2:
        return None
    return {"rx": _parse_size(parts[0]), "tx": _parse_size(parts[1])}


def _sb_conns() -> list | None:
    """返回 sing-box 当前活动连接列表（每个含 outbound 字段），供统计总数与按节点分布。"""
    r = host.run(
        ["curl", "-sS", "--max-time", "6",
         "-H", f"Authorization: Bearer {_SB_TOKEN}",
         f"{_SB_API}/connections"],
        timeout=10,
    )
    if r["rc"] != 0:
        return None
    try:
        d = json.loads(r["out"] or "{}")
        return d.get("connections") or []
    except Exception:
        return None


def snapshot() -> dict:
    cfg = st.load()
    mode = cfg.outlet.mode
    name = None
    running = False
    if mode == "warp":
        name = outlets.WARP_NAME
        running = outlets.warp_status(cfg).get("running")
    elif mode == "singbox":
        name = outlets.SB_NAME
        running = outlets.sb_status(cfg).get("running")

    if not (running and name):
        return {"ok": True, "running": False, "mode": mode}

    conns_list = _sb_conns() if mode == "singbox" else None
    conns = len(conns_list) if conns_list is not None else None
    # 按节点（sing-box 出站 tag）统计连接分布
    conns_by_node: dict[str, int] = {}
    if conns_list:
        for c in conns_list:
            tag = (c.get("outbound") or {}).get("tag") or c.get("outbound_tag") or "?"
            conns_by_node[tag] = conns_by_node.get(tag, 0) + 1

    net = _docker_netio(name)
    if net is None:
        return {"ok": True, "running": True, "mode": mode,
                "conns": conns, "conns_by_node": conns_by_node}

    now = time.time()
    prev = _prev.get(name)
    down_rate = up_rate = 0.0
    if prev:
        dt = max(now - prev["t"], 0.001)
        down_rate = max(0.0, (net["rx"] - prev["rx"])) / dt
        up_rate = max(0.0, (net["tx"] - prev["tx"])) / dt
    _prev[name] = {"rx": net["rx"], "tx": net["tx"], "t": now}

    return {
        "ok": True, "running": True, "mode": mode,
        "down_bytes": net["rx"], "up_bytes": net["tx"],
        "down_rate": down_rate, "up_rate": up_rate,
        "conns": conns, "conns_by_node": conns_by_node,
    }
