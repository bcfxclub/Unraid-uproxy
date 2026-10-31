"""出口供给：WARP 容器 / sing-box 容器。

两者都通过宿主机 docker.sock 编排，并接入 uproxy-net，
这样本容器可以直接用容器名访问出口（http://uproxy-warp:9091）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket

from . import groups
from . import host
from . import route
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


# 供测速等「临时容器」复用：它们也要能用容器名互相访问
def ensure_net() -> None:
    _ensure_net()


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
# 具体分流规则已挪到 app/route.py：一张可编辑、有序的规则表。
# 这里只负责把规则表塞进 sing-box 配置，并处理不同版本之间的语法差异。


_VER_CACHE: dict[str, tuple[int, int]] = {}


def sb_version(image: str) -> tuple[int, int]:
    """探测镜像里的 sing-box 大版本（结果按镜像名缓存）。

    为什么要探：`action: reject` 是 1.11 才有的，而旧的 block 出站 1.13 又被删了。
    写死任何一种，碰到另一侧版本就是「配置被拒、整个代理起不来」。
    探测失败时按新版处理（`:latest` 镜像基本上都是新的）。
    """
    img = image or st.OutletCfg().sb_image
    if img in _VER_CACHE:
        return _VER_CACHE[img]
    ver = (1, 11)
    r = host.docker("run", "--rm", "--network", "none", img, "version", timeout=90)
    txt = (r.get("out") or "") + (r.get("err") or "")
    m = re.search(r"version\s+(\d+)\.(\d+)", txt, re.I) or re.search(r"(\d+)\.(\d+)\.\d+", txt)
    if r.get("rc") == 0 and m:
        ver = (int(m.group(1)), int(m.group(2)))
    _VER_CACHE[img] = ver
    return ver


def _sb_config(cfg: st.AppCfg, version: tuple[int, int] | None = None,
               select_tag: str = "select") -> dict:
    ver = version or sb_version(cfg.outlet.sb_image)
    modern = ver >= (1, 11)      # 规则动作用 action: reject
    new_dns = ver >= (1, 12)     # 旧 DNS 服务器写法在 1.14 被删，1.12 起用 type+server
    new_tun = ver >= (1, 10)     # tun 的 inet4_address 并入 address

    groups.ensure(cfg)
    nodes = groups.enabled_nodes(cfg)
    if not nodes:
        raise ValueError("没有可用节点，请先拉取订阅并选择节点（或启用被停用的节点）")
    # 节点字典只暴露 name/type/server/port/outbound，没有顶层「tag」；
    # 选择器需要的是出站 tag，与 name 同一值（_dedupe 已保证唯一）。
    tags = [n["name"] for n in nodes]
    # 出口只用「当前分组」里的节点：分组是用户划出来的可用集合
    grp = groups.active(cfg)
    members = set(grp["nodes"] or [])
    gtags = [t for t in tags if t in members] or tags
    selected = grp["node"] if grp["node"] in gtags else ""

    inbounds = [{
        "type": "mixed",
        "tag": "mixed-in",
        "listen": "0.0.0.0",
        "listen_port": cfg.outlet.sb_port,
    }]
    if cfg.outlet.sb_tun:
        tun = {
            "type": "tun",
            "tag": "tun-in",
            "interface_name": "tun0",
            "auto_route": True,
            "strict_route": True,
            "stack": "system",
        }
        tun["address" if new_tun else "inet4_address"] = (
            ["172.19.0.1/30"] if new_tun else "172.19.0.1/30")
        if not modern:
            # 1.11 起嗅探改由规则动作负责，见 route.build()
            tun["sniff"] = True
        inbounds.insert(0, tun)

    if new_dns:
        dns_block = {"servers": [
            {"type": "udp", "tag": "ali", "server": "223.5.5.5"},
            {"type": "udp", "tag": "dnspod", "server": "119.29.11.11"},
        ], "final": "ali"}
    else:
        dns_block = {"servers": [{"address": "223.5.5.5"}, {"address": "119.29.11.11"}],
                     "final": "223.5.5.5"}

    built = route.build(cfg, modern=modern, select_tag=select_tag)
    if selected:
        group_out: dict = {"type": "selector", "tag": select_tag,
                           "outbounds": gtags, "default": selected}
    else:
        # 分组没指定节点 = 「自动（延迟最低）」：交给 urltest 真去挑最快的那台，
        # 而不是在这里假装有个自动。sing-box 会按 url 定期重测。
        group_out = {"type": "urltest", "tag": select_tag, "outbounds": gtags,
                     "url": "https://www.gstatic.com/generate_204",
                     "interval": "3m", "tolerance": 50}
    outbounds = [
        group_out,
        *[n["outbound"] for n in nodes],
        {"type": "direct", "tag": "direct"},
    ]
    if built["need_block"]:
        # 只有 ≤1.10 的老镜像才会走到这里（1.13 起 block 出站已被移除）
        outbounds.append({"type": "block", "tag": "block"})

    conf: dict = {
        "log": {"level": "warn"},
        # Clash API：WebUI 用它拉实时连接数（统计面板的 /connections）。
        # 只在 uproxy-net 内可达，不映射到宿主机。
        "experimental": {
            "clash_api": {"external_controller": "0.0.0.0:9099", "secret": "uproxy"},
        },
        # 解析走公共 DNS 直连，避免路由死循环
        "dns": dns_block,
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": built["route"],
    }
    return conf


CONF_IN_CONTAINER = "/sb-config.json"


def _stage_conf(conf: dict):
    """把配置落到状态目录留档（便于事后排查），并返回路径。"""
    SB_CONF_DIR.mkdir(parents=True, exist_ok=True)
    path = SB_CONF_DIR / "config.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    return path


def _run_with_conf(name: str, image: str, conf: dict, cmd: list[str],
                   extra: list[str] | None = None, detach: bool = False,
                   network: str = NET, timeout: int = 180) -> dict:
    """create -> docker cp 配置 -> start。

    不能用 `-v` 挂载配置目录：docker 守护进程跑在**宿主机**上，`-v` 的源路径
    必须写宿主机路径，而容器里的 /config 在宿主机上并不存在 —— docker 会照着
    这个名字在宿主机凭空建一个空目录挂进去，sing-box 读到的是空目录，
    于是「容器起来了、日志里说找不到配置、然后不停重启」。
    docker cp 不经过宿主机路径解析，最稳。
    """
    path = _stage_conf(conf)
    host.docker("rm", "-f", name, timeout=60)
    net_args = ["--network", network] if network else []
    r = host.docker("create", "--name", name, *net_args,
                    *(extra or []), image, *cmd, timeout=120)
    if r["rc"] != 0:
        return r
    r = host.docker("cp", str(path), f"{name}:{CONF_IN_CONTAINER}", timeout=60)
    if r["rc"] != 0:
        host.docker("rm", "-f", name, timeout=30)
        return r
    if detach:
        return host.docker("start", name, timeout=timeout)
    out = host.docker("start", "-a", name, timeout=timeout)
    host.docker("rm", "-f", name, timeout=60)
    return out


def docker_path() -> str:
    """docker 命令行在哪。找不到说明当前不是真正的部署环境（比如本地预览）。"""
    return shutil.which("docker") or ""


def docker_available() -> bool:
    return bool(docker_path())


def sb_check(cfg: st.AppCfg, conf: dict | None = None) -> dict:
    """用目标镜像自带的 `sing-box check` 先验证一遍配置，避免起一个秒退的容器。"""
    if not docker_available():
        return {"ok": False, "nodocker": True,
                "message": "当前环境没有 docker 命令行，无法校验/启动 sing-box"}
    conf = conf if conf is not None else _sb_config(cfg)
    r = _run_with_conf(f"{SB_NAME}-check", cfg.outlet.sb_image, conf,
                       ["check", "-c", CONF_IN_CONTAINER],
                       network="none", timeout=150)
    msg = (r.get("err") or r.get("out") or "").strip()
    # 127 = 命令都没跑起来；这不是「配置被拒」，别把用户引到错误方向
    if r["rc"] == 127 or "WinError 2" in msg:
        return {"ok": False, "nodocker": True,
                "message": "当前环境没有 docker 命令行，无法校验/启动 sing-box"}
    return {"ok": r["rc"] == 0, "message": msg[:800]}


def sb_start(cfg: st.AppCfg) -> dict:
    if not docker_available():
        return {"ok": False, "nodocker": True,
                "message": "当前环境没有 docker 命令行，规则已保存，部署到 Unraid 后重启出口即可生效"}
    _ensure_net()
    conf = _sb_config(cfg)

    # 先校验再启动：配置被拒时容器会不停重启，用户只会看到「启动了但连不上」
    chk = sb_check(cfg, conf)
    if not chk["ok"]:
        if chk.get("nodocker"):
            return {"ok": False, "nodocker": True, "message": chk["message"]}
        return {"ok": False, "message": "sing-box 拒绝了这份配置：" + (chk["message"] or "未知原因")}

    extra = ["--restart", "unless-stopped"]
    if cfg.outlet.sb_tun:
        # TUN 透明代理需要内核 tun 设备与网络管理权限
        extra += ["--cap-add", "NET_ADMIN", "--device", "/dev/net/tun",
                  "--sysctl", "net.ipv4.ip_forward=1"]
    r = _run_with_conf(SB_NAME, cfg.outlet.sb_image, conf,
                       ["-c", CONF_IN_CONTAINER, "run"], extra=extra, detach=True)
    if r["rc"] != 0:
        return {"ok": False, "message": (r["err"] or r["out"] or "启动失败")[:800]}
    return {"ok": True, "message": "sing-box 出口已启动",
            "endpoint": f"http://{SB_NAME}:{cfg.outlet.sb_port}",
            "group": groups.active(cfg)["name"],
            "selected": groups.active(cfg)["node"] or "自动（延迟最低）"}


def sb_stop() -> dict:
    r = host.docker("rm", "-f", SB_NAME, timeout=60)
    return {"ok": r["rc"] == 0, "message": "已移除 sing-box 容器" if r["rc"] == 0 else (r["err"] or "")[:400]}


def sb_status(cfg: st.AppCfg) -> dict:
    s = _status(SB_NAME)
    s["endpoint"] = f"http://{SB_NAME}:{cfg.outlet.sb_port}"
    return s
