"""节点实测：TCP 握手 vs 真实 HTTP 出网。

为什么要两种：

* TCP 握手（``tcp_all``）只是连一下 ``服务器:端口``，只说明「端口开着」。
  节点被墙、密码错、TLS 握不上、落地机没出网，它全都照样显示 30ms，
  所以看着一片绿、实际一个都用不了。
* HTTP 实测是把**每个节点**挂进一个一次性 sing-box 实例：每个节点配一个
  独立的 mixed 入站端口，再用 curl 从该端口出去访问 Google / GitHub。
  拿到的是「经这个节点访问目标站点」的真实 HTTP 状态码与耗时。

为什么不用 clash_api 的 /proxies/{tag}/delay：
  那套实验性字段名在 sing-box 各版本之间漂过，写死容易「配置被拒 → 全部超时」。
  mixed 入站 + route 规则是 sing-box 一直没动过的基础语法，稳得多，
  而且能顺带拿到 HTTP 状态码，比一个毫秒数更能说明「到底通不通」。

一次性容器用完即删，不碰正在运行的 ``uproxy-singbox``；容器内拿不到 Docker
（例如本地开发机）时自动降级为 TCP 测速，并在 note 里写明原因。
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

TEST_NAME = "uproxy-sbtest"
BASE_PORT = 20000         # 每个节点一个 mixed 入站在容器内的起始端口
MAX_NODES = 120           # 端口预算 20000~20119，够用且不至于把容器拖垮

# 预置测试目标：走代理时必须能到、返回体极小的站点，适合当探针
TARGETS: dict[str, str] = {
    "google": "https://www.gstatic.com/generate_204",
    "github": "https://github.com/",
    "cloudflare": "https://cloudflare.com/cdn-cgi/trace",
}
DEFAULT_TARGET = TARGETS["google"]

_LOCK = threading.Lock()   # 同一时间只允许一个测速容器，避免重名冲突


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
# 一次性 sing-box 实例
# --------------------------------------------------------------------------
def _conf(outs: list[dict], inbounds: list[dict], rules: list[dict]) -> dict:
    return {
        "log": {"level": "error"},
        "inbounds": inbounds,
        "outbounds": outs + [{"type": "direct", "tag": "direct"}],
        "route": {"rules": rules, "final": "direct"},
    }


def _image() -> str:
    from . import state as st
    try:
        return st.load().outlet.sb_image or "ghcr.io/sagernet/sing-box:latest"
    except Exception:  # noqa: BLE001
        return "ghcr.io/sagernet/sing-box:latest"


def _docker_ready() -> tuple[bool, str]:
    """先确认 Docker 能用，免得在 create 上白等（开发机没有 docker 命令）。"""
    if not outlets.docker_available():
        return False, outlets.nodocker_message("起临时 sing-box 做 HTTP 实测")
    r = host.docker("info", "--format", "{{.ServerVersion}}", timeout=20)
    if r["rc"] == 0:
        return True, ""
    if r["rc"] == 127:
        return False, outlets.nodocker_message("起临时 sing-box 做 HTTP 实测")
    msg = (r["err"] or r["out"] or "未知原因").replace("\n", " ")[:200]
    return False, f"Docker 不可用（{msg}）"


def _start_container(conf: dict) -> tuple[bool, str]:
    """create -> cp 配置 -> start。

    用 docker cp 而不是 -v 绑定：Docker 守护进程在宿主机上，绑定源必须写宿主机
    路径，而容器内的 /config 与宿主机 appdata 路径并不等价，写 /config/... 会在
    宿主机上凭空建出一个空目录，sing-box 拿到空配置直接退出。
    """
    S.ensure_dirs()
    cfg_dir = S.STATE_DIR / "sbtest"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    local = cfg_dir / "config.json"
    with open(local, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)

    host.docker("rm", "-f", TEST_NAME, timeout=30)
    # 配置放容器根目录，不依赖镜像里是否预建 /etc/sing-box
    r = host.docker("create", "--name", TEST_NAME, "--network", outlets.NET,
                    "--restart", "no", _image(),
                    "-c", "/sb-test.json", "run", timeout=180)
    if r["rc"] != 0:
        return False, (r["err"] or r["out"] or "docker create 失败")[:300]

    r = host.docker("cp", str(local), f"{TEST_NAME}:/sb-test.json", timeout=30)
    if r["rc"] != 0:
        host.docker("rm", "-f", TEST_NAME, timeout=30)
        return False, (r["err"] or r["out"] or "写入测速配置失败")[:300]

    r = host.docker("start", TEST_NAME, timeout=60)
    if r["rc"] != 0:
        host.docker("rm", "-f", TEST_NAME, timeout=30)
        return False, (r["err"] or r["out"] or "docker start 失败")[:300]
    return True, ""


def _wait_port(port: int, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            with socket.create_connection((TEST_NAME, port), timeout=1.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _probe_one(url: str, port: int, timeout: int) -> dict:
    r = host.curl(url, proxy=f"http://{TEST_NAME}:{port}", timeout=timeout)
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


def _run_probe(nodes: list[dict], url: str, timeout_ms: int) -> dict:
    """起临时容器并逐节点做 HTTP 实测。返回 {results, note}，note 非空表示没测成。"""
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
                         "listen": "0.0.0.0", "listen_port": port})
        rules.append({"inbound": [in_tag], "outbound": tag})
        pairs.append((port, tag, name))

    if not pairs:
        return {"results": bad, "note": ""}

    ready, why = _docker_ready()
    if not ready:
        return {"results": [], "note": why}

    conf = _conf(outs, inbounds, rules)
    try:
        outlets.ensure_net()
    except Exception:  # noqa: BLE001  建网失败就让下面的 create 报错
        pass
    ok, err = _start_container(conf)
    if not ok:
        return {"results": [], "note": f"临时 sing-box 启动失败：{err}"}

    if not _wait_port(BASE_PORT, time.time() + 12):
        logs = host.docker("logs", "--tail", "6", TEST_NAME, timeout=15)
        detail = (logs["out"] or logs["err"] or "").strip().replace("\n", " ")[:300]
        host.docker("rm", "-f", TEST_NAME, timeout=30)
        return {"results": [], "note": f"临时 sing-box 没能监听端口：{detail or '未知原因'}"}

    secs = max(2, int(round(timeout_ms / 1000)))

    def one(pair: tuple[int, str, str]) -> dict:
        port, _tag, name = pair
        return {"tag": name, **_probe_one(url, port, secs)}

    try:
        with ThreadPoolExecutor(max_workers=min(10, len(pairs))) as ex:
            res = list(ex.map(one, pairs))
    finally:
        host.docker("rm", "-f", TEST_NAME, timeout=30)

    return {"results": _sort(res + bad), "note": ""}


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
