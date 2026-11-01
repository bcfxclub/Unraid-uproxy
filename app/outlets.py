"""出口供给：sing-box / WARP 现在都在本容器内作为**进程**直接运行，
不再依赖宿主 docker 去编排子容器（docker-in-docker 既脆又绕，正是之前
「没有 docker 命令行 → sing-box 起不来」的根因）。

设计要点：
- sing-box 二进制由镜像提供（见 Dockerfile），uproxy 直接用子进程拉起 / 停止 / 健康检查。
- 监听 ``0.0.0.0:<port>``，并把该端口 publish 到宿主；Unraid 本机既可用
  ``http://127.0.0.1:<port>``，也可用 ``http://<Unraid-IP>:<port>``（后者给 7.0+ 的
  Outgoing Proxy Manager 用）。
- WARP 收编为 sing-box 的一个 wireguard outbound（用匿名注册拿到的 WG 参数），
  不再单独起一个 WARP 容器。
- docker 只在极少数场景才用到：6.x 老系统手动下发后「重启 dockerd」让 daemon.json 生效。
"""
from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.request

from . import groups
from . import host
from . import route
from . import settings as S
from . import state as st

# sing-box 二进制位置（Dockerfile 装好；可用环境变量覆盖便于本地调试）
SB_BIN = os.environ.get("UPX_SB_BIN", "/usr/local/bin/sing-box")

# 兼容旧引用（stats.py 等）：容器名不再用于编排，仅作展示标识
WARP_NAME = "uproxy-warp"
SB_NAME = "uproxy-singbox"
# 保留常量，避免 nettest 等旧导入报错（已不再真正建网）
NET = "uproxy-net"
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


# --------------------------------------------------------------------------
# 进程管理（本容器内的 sing-box 子进程）
# --------------------------------------------------------------------------
def _sb_dir() -> "Path":
    from pathlib import Path
    d = S.STATE_DIR / "singbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_path(kind: str) -> "Path":
    return _sb_dir() / f"{kind}.pid"


def _read_pid(pid_path: "Path") -> int:
    try:
        return int((pid_path).read_text(encoding="utf-8").strip())
    except Exception:
        return -1


def _is_running(pid_path: "Path") -> bool:
    pid = _read_pid(pid_path)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid_path: "Path") -> None:
    pid = _read_pid(pid_path)
    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        # 等它优雅退出
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.2)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        pid_path.unlink()
    except OSError:
        pass


