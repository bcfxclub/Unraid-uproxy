"""测速日志开关（quiet）回归测试。

界面会自己发起一些「补测」：比如打开分组编辑弹窗时，把列表里还没测过的节点
补一轮 TCP 握手（勾没勾都测，不然没法比较着挑成员）。这种后台补测不该每条
都往日志页写一行，所以请求里带 ``quiet`` 时接口不记日志。

覆盖：
  1. quiet=true  → /api/logs 的 last_seq 不变
  2. 不带 quiet  → 照常记一条 nodes 作用域的日志（不能因为加了开关就把正常测速的日志吞了）

用法：python tests/quiet_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="uproxy-quiet-")
os.environ["UPX_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["UPX_HOST_ROOT"] = os.path.join(TMP, "host")
for _k in ("UPX_TOKEN", "UPX_PASSWORD", "UPX_USER"):
    os.environ.pop(_k, None)
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from app import main as M  # noqa: E402
from app import state as st  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


def node(name: str, port: int = 9) -> dict:
    return {
        "name": name, "type": "ss", "server": "127.0.0.1", "port": port, "source": "测试订阅",
        "outbound": {"type": "shadowsocks", "tag": name, "server": "127.0.0.1",
                     "server_port": port, "method": "aes-128-gcm", "password": "x"},
    }


def main() -> int:
    client = TestClient(M.app)
    tok = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
    hdr = {"X-UPX-Token": tok}

    cfg = st.load()
    cfg.outlet.sb_nodes = [node("q1"), node("q2"), node("q3")]
    cfg.outlet.sb_order = []
    st.save(cfg)

    def seq() -> int:
        r = client.get("/api/logs", params={"since": 0, "limit": 1}, headers=hdr)
        assert r.status_code == 200, r.text
        return r.json()["last_seq"]

    def test(quiet: bool) -> dict:
        body = {"mode": "tcp", "timeout": 1000,
                "nodes": [node("q1"), node("q2"), node("q3")]}
        if quiet:
            body["quiet"] = True
        r = client.post("/api/subs/test", json=body, headers=hdr)
        assert r.status_code == 200, r.text
        return r.json()

    print("\n[1] quiet=true：补测不写日志")
    before = seq()
    d = test(True)
    after = seq()
    check(len(d.get("results") or []) == 3, f"仍然返回 3 条结果（拿到 {len(d.get('results') or [])} 条）")
    check(after == before, f"日志 seq 没变（{before} -> {after}）")

    print("\n[2] 不带 quiet：正常测速照旧记一行")
    before = seq()
    d = test(False)
    after = seq()
    check(len(d.get("results") or []) == 3, "仍然返回 3 条结果")
    check(after == before + 1, f"日志多了一条（{before} -> {after}）")
    logs = client.get("/api/logs", params={"since": after - 1, "limit": 5}, headers=hdr).json()["logs"]
    row = logs[-1] if logs else {}
    check(row.get("scope") == "nodes", f"这条日志的 scope 是 nodes（实际 {row.get('scope')}）")
    check("TCP 握手" in (row.get("msg") or ""), f"日志内容能看出是一次握手测速（{row.get('msg')}）")

    if FAILED:
        print("\n失败：")
        for f in FAILED:
            print(" -", f)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
