"""出口供给：WARP 容器 / sing-box 容器。

两者都通过宿主机 docker.sock 编排，并接入 uproxy-net，
这样本容器可以直接用容器名访问出口（http://uproxy-warp:9091）。
"""
from __future__ import annotations

import json
import os
import socket

from . import host
from . import settings as S
from . import state as st

NET = os.environ.get("UPX_NET", "uproxy-net")
WARP_NAME = "uproxy-warp"
SB_NAME = "uproxy-singbox"
SB_CONF_DIR = S.STATE_DIR / "singbox"

# WARP 注册接口下发的 endpoint 就是这个域名；它在部分网络里会被解析到黑洞 IP
WARP_ENDPOINT_DOMAIN = "engage.cloudflareclient.com"
# 合法的 WARP 接入点网段（CF 自持）
WARP_OK_PREFIXES = ("162.159.", "188.114.", "172.64.", "104.16.", "104.17.")
# 已知的接入点候选，用于域名被污染时挑选可直连的 IP
WARP_CANDIDATES = [
    "162.159.193.1", "162.159.194.1", "162.159.196.1",
    "162.159.197.1", "162.159.199.1",
    "188.114.96.1", "188.114.97.1",
]


def _self_name() -> str:
    """本容器在 docker 中的标识（默认就是 hostname / 短 ID）。"""
    return os.environ.get("UPX_SELF_NAME", "").strip() or socket.gethostname()


def _ensure_net() -> None:
    """建网并把自己也接进去——默认 bridge 没有容器名 DNS，
    不把自己加进同一个用户网络，出口容器的名字永远解析不了。"""
    r = host.docker("network", "inspect", NET)
    if r["rc"] != 0:
        host.docker("network", "create", NET)
    # 已连接时会报错，忽略即可
    host.docker("network", "connect", NET, _self_name(), timeout=30)


def _status(name: str) -> dict:
    r = host.docker("inspect", "-f", "{{.State.Status}}|{{.State.Running}}", name)
    if r["rc"] != 0:
        return {"name": name, "running": False, "state": "absent"}
    state, running = (r["out"].split("|") + [""])[:2]
    return {"name": name, "running": running == "true", "state": state}


# --------------------------------------------------------------------------
# WARP
# --------------------------------------------------------------------------
def warp_start(cfg: st.AppCfg) -> dict:
    _ensure_net()
    host.docker("rm", "-f", WARP_NAME, timeout=30)
    port = cfg.outlet.warp_port
    args = [
        "run", "-d", "--name", WARP_NAME, "--restart", "unless-stopped",
        "--network", NET,
    ]
    # endpoint 域名被污染时，用 --add-host 把解析钉死在可达 IP 上。
    # 这比猜镜像的环境变量名稳，且不依赖具体镜像。
    pin = (cfg.outlet.warp_endpoint_ip or "").strip()
    if pin:
        args += ["--add-host", f"{WARP_ENDPOINT_DOMAIN}:{pin}"]
    img = cfg.outlet.warp_image
    if "microwarp" in img:
        args += ["--cap-add", "NET_ADMIN", "--cap-add", "SYS_MODULE",
                 "--sysctl", "net.ipv4.conf.all.src_valid_mark=1"]
        args += ["-e", f"BIND_PORT={port}"]
        if cfg.outlet.warp_user:
            args += ["-e", f"SOCKS_USER={cfg.outlet.warp_user}",
                     "-e", f"SOCKS_PASS={cfg.outlet.warp_pass}"]
        args += ["-v", f"{WARP_NAME}-data:/etc/wireguard"]
    else:
        args += ["-e", f"PORT={port}"]
    args.append(img)
    r = host.docker(*args, timeout=180)
    if r["rc"] != 0:
        return {"ok": False, "message": (r["err"] or r["out"] or "启动失败")[:800]}
    return {"ok": True, "message": f"WARP 容器已启动：{img}", "endpoint": f"http://{WARP_NAME}:{port}"}


def warp_stop() -> dict:
    r = host.docker("rm", "-f", WARP_NAME, timeout=60)
    return {"ok": r["rc"] == 0, "message": "已移除 WARP 容器" if r["rc"] == 0 else (r["err"] or "")[:400]}


def warp_status(cfg: st.AppCfg) -> dict:
    s = _status(WARP_NAME)
    s["endpoint"] = f"http://{WARP_NAME}:{cfg.outlet.warp_port}"
    return s


def warp_test(cfg: st.AppCfg) -> dict:
    """用 Cloudflare trace 接口确认流量真的走了 WARP。"""
    proxy = f"http://{WARP_NAME}:{cfg.outlet.warp_port}"
    r = host.run(["curl", "-sS", "--max-time", "15", "-x", proxy,
                  "https://cloudflare.com/cdn-cgi/trace"], timeout=25)
    body = r["out"] or ""
    info = {}
    for line in body.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v
    return {
        "ok": r["rc"] == 0,
        "warp": info.get("warp", "unknown"),
        "ip": info.get("ip", ""),
        "colo": info.get("colo", ""),
        "raw": body[:800],
    }