def _spawn(binp: str, conf_path: "Path", pid_path: "Path", kind: str = "singbox"):
    """拉起一个 sing-box 子进程（run），把 PID 写入 pid 文件，日志落到状态目录。"""
    log_path = _sb_dir() / f"{kind}.log"
    fout = open(log_path, "ab")
    proc = subprocess.Popen(
        [binp, "-c", str(conf_path), "run"],
        stdout=fout, stderr=fout, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    return proc


def _wait_port(port: int, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _tail_log(kind: str, n: int = 30) -> str:
    p = _sb_dir() / f"{kind}.log"
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def _sb_ok() -> tuple[bool, str]:
    if os.path.isfile(SB_BIN) and os.access(SB_BIN, os.X_OK):
        return True, SB_BIN
    return False, SB_BIN


def ensure_net() -> None:
    """兼容旧调用：现在不再需要建 docker 网络，直接空操作。"""
    return None


# 供 nettest 等「临时 sing-box 实例」复用
def temp_run(conf: dict, kind: str, pid_name: str, port: int, timeout: float = 12.0) -> dict:
    """写配置、校验、拉起一个临时 sing-box 子进程，等端口就绪。"""
    ok, binp = _sb_ok()
    if not ok:
        return {"ok": False, "message": f"未找到 sing-box 二进制（{binp}），无法做 HTTP 实测"}
    d = _sb_dir()
    conf_path = d / f"{kind}.json"
    conf_path.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    chk = host.run([binp, "check", "-c", str(conf_path)], timeout=120)
    if chk["rc"] != 0:
        return {"ok": False, "message": "配置被拒：" + ((chk["err"] or chk["out"] or "")[:300])}
    _spawn(binp, conf_path, _pid_path(pid_name), kind=kind)
    if not _wait_port(port, time.time() + timeout):
        err = _tail_log(kind)
        _kill(_pid_path(pid_name))
        return {"ok": False, "message": "端口未就绪：" + (err[:300] or "未知原因")}
    return {"ok": True}


def stop_temp(pid_name: str) -> None:
    _kill(_pid_path(pid_name))


# --------------------------------------------------------------------------
# sing-box 配置生成（订阅节点出口）
# --------------------------------------------------------------------------
# 具体分流规则已挪到 app/route.py：一张可编辑、有序的规则表。
# 这里只负责把规则表塞进 sing-box 配置，并处理不同版本之间的语法差异。


_VER_CACHE: dict[str, tuple[int, int]] = {}


def sb_version(image: str = "") -> tuple[int, int]:
    """探测 sing-box 大版本（本机二进制）。

    为什么要探：`action: reject` 是 1.11 才有的，而旧的 block 出站 1.13 又被删了；
    1.12.0 起又强制要求显式 `route.default_domain_resolver`，缺了直接 FATAL。
    写死任何一种，碰到另一侧版本就是「配置被拒、整个代理起不来」。

    探测失败时的兜底**必须按旧版（1.10）处理，而不是按最新**：因为一旦 `version`
    命令没跑通（rc 非 0 / 输出格式不符），说明我们无法确证它的新语法，此时若仍
    按 1.12 生成 `inbound_port`/`default_domain_resolver`/`type+server` 等新字段，
    会被实际偏老的二进制拒绝（报 "unknown field"），整个测速/起代理就崩。
    按 1.10 生成的是「全版本通用」的旧写法，偏新二进制也只会忽略而非拒绝。
    """
    ok, binp = _sb_ok()
    if not ok:
        return (1, 10)
    txt = ""
    r = host.run([binp, "version"], timeout=30)
    txt = (r.get("out") or "") + (r.get("err") or "")
    # sing-box 输出形如 "sing-box 1.12.7"，取「前两个数字段」。
    # 兜底匹配 3 段版本号，防止 `version X.Y.Z` 文案变化。
    m = re.search(r"version\s+(\d+)\.(\d+)", txt, re.I) or re.search(r"(\d+)\.(\d+)\.\d+", txt)
    if r.get("rc") == 0 and m:
        return (int(m.group(1)), int(m.group(2)))
    # 探测不可信：按全版本通用的旧写法处理（见函数说明），不要默认按最新。
    return (1, 10)


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
        # 1.12 起 DNS 服务器用 type+server。关键："detour": "direct" 保证「去查 DNS
        # 服务器」这一步永远走大陆直连，不被 global final（某节点）带走——否则大陆
        # 机房里 DNS 解析请求会跟随 final 流进未必连通的节点，解析失败 -> 整站全超时。
        # 这与 route.build 的 {protocol: dns -> direct} 固定规则语义一致。
        dns_block = {"servers": [
            {"type": "udp", "tag": "ali", "server": "223.5.5.5", "detour": "direct"},
            {"type": "udp", "tag": "dnspod", "server": "119.29.11.11", "detour": "direct"},
        ], "final": "ali"}
    else:
        dns_block = {"servers": [{"address": "223.5.5.5"}, {"address": "119.29.11.11"}],
                     "final": "223.5.5.5"}

    built = route.build(cfg, modern=modern, select_tag=select_tag)
    probe_url, probe_interval, strategy = groups.probe(cfg, grp["name"])
    if selected:
        # 显式选了节点就以它为准（等价手动固定），不再让 urltest 抢走
        group_out: dict = {"type": "selector", "tag": select_tag,
                           "outbounds": gtags, "default": selected}
    elif strategy == "select":
        # 类型=手动选择：不自动挑，固定到组内选中节点（没选就第一台）
        group_out = {"type": "selector", "tag": select_tag, "outbounds": gtags,
                     "default": selected or (gtags[0] if gtags else "")}
    else:
        # 「自动（延迟最低）」：交给 urltest 真去挑最快的那台，而不是假装有个自动。
        # 探测地址与间隔来自分组自己的配置（仪表板齿轮里可改）。
        group_out = {"type": "urltest", "tag": select_tag, "outbounds": gtags,
                     "url": probe_url,
                     "interval": f"{probe_interval}s", "tolerance": 50}
    outbounds = [
        group_out,
        *[n["outbound"] for n in nodes],
        {"type": "direct", "tag": "direct"},
    ]
    if built["need_block"]:
        # 只有 ≤1.10 的老镜像才会走到这里（1.13 起 block 出站已被移除）
        outbounds.append({"type": "block", "tag": "block"})

    route_block = built["route"]
    if new_dns:
        # sing-box 1.12.0 起强制要求显式声明默认域名解析器，否则配置被拒（FATAL）。
        # 指向 dns 块里名为 ali 的服务器（见 dns_block 的 final）。
        route_block = dict(route_block, default_domain_resolver="ali")

    # 自保护：TUN 模式下，Unraid 宿主机自身的出网（Unraid 商店 / CA / 插件 /
    # docker 拉镜像 / GitHub / pypi 等）绝不能被打进代理节点。否则一旦默认节点
    # 连不上（比如 HK 011 那种 SNI 错位），Unraid 自身拉镜像、装插件全部失败，
    # 表现为「出口启停失败、拉不到任何东西」。这些目标走 direct 直连。
    #
    # 不影响用户「把系统代理指 7890」的用法：mixed 入站不走 TUN，浏览器 / 手
    # 机 / 客户端指 7890 时流量仍正常走节点，这里只管 TUN 接管到的宿主机自身流量。
    self_protect_rules = []
    if cfg.outlet.sb_tun:
        # 1) 私有网段（已含 172.16/12、10/8、192.168/16）——route.build 已有，这里
        #    不重复（避免 1.12 上重复 rule 被拒），只补 host IP 网段兜底。
        # 2) 明确列出的出网目标直连：让 Unraid 系统、商店、docker、GitHub 不绕节点。
        #    这些都是「Unraid 自身要出网」而不是「用户想翻墙」的流量，直连是刚需。
        self_protect_rules = [
            {"outbound": "direct", "domain": [
                # Unraid 商店 / CA / 插件 / 论坛
                "ca.unraid.net", "forum.unraid.net", "unraid.net",
                # Docker 拉镜像（用户拉别的代理容器用）
                "registry-1.docker.io", "docker.io", "registry.hub.docker.com",
                # GHCR（本仓库 & 其它 GHCR 镜像）
                "ghcr.io",
                # GitHub（模板 / 依赖 / 插件）
                "github.com", "raw.githubusercontent.com", "objects.githubusercontent.com",
                # PyPI / npm（装依赖）
                "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
                # 系统更新 / 基础服务
                "ntp.org",
            ]},
        ]
        # 插到规则表最前（命中即停），优先级高于用户自定义规则。
        existing_rules = route_block.get("rules", [])
        route_block["rules"] = self_protect_rules + existing_rules

    conf: dict = {
        "log": {"level": "warn"},
        # Clash API：WebUI 用它拉实时连接数（统计面板的 /connections）。
        # 只监听容器内 9099，不 publish 到宿主，外部不可达。
        "experimental": {
            "clash_api": {"external_controller": "127.0.0.1:9099", "secret": "uproxy"},
        },
        # 解析走公共 DNS 直连，避免路由死循环
        "dns": dns_block,
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": route_block,
    }
    return conf


def _stage_conf(conf: dict, name: str = "config.json") -> "Path":
    d = _sb_dir()
    path = d / name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    return path


def sb_check(cfg: st.AppCfg, conf: dict | None = None) -> dict:
    """用本机 sing-box 的 `check` 先验证一遍配置，避免起一个秒退的进程。"""
    ok, binp = _sb_ok()
    if not ok:
        return {"ok": False, "message": f"未找到 sing-box 二进制（{binp}）。请重新拉取最新镜像。"}
    if conf is None:
        try:
            conf = _sb_config(cfg)
        except ValueError as e:
            return {"ok": False, "message": str(e)}
    path = _stage_conf(conf, "check.json")
    r = host.run([binp, "check", "-c", str(path)], timeout=120)
    msg = (r["err"] or r["out"] or "").strip()
    return {"ok": r["rc"] == 0, "message": msg[:800]}


def _sb_start_wait_port(cfg, deadline: float) -> bool:
    """等待 7890 端口就绪，TUN 模式下放宽超时。

    关键修复：TUN 模式下，容器自身（拉 docker 镜像 / Unraid 商店 / 装插件）
    流量全被 TUN 接管，一启动就被打进当前选中的节点。如果该节点是 SNI 错位
    / 连不上的坏节点（如 HK 011），日志里会先刷出 TLS/certificate 错误，
    而 7890 要等 TUN 网卡建好 + 路由配完才 listen。固定 15s 会误判"启动失败"。
    这里 TUN 模式把等待时间翻倍（最长 60s），给网卡 + 首包留出时间。
    """
    base_deadline = time.time() + 15
    deadline = max(deadline, base_deadline)
    if cfg.outlet.sb_tun:
        deadline = max(deadline, time.time() + 45)
    return _wait_port(cfg.outlet.sb_port, deadline)


def _classify_startup_log(log: str) -> str:
    """把「某节点连接 TLS 失败」和「进程真没起来」区分开，给用户可操作的提示。

    如果日志里只有节点级错误（tls/certificate/x509/connection: open connection
    ...）而没有进程级 FATAL（fatal/panic/sing-box: /config），说明 sing-box
    **已经起来在接流量了**，只是某条节点连不通——不是「启动失败」。把这条
    区别写清楚，免得用户误以为代码坏了。
    """
    text = (log or "").strip()
    if not text:
        return ""
    has_fatal = re.search(r"(?i)(^|\s)(FATAL|panic|sing-box:\s*config|failed to listen|bind:)", text)
    has_conn = re.search(r"(?i)(tls: failed|certificate|x509|open connection to|connection: open)", text)
    if has_conn and not has_fatal:
        return ("（sing-box 进程已启动在接流量，错误是某条节点连接握手失败——"
                "把该节点停用、换一个能用的节点即可，不是启动器的问题）")
    return ""


def sb_start(cfg: st.AppCfg) -> dict:
    ok, binp = _sb_ok()
    if not ok:
        return {"ok": False, "message": f"未找到 sing-box 二进制（{binp}）。请重新拉取最新镜像："
                                         f"docker pull ghcr.io/bcfxclub/unraid-uproxy:latest"}
    # 先停掉可能存在的旧实例，避免端口冲突
    _kill(_pid_path("sb"))
    try:
        conf = _sb_config(cfg)
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    # 先校验再启动：配置被拒时进程会不停重启，用户只会看到「启动了但连不上」
    chk = sb_check(cfg, conf)
    if not chk["ok"]:
        return {"ok": False, "message": "sing-box 拒绝了这份配置：" + (chk["message"] or "未知原因")}

    path = _stage_conf(conf, "config.json")
    _spawn(binp, path, _pid_path("sb"), kind="sb")
    if not _sb_start_wait_port(cfg, time.time()):
        err = _tail_log("sb")
        note = _classify_startup_log(err)
        _kill(_pid_path("sb"))
        # 如果日志里只是节点级连接失败（TLS/cert），说明 sing-box 其实已起来，
        # 给用户可操作的建议而不是"启动失败"这种误导。
        msg = "sing-box 启动后未能监听端口：" + (err[:300] or "未知原因")
        if note:
            msg += note
        return {"ok": False, "message": msg,
                "hint": "节点级连接失败" if note else ""}

    tun_note = "（已启用 TUN 透明代理，需要容器有 NET_ADMIN 权限并挂载 /dev/net/tun）" if cfg.outlet.sb_tun else ""
    return {
        "ok": True,
        "message": "sing-box 出口已启动" + tun_note,
        "endpoint": f"http://127.0.0.1:{cfg.outlet.sb_port}",
        "port": cfg.outlet.sb_port,
        "group": groups.active(cfg)["name"],
        "selected": groups.active(cfg)["node"] or "自动（延迟最低）",
    }


def sb_stop() -> dict:
    _kill(_pid_path("sb"))
    return {"ok": True, "message": "已停止 sing-box"}


def sb_status(cfg: st.AppCfg) -> dict:
    running = _is_running(_pid_path("sb"))
    return {
        "name": SB_NAME,
        "running": running,
        "state": "running" if running else "absent",
        "endpoint": f"http://127.0.0.1:{cfg.outlet.sb_port}",
        "port": cfg.outlet.sb_port,
    }


# sing-box 在容器内开了 clash_api（见 _sb_config），用它问「自动分组现在用的是哪台」
_CLASH = "http://127.0.0.1:9099"
_CLASH_SECRET = "uproxy"
_NOW_CACHE = {"t": 0.0, "tag": "", "running": None}


def sb_now(ttl: float = 8.0) -> dict:
    """当前分组在 sing-box 里实际用的节点（urltest 自己挑的那台）。

    「自动（延迟最低）」不是空标签：真值在 sing-box 里，只有问它才知道现在选的是谁。
    拿不到（没起进程 / 老镜像没开 api）时返回空字符串，界面自己降级成「自动」。
    结果缓存 ttl 秒，避免统计轮询每 1.5s 打一次。
    """
    now = time.time()
    if ttl > 0 and now - float(_NOW_CACHE["t"] or 0) < ttl:
        return {"tag": _NOW_CACHE["tag"], "running": _NOW_CACHE["running"]}
    running = _is_running(_pid_path("sb"))
    tag = ""
    if running:
        try:
            req = urllib.request.Request(
                f"{_CLASH}/proxies/{route_tag()}",
                headers={"Authorization": f"Bearer {_CLASH_SECRET}"},
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                d = json.loads(r.read().decode("utf-8") or "{}")
            tag = str(d.get("now") or "")
        except Exception:
            tag = ""
    _NOW_CACHE.update(t=now, tag=tag, running=running)
    return {"tag": tag, "running": running}


def route_tag() -> str:
    """分组出站的 tag（route.build 的默认值，多处引用，集中放这里）。"""
    return "select"


# --------------------------------------------------------------------------
# WARP（收编为 sing-box 的 wireguard outbound）
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
    """真的去注册一个匿名 WARP 身份，并拿出 wireguard 所需的全部参数。

    返回字段（供 _warp_conf 直接用）：
        private_key      客户端 X25519 私钥（base64，我们自己生成的）
        address_v4/v6    WARP 分给我们的地址
        peer_public_key  对端公钥
        server           对端 host:port
        reserved         WARP 握手必需的 3 字节（由 client_id 推出）
    """
    import base64 as _b64
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    except Exception as e:
        return {"ok": False, "detail": f"缺少 cryptography 依赖，跳过注册检测：{e}"}

    try:
        priv = X25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)
        priv_b64 = _b64.b64encode(
            priv.private_bytes(serialization.Encoding.Raw,
                               serialization.PrivateFormat.Raw,
                               serialization.NoEncryption())
        ).decode()
    except Exception as e:
        return {"ok": False, "detail": f"生成密钥失败：{e}"}

    body = json.dumps({
        "install_id": "", "fcm_token": "",
        "tos": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "key": _b64.b64encode(pub).decode(),
        "type": "PC", "locale": "en_US", "warp_enabled": True,
    }).encode()
    req = urllib.request.Request(
        "https://api.cloudflareclient.com/v0a2223/reg", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "okhttp/3.12.1",
                 "CF-Client-Version": "a-6.30-2223"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:120]}"}

    c = d.get("config", {}) or {}
    peers = c.get("peers") or [{}]
    ep = (peers[0].get("endpoint") or {}) if peers else {}
    addrs = (c.get("interface", {}) or {}).get("addresses") or {}
    server = f"{ep.get('host', '')}:{ep.get('port', '')}"
    client_id = d.get("client_id", "")
    reserved = ""
    if client_id:
        try:
            raw = _b64.b64decode(client_id)
            reserved = _b64.b64encode(raw[:3]).decode()
        except Exception:
            reserved = ""
    return {
        "ok": True,
        "detail": f"账户 {d.get('account', {}).get('account_type', 'free')}",
        "private_key": priv_b64,
        "address_v4": addrs.get("v4") or "",
        "address_v6": addrs.get("v6") or "",
        "peer_public_key": peers[0].get("public_key", "") if peers else "",
        "server": server,
        "client_id": client_id,
        "reserved": reserved,
    }


def _warp_conf(cfg: st.AppCfg, reg: dict) -> dict:
    ver = sb_version()
    new_dns = ver >= (1, 12)
    local = [a for a in (reg.get("address_v4"), reg.get("address_v6")) if a]
    wg: dict = {
        "type": "wireguard",
        "tag": "warp",
        "local_address": local,
        "private_key": reg["private_key"],
        "peer_public_key": reg["peer_public_key"],
        "server": reg["server"],
    }
    if reg.get("reserved"):
        wg["reserved"] = reg["reserved"]
    route_block = {
        "rules": [
            {"protocol": "dns", "outbound": "direct"},
            {"ip_is_private": True, "outbound": "direct"},
        ],
        "final": "warp",
        "auto_detect_interface": True,
    }
    if new_dns:
        # 1.12.0+ 同样要求显式默认域名解析器；DNS 服务器走 direct 出口，
        # 避免「解析 WARP 对端域名 → 又经 warp 出口」这种自环。
        route_block["default_domain_resolver"] = "ali"
        dns_block = {"servers": [
            {"type": "udp", "tag": "ali", "server": "223.5.5.5", "detour": "direct"},
            {"type": "udp", "tag": "dnspod", "server": "119.29.11.11", "detour": "direct"},
        ], "final": "ali"}
    else:
        dns_block = None
    conf: dict = {
        "log": {"level": "warn"},
        "inbounds": [{
            "type": "mixed", "tag": "mixed-in",
            "listen": "0.0.0.0", "listen_port": cfg.outlet.warp_port,
        }],
        "outbounds": [wg, {"type": "direct", "tag": "direct"}],
        "route": route_block,
    }
    if dns_block:
        conf["dns"] = dns_block
    return conf


def warp_start(cfg: st.AppCfg) -> dict:
    ok, binp = _sb_ok()
    if not ok:
        return {"ok": False, "message": f"未找到 sing-box 二进制（{binp}）。请重新拉取最新镜像："
                                         f"docker pull ghcr.io/bcfxclub/unraid-uproxy:latest"}
    reg = _warp_register()
    if not reg.get("ok"):
        return {"ok": False, "message": "WARP 注册失败：" + (reg.get("detail") or "未知原因")}
    _kill(_pid_path("warp"))
    conf = _warp_conf(cfg, reg)
    path = _stage_conf(conf, "warp-config.json")
    chk = host.run([binp, "check", "-c", str(path)], timeout=120)
    if chk["rc"] != 0:
        return {"ok": False, "message": "WARP 配置被拒：" + ((chk["err"] or chk["out"] or "")[:300])}
    _spawn(binp, path, _pid_path("warp"), kind="warp")
    if not _wait_port(cfg.outlet.warp_port, time.time() + 15):
        err = _tail_log("warp")
        _kill(_pid_path("warp"))
        hint = "（WARP 需要 wireguard 支持：确认容器有 NET_ADMIN 权限且宿主机已加载 wireguard 内核模块）"
        return {"ok": False, "message": "WARP 启动后未能监听端口：" + (err[:240] or "未知原因") + " " + hint}
    return {
        "ok": True,
        "message": "WARP 出口已启动（sing-box wireguard）",
        "endpoint": f"http://127.0.0.1:{cfg.outlet.warp_port}",
        "port": cfg.outlet.warp_port,
    }


def warp_stop() -> dict:
    _kill(_pid_path("warp"))
    return {"ok": True, "message": "已停止 WARP"}


def warp_status(cfg: st.AppCfg) -> dict:
    running = _is_running(_pid_path("warp"))
    return {
        "name": WARP_NAME,
        "running": running,
        "state": "running" if running else "absent",
        "endpoint": f"http://127.0.0.1:{cfg.outlet.warp_port}",
        "port": cfg.outlet.warp_port,
    }


def warp_test(cfg: st.AppCfg) -> dict:
    """用 Cloudflare trace 接口确认流量真的走了 WARP。"""
    if not _is_running(_pid_path("warp")):
        return {"ok": False, "warp": "off", "ip": "", "colo": "", "raw": "WARP 未运行"}
    proxy = f"http://127.0.0.1:{cfg.outlet.warp_port}"
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


def _ping(ip: str, count: int = 4) -> dict:
    """探测接入点。不依赖 ping 的本地化文案，直接数回包行——中英文输出都能正确判定。"""
    if os.name == "nt":
        args = ["ping", "-n", str(count), "-w", "2000", ip]
    else:
        args = ["ping", "-c", str(count), "-W", "2", ip]
    r = host.run(args, timeout=15 + count * 3)
    out = r["out"] or ""
    recv = len(re.findall(r"bytes from|来自|Reply from|来自", out))
    loss = int(round((count - min(recv, count)) * 100.0 / count))
    avg = None
    m = re.search(r"= [\d.]+/([\d.]+)/[\d.]+", out)
    if m:
        avg = float(m.group(1))
    else:
        m = re.search(r"(?:平均|Average)\s*=\s*(\d+)", out)
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

    # 3. 注册链路（顺便拿到 wireguard 参数）
    reg = _warp_register()
    steps.append({"name": "WARP 注册接口", "ok": reg.get("ok", False),
                  "detail": (reg.get("detail", "")
                            + (f" · 下发 server {reg['server']}" if reg.get("server") else ""))})

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
                   f"工具会用该 IP 作为 wireguard 对端。延迟约 "
                   f"{alive[0]['avg']:.0f}ms，只适合 CA 模板 / GitHub 这类小流量，别用来拉镜像。")
    else:
        verdict = "域名解析正常，可直接启动。"
    if kbps and kbps < 200:
        verdict += f" 注意：到 CF 只有 {kbps:.0f} KB/s，即使通了也非常慢。"

    return {"ok": bool(alive) and reg.get("ok", False), "verdict": verdict,
            "suggested_ip": suggested, "steps": steps}


