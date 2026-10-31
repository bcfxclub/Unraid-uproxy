"""出口流量统计：仿 v2rayA 的实时吞吐面板。

数据来源（按出口类型自适应）：
- 连接数：sing-box 在容器内开了原生 api（见 outlets._sb_config），直接查
  ``/connections`` 拿活动连接条数；WARP 没有现成接口，返回 None。
- 吞吐：sing-box 的 clash_api 提供 ``/traffic``（累计 up/down 字节），
  相邻两次采样相减再除以间隔得到实时速率。WARP 没有 api，不显示吞吐。
- 运行状态：直接读本容器内 sing-box 子进程的 PID，不再依赖 ``docker stats``。

没有活动出口时返回 ``running:false``，前端据此显示占位文案。
"""
from __future__ import annotations

import json
import time

from . import host
from . import outlets
from . import state as st

_SB_API = "http://127.0.0.1:9099"
_SB_TOKEN = "uproxy"

# 出口类型 -> 采样缓存，用于算速率
_prev: dict[str, dict] = {}


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


def _sb_traffic() -> dict | None:
    """sing-box 累计收发字节；连不上 api 返回 None。"""
    r = host.run(
        ["curl", "-sS", "--max-time", "6",
         "-H", f"Authorization: Bearer {_SB_TOKEN}",
         f"{_SB_API}/traffic"],
        timeout=10,
    )
    if r["rc"] != 0:
        return None
    try:
        d = json.loads(r["out"] or "{}")
        return {"up": int(d.get("up", 0)), "down": int(d.get("down", 0))}
    except Exception:
        return None


def snapshot() -> dict:
    cfg = st.load()
    mode = cfg.outlet.mode
    running = False
    if mode == "warp":
        running = outlets.warp_status(cfg).get("running")
    elif mode == "singbox":
        running = outlets.sb_status(cfg).get("running")

    if not running:
        return {"ok": True, "running": False, "mode": mode, "now": ""}

    # 「自动（延迟最低）」现在用的是哪台：只有 sing-box 自己知道（urltest 选的）
    now_tag = outlets.sb_now().get("tag") if mode == "singbox" else ""

    conns_list = _sb_conns() if mode == "singbox" else None
    conns = len(conns_list) if conns_list is not None else None
    # 按节点（sing-box 出站 tag）统计连接分布
    conns_by_node: dict[str, int] = {}
    if conns_list:
        for c in conns_list:
            tag = (c.get("outbound") or {}).get("tag") or c.get("outbound_tag") or "?"
            conns_by_node[tag] = conns_by_node.get(tag, 0) + 1

    # 吞吐：仅 sing-box 有 api
    net = _sb_traffic() if mode == "singbox" else None
    if net is None:
        return {"ok": True, "running": True, "mode": mode, "now": now_tag,
                "conns": conns, "conns_by_node": conns_by_node}

    now = time.time()
    prev = _prev.get(mode)
    down_rate = up_rate = 0.0
    if prev:
        dt = max(now - prev["t"], 0.001)
        down_rate = max(0.0, (net["down"] - prev["down"])) / dt
        up_rate = max(0.0, (net["up"] - prev["up"])) / dt
    _prev[mode] = {"down": net["down"], "up": net["up"], "t": now}

    return {
        "ok": True, "running": True, "mode": mode, "now": now_tag,
        "down_bytes": net["down"], "up_bytes": net["up"],
        "down_rate": down_rate, "up_rate": up_rate,
        "conns": conns, "conns_by_node": conns_by_node,
    }