# --------------------------------------------------------------------------
# WARP 预检
# --------------------------------------------------------------------------
def _resolve(name: str) -> str:
    try:
        return socket.gethostbyname(name)
    except Exception:
        return ""


def _poisoned(ip: str) -> bool:
    """WARP 接入点只可能落在 Cloudflare 自持网段；落到别处基本就是被黑洞了。"""
    return (not ip) or (not ip.startswith(WARP_OK_PREFIXES))


def _warp_register(timeout: int = 20) -> dict:
    """真的去注册一个匿名 WARP 身份，验证注册链路是否可用。"""
    import base64
    import json as _json
    import time
    import urllib.request

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    except Exception:
        return {"ok": False, "detail": "缺少 cryptography 依赖，跳过注册检测"}

    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)
    body = _json.dumps({
        "install_id": "", "fcm_token": "",
        "tos": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "key": base64.b64encode(pub).decode(),
        "type": "PC", "locale": "en_US", "warp_enabled": True,
    }).encode()
    req = urllib.request.Request(
        "https://api.cloudflareclient.com/v0a2223/reg", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "okhttp/3.12.1",
                 "CF-Client-Version": "a-6.30-2223"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = _json.loads(r.read())
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:120]}"}
    c = d.get("config", {}) or {}
    peers = c.get("peers") or [{}]
    ep = (peers[0].get("endpoint") or {}) if peers else {}
    return {
        "ok": True,
        "detail": f"账户 {d.get('account', {}).get('account_type', 'free')}",
        "client_ip": (c.get("interface", {}).get("addresses") or {}).get("v4", ""),
        "endpoint": f"{ep.get('host', '')}:{ep.get('port', '')}",
    }


def _ping(ip: str, count: int = 4) -> dict:
    """探测接入点。不依赖 ping 的本地化文案，直接数回包行——
    中英文输出、有无统计行都能正确判定，避免把可达误报成不可达。"""
    import re
    import sys

    if sys.platform.startswith("win"):
        args = ["ping", "-n", str(count), "-w", "2000", ip]
    else:
        args = ["ping", "-c", str(count), "-W", "2", ip]
    r = host.run(args, timeout=15 + count * 3)
    out = r["out"] or ""

    # 回包行：Linux "64 bytes from ..."，Windows 中文「来自 ... 的回复」
    recv = len(re.findall(r"bytes from|来自|Reply from|�Ļظ�", out))
    loss = int(round((count - min(recv, count)) * 100.0 / count))

    avg = None
    m = re.search(r"= [\d.]+/([\d.]+)/[\d.]+", out)      # Linux rtt min/avg/max
    if m:
        avg = float(m.group(1))
    else:
        m = re.search(r"(?:平均|Average)\s*=\s*(\d+)", out)  # Windows
        if m:
            avg = float(m.group(1))
    return {"ip": ip, "loss": loss, "avg": avg}