# --------------------------------------------------------------------------
# docker 诊断（仅 6.x 老系统「重启 dockerd」下发路径用到，不再用于出口本身）
# --------------------------------------------------------------------------
def _docker_path() -> str:
    p = shutil_which("docker")
    if p:
        return p
    for c in ("/usr/local/bin/docker", "/usr/bin/docker", "/bin/docker",
             "/usr/local/sbin/docker", "/usr/sbin/docker"):
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def docker_available() -> bool:
    return bool(_docker_path())


def docker_diag() -> dict:
    """把「docker 到底哪儿不通」拆开说清楚。注意：现在出口本身**不**依赖 docker，
    这里仅为 6.x 系统下发 / 重启 dockerd 服务做诊断。"""
    cli = _docker_path()
    sock_path = S.DOCKER_SOCK
    sock_ok = os.path.exists(sock_path)
    info = {"cli": bool(cli), "path": cli or "(未找到)", "sock": sock_ok,
            "sock_path": sock_path, "daemon": False, "detail": ""}
    if not cli:
        info["detail"] = ("容器里没有 docker 命令行。仅影响「6.x 系统下发后重启 dockerd」这一操作；"
                          "sing-box / WARP 出口已不再依赖 docker。")
        return info
    if not sock_ok:
        info["detail"] = (f"docker 命令在（{cli}），但找不到 {sock_path}。"
                          "如需重启 dockerd，请在容器里把 /var/run/docker.sock 以「设备」方式挂进来。")
        return info
    r = host.run([cli, "version", "--format", "{{.Server.Version}}"], timeout=20)
    if r["rc"] == 0 and (r["out"] or "").strip():
        info["daemon"] = True
        info["detail"] = "docker 可用，守护进程版本 " + r["out"].strip()
    else:
        info["detail"] = (f"docker 命令在（{cli}），{sock_path} 也已挂载，但连不上守护进程："
                          f"{(r['err'] or r['out'] or '未知错误')[:200]}。")
    return info


def nodocker_message(action: str = "启动出口") -> str:
    d = docker_diag()
    return f"无法{action}：{d['detail']}"


def shutil_which(cmd: str) -> str:
    from shutil import which
    return which(cmd) or ""


# 兼容旧引用：sing-box 不再以独立容器身份存在
def _status(name: str) -> dict:
    return {"name": name, "running": False, "state": "absent"}
