"""验证节点测速的两条路径：TCP 握手、以及「经每个节点真实 HTTP 请求」。

没有 Docker 也能跑：HTTP 路径用本地假代理顶替 sing-box —— 每个节点对应一个
入站端口，假代理在端口上直接回 204 或立刻断连，用来确认
「状态码判定 / 失败原因 / 结果与节点名的对应关系」都是对的。

用法：python tests/nettest_check.py
"""
from __future__ import annotations

import os
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("UPX_STATE_DIR", os.path.join(ROOT, ".fakestate"))

from app import nettest  # noqa: E402

NODES = [
    {"name": "东京 01", "type": "vless", "server": "127.0.0.1", "port": 9,
     "outbound": {"type": "vless", "tag": "东京 01", "server": "127.0.0.1", "server_port": 9}},
    {"name": "洛杉矶 02", "type": "vmess", "server": "127.0.0.1", "port": 9,
     "outbound": {"type": "vmess", "tag": "洛杉矶 02", "server": "127.0.0.1", "server_port": 9}},
    {"name": "没有出站", "type": "ss", "server": "127.0.0.1", "port": 9},
]


def _bind_pair():
    """找一对连续可用端口：第一个回 204，第二个立刻断开。"""
    for cand in range(30000, 40000):
        try:
            a = socket.socket()
            a.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            a.bind(("127.0.0.1", cand))
            a.listen(16)
            b = socket.socket()
            b.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            b.bind(("127.0.0.1", cand + 1))
            b.listen(16)
            return a, b, cand
        except OSError:
            for s in (locals().get("a"), locals().get("b")):
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
    raise RuntimeError("找不到可用端口")


def _serve_ok(srv):
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return

        def handle(c=conn):
            try:
                c.recv(4096)
                c.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            finally:
                c.close()

        threading.Thread(target=handle, daemon=True).start()


def _serve_dead(srv):
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        conn.close()


def main() -> int:
    fails = []

    # 1. TCP 模式：端口 9 上没人监听 -> 应当全部不可达，且带上 mode 字段
    r = nettest.test_nodes(NODES, mode="tcp")
    if r["mode"] != "tcp" or not all(x["mode"] == "tcp" for x in r["results"]):
        fails.append("TCP 模式返回结构不对")
    if any(x["reachable"] for x in r["results"]):
        fails.append("TCP 模式把不可达的端口判成可达")
    print(f"[1] TCP 模式：{len(r['results'])} 条结果，全部不可达 = "
          f"{not any(x['reachable'] for x in r['results'])}")

    # 2. HTTP 模式：用假代理替换 sing-box 容器
    ok_srv, dead_srv, base = _bind_pair()
    threading.Thread(target=_serve_ok, args=(ok_srv,), daemon=True).start()
    threading.Thread(target=_serve_dead, args=(dead_srv,), daemon=True).start()

    nettest.TEST_NAME = "127.0.0.1"
    nettest.BASE_PORT = base
    nettest._docker_ready = lambda: (True, "")
    nettest._start_container = lambda conf: (True, "")
    nettest.outlets.ensure_net = lambda: None
    nettest.host.docker = lambda *a, timeout=60: {"rc": 0, "out": "", "err": ""}

    r = nettest.test_nodes(NODES, mode="http", url="http://probe.local/204")
    by = {x["tag"]: x for x in r["results"]}
    if r["mode"] != "http":
        fails.append(f"HTTP 模式没跑起来：{r.get('note')}")
    if not by["东京 01"]["reachable"] or by["东京 01"]["code"] != "204":
        fails.append("通了的那条没被判为 204")
    if by["洛杉矶 02"]["reachable"] or "连接" not in (by["洛杉矶 02"].get("error") or ""):
        fails.append("断了的那条没有给出失败原因")
    if by["没有出站"]["reachable"] or "出站" not in (by["没有出站"].get("error") or ""):
        fails.append("缺出站配置的节点没有单独标记")
    print(f"[2] HTTP 模式：可用 {sum(1 for x in r['results'] if x['reachable'])}/{len(r['results'])}"
          f"，失败原因示例：{by['洛杉矶 02'].get('error')}")

    # 3. 降级：Docker 不可用时必须回退 TCP 并说明原因，而不是返回空结果
    nettest._docker_ready = lambda: (False, "测试用：没有 docker")
    r = nettest.test_nodes(NODES, mode="http")
    if r["mode"] != "tcp" or "docker" not in (r.get("note") or ""):
        fails.append("Docker 不可用时没有降级 + 说明")
    print(f"[3] 降级：mode={r['mode']}，note={r.get('note')}")

    if fails:
        print("\n失败：")
        for f in fails:
            print(" -", f)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