def warp_precheck(cfg: st.AppCfg) -> dict:
    """启动 WARP 前先做一次可行性体检：域名是否被黑洞、注册能否过、
    有没有可达的接入点 IP、到 CF 边缘的吞吐大概多少。"""
    steps: list[dict] = []

    # 1. 出口基线
    r = host.run(["curl", "-sS", "--max-time", "12",
                  "https://cloudflare.com/cdn-cgi/trace"], timeout=20)
    base = {}
    for line in (r["out"] or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            base[k] = v
    steps.append({"name": "当前出口", "ok": bool(base.get("ip")),
                  "detail": f"IP {base.get('ip', '未知')} · {base.get('loc', '?')} · colo {base.get('colo', '?')}"})

    # 2. endpoint 域名解析
    ip = _resolve(WARP_ENDPOINT_DOMAIN)
    bad = _poisoned(ip)
    steps.append({"name": "接入点域名解析", "ok": not bad,
                  "detail": f"{WARP_ENDPOINT_DOMAIN} -> {ip or '解析失败'}"
                            + ("（不是 Cloudflare 网段，判定被黑洞）" if bad else "（正常）")})

    # 3. 注册链路
    reg = _warp_register()
    steps.append({"name": "WARP 注册接口", "ok": reg.get("ok", False),
                  "detail": reg.get("detail", "")
                            + (f" · 下发 endpoint {reg['endpoint']}" if reg.get("endpoint") else "")})

    # 4. 接入点连通性
    probes = [_ping(c) for c in WARP_CANDIDATES]
    alive = [p for p in probes if p["loss"] < 50]
    alive.sort(key=lambda p: (p["avg"] is None, p["avg"] or 9999))
    steps.append({"name": "接入点连通性", "ok": bool(alive),
                  "detail": (f"{len(alive)}/{len(probes)} 可达"
                             + (f"，最快 {alive[0]['ip']} 平均 {alive[0]['avg']:.0f}ms" if alive else "，全部不可达")),
                  "probes": probes})

    # 5. 到 CF 边缘的吞吐
    r = host.run(["curl", "-sS", "-o", os.devnull, "--max-time", "25",
                  "-w", "%{speed_download}",
                  "https://speed.cloudflare.com/__down?bytes=5000000"], timeout=35)
    try:
        bps = float((r["out"] or "0").strip())
    except Exception:
        bps = 0.0
    kbps = bps / 1024
    steps.append({"name": "到 CF 边缘吞吐", "ok": kbps > 200,
                  "detail": f"约 {kbps:.0f} KB/s（{kbps*8/1000:.1f} Mbps）"})

    # 结论
    suggested = alive[0]["ip"] if alive else ""
    if bad and not alive:
        verdict = "不可用：endpoint 域名被黑洞，且没有可达的接入点 IP。别在 WARP 上浪费时间。"
    elif bad:
        verdict = (f"能用，但必须手动指定接入点：把 {suggested} 填进「接入点 IP」，"
                   f"工具会用 --add-host 绕开被污染的 DNS。延迟约 "
                   f"{alive[0]['avg']:.0f}ms，只适合 CA 模板 / GitHub 这类小流量，别用来拉镜像。")
    else:
        verdict = "域名解析正常，可直接启动。"
    if kbps and kbps < 200:
        verdict += f" 注意：到 CF 只有 {kbps:.0f} KB/s，即使通了也非常慢。"

    return {"ok": bool(alive) and reg.get("ok", False), "verdict": verdict,
            "suggested_ip": suggested, "steps": steps}


# --------------------------------------------------------------------------
# sing-box（订阅选路后的出口）
# --------------------------------------------------------------------------
def _sb_config(cfg: st.AppCfg) -> dict:
    nodes = [n for n in cfg.outlet.sb_nodes if n.get("outbound")]
    if not nodes:
        raise ValueError("没有可用节点，请先拉取订阅并选择节点")
    # 节点字典只暴露 name/type/server/port/outbound，没有顶层「tag」；
    # 选择器需要的是出站 tag，与 name 同一值（_dedupe 已保证唯一）。
    tags = [n["name"] for n in nodes]
    selected = cfg.outlet.sb_node if cfg.outlet.sb_node in tags else tags[0]
    return {
        "log": {"level": "warn"},
        # 原生 api：供 WebUI 拉取实时连接数（只在 uproxy-net 内可达，不暴露到宿主）
        "api": {"listen": "0.0.0.0:9099", "token": "uproxy"},
        "inbounds": [{
            "type": "mixed",
            "tag": "mixed-in",
            "listen": "0.0.0.0",
            "listen_port": cfg.outlet.sb_port,
        }],
        "outbounds": [
            {"type": "selector", "tag": "select", "outbounds": tags, "default": selected},
            *[n["outbound"] for n in nodes],
            {"type": "direct", "tag": "direct"},
        ],
    }


def sb_start(cfg: st.AppCfg) -> dict:
    _ensure_net()
    conf = _sb_config(cfg)
    SB_CONF_DIR.mkdir(parents=True, exist_ok=True)
    conf_path = SB_CONF_DIR / "config.json"
    with open(conf_path, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)

    host.docker("rm", "-f", SB_NAME, timeout=30)
    r = host.docker(
        "run", "-d", "--name", SB_NAME, "--restart", "unless-stopped",
        "--network", NET,
        "-v", f"{SB_CONF_DIR}:/etc/sing-box",
        cfg.outlet.sb_image,
        "-c", "/etc/sing-box/config.json", "run",
        timeout=180,
    )
    if r["rc"] != 0:
        return {"ok": False, "message": (r["err"] or r["out"] or "启动失败")[:800]}
    return {"ok": True, "message": "sing-box 出口已启动",
            "endpoint": f"http://{SB_NAME}:{cfg.outlet.sb_port}",
            "selected": cfg.outlet.sb_node or "自动"}


def sb_stop() -> dict:
    r = host.docker("rm", "-f", SB_NAME, timeout=60)
    return {"ok": r["rc"] == 0, "message": "已移除 sing-box 容器" if r["rc"] == 0 else (r["err"] or "")[:400]}


def sb_status(cfg: st.AppCfg) -> dict:
    s = _status(SB_NAME)
    s["endpoint"] = f"http://{SB_NAME}:{cfg.outlet.sb_port}"
    return s
