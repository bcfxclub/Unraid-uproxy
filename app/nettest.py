"""节点实测：TCP 握手 vs 真实 HTTP 出网。

为什么要两种：

* TCP 握手（``tcp_all``）只是连一下 ``服务器:端口``，只说明「端口开着」。
  节点被墙、密码错、TLS 握不上、落地机没出网，它全都照样显示 30ms，
  所以看着一片绿、实际一个都用不了。
* HTTP 实测是把**每个节点**挂进一个一次性 sing-box 进程：每个节点配一个
  独立的 mixed 入站端口（127.0.0.1 本地），再用 curl 从该端口出去访问目标站点。
  拿到的是「经这个节点访问目标站点」的真实 HTTP 状态码与耗时。

一次性进程用完即删，不碰正在运行的 sing-box；本机拿不到 sing-box 二进制时
（极少见，镜像异常）自动降级为 TCP 测速，并在 note 里写明原因。

不再依赖宿主 Docker——这也是之前「没有 docker 命令行就完全测不了」的根因。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import host
from . import outlets
from . import settings as S

BASE_PORT = 20000         # 每个节点一个 mixed 入站在本地的起始端口
MAX_NODES = 120           # 端口预算 20000~20119，够用且不至于把容器拖垮

# 预置测试目标：走代理时必须能到、返回体极小的站点，适合当探针
TARGETS: dict[str, str] = {
    "google": "https://www.gstatic.com/generate_204",
    "github": "https://github.com/",
    "cloudflare": "https://cloudflare.com/cdn-cgi/trace",
}
DEFAULT_TARGET = TARGETS["google"]

_LOCK = threading.Lock()   # 同一时间只允许一个实测进程，避免端口冲突


# --------------------------------------------------------------------------
# TCP 握手（降级方案 / 显式选择）
# --------------------------------------------------------------------------
def _tcp_ping(node: dict, timeout: float = 3.0) -> dict:
    server = str(node.get("server") or "")
    port = node.get("port")
    name = node.get("name", "")
    if not server or not port:
        return {"tag": name, "ms": None, "reachable": False, "mode": "tcp",
                "error": "缺少服务器地址或端口"}
    start = time.time()
    try:
        with socket.create_connection((server, int(port)), timeout=timeout):
            ms = int((time.time() - start) * 1000)
        return {"tag": name, "ms": ms, "reachable": True, "mode": "tcp"}
    except OSError as e:
        return {"tag": name, "ms": None, "reachable": False, "mode": "tcp",
                "error": str(e)[:120]}


def tcp_all(nodes: list[dict], timeout: float = 3.0) -> list[dict]:
    if not nodes:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(nodes))) as ex:
        res = list(ex.map(lambda n: _tcp_ping(n, timeout), nodes))
    return _sort(res)


def _sort(res: list[dict]) -> list[dict]:
    res.sort(key=lambda x: (not x.get("reachable"),
                            x.get("ms") if x.get("ms") is not None else 999999))
    return res


# --------------------------------------------------------------------------
# 一次性 sing-box 进程
# --------------------------------------------------------------------------
def _conf(outs: list[dict], inbounds: list[dict], rules: list[dict],
          version: "tuple[int, int] | None" = None) -> dict:
    """生成一次性测速用的 sing-box 配置。

    必须跟主配置（outlets._sb_config）保持同一套版本自适应：sing-box 1.12+ 强制
    要求显式 ``route.default_domain_resolver``，否则启动直接 FATAL，测速就会整体
    失败——表现为「所有节点都测不出延迟 / 显示 HTTP 超时」。这里漏掉这点就是
    v1.0.5 之后还测不出延迟的原因。
    """
    ver = version or outlets.sb_version()
    new_dns = ver >= (1, 12)
    if new_dns:
        # 1.12 起 DNS 服务器用 type+server 写法；default_domain_resolver 指向 ali。
        dns_block = {"servers": [
            {"type": "udp", "tag": "ali", "server": "223.5.5.5"},
            {"type": "udp", "tag": "dnspod", "server": "119.29.11.11"},
        ], "final": "ali"}
    else:
        dns_block = {"servers": [{"address": "223.5.5.5"},
                                 {"address": "119.29.11.11"}],
                     "final": "223.5.5.5"}
    # 与主配置 route.build 对齐：这两条固定规则保证 DNS 解析、私有/局域网网段永远
    # 走直连，绝不绕进节点。否则临时实例里 DNS 查询可能跟着 final 走（被送进节点）
    # 或选错网卡，表现为「经节点的 HTTP 实测全部超时 / DNS 解析失败」。
    safety = [
        {"outbound": "direct", "protocol": "dns"},
        {"outbound": "direct", "ip_is_private": True},
    ]
    route_block: dict = {"rules": safety + list(rules), "final": "direct"}
    # 关键：临时实例也要和主配置一样开 auto_detect_interface。否则容器里 direct
    # 出站（节点 detour / final）会选错出口网卡，表现为「TCP 握手能通、但经节点
    # 的 HTTP 实测全部超时」——这正是用户报的 bug。主配置 route.build 一直开着，
    # 临时配置漏了，这里补齐。
    route_block["auto_detect_interface"] = True
    if new_dns:
        route_block["default_domain_resolver"] = "ali"
    return {
        "log": {"level": "error"},
        "dns": dns_block,
        "inbounds": inbounds,
        "outbounds": outs + [{"type": "direct", "tag": "direct"}],
        "route": route_block,
    }


def _run_probe(nodes: list[dict], url: str, timeout_ms: int) -> dict:
    """起临时 sing-box 进程并逐节点做 HTTP 实测。返回 {results, note}，note 非空表示没测成。"""
    outs: list[dict] = []
    inbounds: list[dict] = []
    rules: list[dict] = []
    pairs: list[tuple[int, str, str]] = []    # (入站端口, 出站 tag, 对外的节点名)
    bad: list[dict] = []
    seen: dict[str, int] = {}

    for n in nodes:
        ob = n.get("outbound") or {}
        name = str(n.get("name") or ob.get("tag") or "").strip()
        if not ob:
            bad.append({"tag": name, "ms": None, "reachable": False, "mode": "http",
                        "url": url, "error": "该节点没有可用的出站配置"})
            continue
        if len(pairs) >= MAX_NODES:
            break
        cnt = seen.get(name, 0) + 1
        seen[name] = cnt
        tag = name if cnt == 1 else f"{name}~{cnt}"
        o = dict(ob)
        o["tag"] = tag
        outs.append(o)
        idx = len(pairs)
        in_tag = f"probe-{idx}"
        port = BASE_PORT + idx
        inbounds.append({"type": "mixed", "tag": in_tag,
                         "listen": "127.0.0.1", "listen_port": port})
        pairs.append((port, tag, name))

    if not pairs:
        return {"results": bad, "note": ""}

    # sing-box 1.11 起废弃、1.12 起**删除**了 route 规则里的 `inbound` 字段，
    # 改用 `inbound_port` / `inbound_port_range`。当前镜像锁 1.12.7，如果用旧字段，
    # 「按端口把探测流量分给各节点」的规则会被整个忽略，所有流量掉到 final:direct
    # （大陆直连不通），表现就是「每个节点 HTTP 实测全超时」——正是用户报的现象。
    # 这里按版本分支：1.11+ 用 inbound_port，1.10 及更早用 inbound。
    ver = outlets.sb_version()
    use_port = ver >= (1, 11)
    for p in pairs:
        port, _tag, _name = p
        if use_port:
            rules.append({"inbound_port": port, "outbound": _tag})
        else:
            rules.append({"inbound": [f"probe-{pairs.index(p)}"], "outbound": _tag})

    ok, binp = outlets._sb_ok()
    if not ok:
        return {"results": [], "note": f"未找到 sing-box 二进制（{binp}），已降级为 TCP 握手测速"}

    conf = _conf(outs, inbounds, rules)
    got = outlets.temp_run(conf, "sbtest", "sbtest", BASE_PORT, timeout=12)
    if not got["ok"]:
        return {"results": [], "note": f"临时 sing-box 启动失败：{got['message']}"}

    secs = max(2, int(round(timeout_ms / 1000)))

    def one(pair: tuple[int, str, str]) -> dict:
        port, _tag, name = pair
        return {"tag": name, **_probe_one(url, port, secs)}

    try:
        with ThreadPoolExecutor(max_workers=min(10, len(pairs))) as ex:
            res = list(ex.map(one, pairs))
    finally:
        outlets.stop_temp("sbtest")

    return {"results": _sort(res + bad), "note": ""}


def _probe_one(url: str, port: int, timeout: int) -> dict:
    r = host.curl(url, proxy=f"http://127.0.0.1:{port}", timeout=timeout)
    code = r["code"]
    ok = code[:1] in ("2", "3")
    out = {"ms": r["ms"], "reachable": ok, "mode": "http", "url": url, "code": code}
    if not ok:
        out["ms"] = None
        out["error"] = (f"HTTP {code}" if code != "000" else "连接超时或被拒绝")
        err = (r.get("err") or "").strip().splitlines()
        if err:
            out["error"] += f"（{err[-1][:80]}）"
    return out


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------
def targets() -> dict:
    return dict(TARGETS)


def test_nodes(nodes: list[dict], mode: str = "auto", url: str = "",
               timeout_ms: int = 5000) -> dict:
    """返回 {mode, url, note, results}。

    mode: auto（优先 HTTP，环境不支持时降级）/ http（强制 HTTP）/ tcp（只握手）
    """
    nodes = [n for n in (nodes or []) if isinstance(n, dict)]
    if not nodes:
        return {"mode": "tcp", "url": "", "note": "", "results": []}

    mode = (mode or "auto").strip().lower()
    timeout_ms = max(1000, min(int(timeout_ms or 5000), 15000))

    if mode == "tcp":
        return {"mode": "tcp", "url": "", "results": tcp_all(nodes),
                "note": "TCP 握手测速：只验证节点端口可达，不代表能真正出网。"}

    url = (url or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    url = url or DEFAULT_TARGET

    if not _LOCK.acquire(blocking=False):
        return {"mode": "tcp", "url": "", "results": tcp_all(nodes),
                "note": "上一次 HTTP 实测还在进行，本次先给出 TCP 结果，请稍后重试。"}

    try:
        got = _run_probe(nodes, url, timeout_ms)
    finally:
        _LOCK.release()

    if got.get("note"):
        return {"mode": "tcp", "url": "", "results": tcp_all(nodes),
                "note": f"{got['note']}；已降级为 TCP 握手测速。"}

    return {"mode": "http", "url": url, "results": got["results"], "note": ""}
