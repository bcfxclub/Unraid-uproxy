"""节点排序（测速后按延迟重排）回归测试。

覆盖四件事：
  1. POST /api/nodes {order} 能把 sb_nodes 按给定顺序重排并落盘到 sb_order
  2. 只排了部分节点时，没点名的节点保持原有相对顺序跟在后面（稳定）
  3. 只传 order 不会顺手清空停用表（曾经因为 disabled 默认 [] 踩过）
  4. 改名 / 删除 / 订阅重新同步后，sb_order 仍能正确套用到新节点集

用法：python tests/order_check.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="uproxy-order-")
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


def names(cfg) -> list[str]:
    return [n["name"] for n in cfg.outlet.sb_nodes]


def node(name: str) -> dict:
    return {
        "name": name, "type": "ss", "server": "1.1.1.1", "port": 443, "source": "测试订阅",
        "outbound": {"type": "shadowsocks", "tag": name, "server": "1.1.1.1",
                     "server_port": 443, "method": "aes-128-gcm", "password": "x"},
    }


def main() -> int:
    client = TestClient(M.app)
    tok = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
    hdr = {"X-UPX-Token": tok}

    def post(body: dict) -> dict:
        r = client.post("/api/nodes", json=body, headers=hdr)
        assert r.status_code == 200, r.text
        return r.json()

    base = ["n1", "n2", "n3", "n4"]

    print("\n[1] 全量排序")
    cfg = st.load()
    cfg.outlet.sb_nodes = [node(x) for x in base]
    cfg.outlet.sb_disabled = ["n3"]
    cfg.outlet.sb_order = []
    st.save(cfg)

    order = ["n4", "n2", "n1", "n3"]
    post({"order": order})
    cfg = st.load()
    check(names(cfg) == order, f"sb_nodes 已按 order 重排：{names(cfg)}")
    check(cfg.outlet.sb_order == order, f"sb_order 已保存：{cfg.outlet.sb_order}")

    saved = json.loads(open(os.path.join(os.environ["UPX_STATE_DIR"], "state.json"),
                            encoding="utf-8").read())
    check(saved["outlet"]["sb_order"] == order, "顺序已落盘到 state.json")
    check(saved["outlet"]["sb_disabled"] == ["n3"], "只传 order 没有清空停用表")

    print("\n[2] 部分排序（未点名的保持原相对顺序，稳定）")
    post({"order": ["n2"]})
    cfg = st.load()
    # 当前顺序是 n4,n2,n1,n3 -> 把 n2 提到最前，其余保持 n4,n1,n3
    check(names(cfg) == ["n2", "n4", "n1", "n3"], f"部分排序结果：{names(cfg)}")
    check(cfg.outlet.sb_order == ["n2", "n4", "n1", "n3"], "sb_order 同步为完整顺序")

    print("\n[3] 改名后顺序跟着走")
    post({"rename": {"from": "n2", "to": "n2x"}})
    cfg = st.load()
    check(names(cfg)[0] == "n2x", f"改名后仍在第一位：{names(cfg)}")
    check("n2x" in cfg.outlet.sb_order and "n2" not in cfg.outlet.sb_order,
          f"sb_order 已改名：{cfg.outlet.sb_order}")

    print("\n[4] 删除后不留残留")
    post({"delete": ["n1"]})
    cfg = st.load()
    check("n1" not in names(cfg), f"n1 已删除：{names(cfg)}")
    check("n1" not in cfg.outlet.sb_order, f"sb_order 无残留：{cfg.outlet.sb_order}")

    print("\n[5] 订阅重新同步（节点集被打乱）后顺序还原")
    before = cfg.outlet.sb_order
    cfg.outlet.sb_nodes = [node(x) for x in reversed(before)]   # 模拟同步把顺序打乱
    cfg.outlet.sb_order = list(before)
    M._apply_order(cfg)
    check(names(cfg) == before, f"同步后仍按原顺序：{names(cfg)}")

    print("\n[6] order 里没有的新节点排在末尾（不插队）")
    cfg.outlet.sb_nodes = [node("newA")] + [node(x) for x in before]
    cfg.outlet.sb_order = list(before)
    M._apply_order(cfg)
    check(names(cfg) == before + ["newA"], f"新节点垫底：{names(cfg)}")

    print("\n[7] 没有排序记录时不乱动")
    cfg.outlet.sb_order = []
    cfg.outlet.sb_nodes = [node(x) for x in ["c", "b", "a"]]
    M._apply_order(cfg)
    check(names(cfg) == ["c", "b", "a"], f"顺序保持原样：{names(cfg)}")

    print("\n" + ("=" * 46))
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for m in FAILED:
            print("  - " + m)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
