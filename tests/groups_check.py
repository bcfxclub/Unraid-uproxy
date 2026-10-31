"""代理分组回归测试。

覆盖：
  1. 老配置（没有 sb_groups）自动铺出一个 PROXY 分组，包含全部可用节点
  2. 固定节点 / 改回自动（""）时 sb_node 与分组同步；停用节点不进分组
  3. 新建 / 改名 / 删除分组的前后置条件（重名、最后一个不许删）
  4. 改成员：把已固定节点剔出成员后自动回到「自动」
  5. 节点重命名 / 删除后，分组里的名字跟着走，不留悬空引用
  6. 生成的 sing-box 配置：固定节点 → selector，自动 → urltest，
     且组出站只包含「当前分组」的节点

用法：python tests/groups_check.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="uproxy-groups-")
os.environ["UPX_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["UPX_HOST_ROOT"] = os.path.join(TMP, "host")
for _k in ("UPX_TOKEN", "UPX_PASSWORD", "UPX_USER"):
    os.environ.pop(_k, None)
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from app import main as M  # noqa: E402
from app import outlets  # noqa: E402
from app import state as st  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


def node(name: str) -> dict:
    return {
        "name": name, "type": "ss", "server": "1.1.1.1", "port": 443, "source": "测试订阅",
        "outbound": {"type": "shadowsocks", "tag": name, "server": "1.1.1.1",
                     "server_port": 443, "method": "aes-128-gcm", "password": "x"},
    }


def outbound_of(conf: dict, tag: str = "select") -> dict:
    for o in conf["outbounds"]:
        if o.get("tag") == tag:
            return o
    return {}


def main() -> int:
    client = TestClient(M.app)
    tok = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
    hdr = {"X-UPX-Token": tok}

    def get(path: str) -> dict:
        r = client.get(path, headers=hdr)
        assert r.status_code == 200, r.text
        return r.json()

    def post(path: str, body: dict) -> dict:
        r = client.post(path, json=body, headers=hdr)
        assert r.status_code == 200, r.text
        return r.json()

    print("\n[1] 老配置自动铺出 PROXY 分组")
    cfg = st.load()
    cfg.outlet.mode = "singbox"
    cfg.outlet.sb_nodes = [node(x) for x in ["a", "b", "c"]]
    cfg.outlet.sb_groups = []
    cfg.outlet.sb_group = ""
    cfg.outlet.sb_node = "b"          # 老版本存下来的默认节点
    cfg.outlet.sb_disabled = ["c"]
    st.save(cfg)

    d = get("/api/groups")
    check([g["name"] for g in d["groups"]] == ["PROXY"], f"只有一个 PROXY：{[g['name'] for g in d['groups']]}")
    check(d["groups"][0]["nodes"] == ["a", "b"], f"停用节点没进去：{d['groups'][0]['nodes']}")
    check(d["active"] == "PROXY", "当前分组是 PROXY")
    check(d["group_node"] == "b", f"保留老配置的默认节点：{d['group_node']}")

    print("\n[2] 固定节点 / 改回自动")
    post("/api/nodes", {"node": "a"})
    cfg = st.load()
    check(cfg.outlet.sb_groups[0]["node"] == "a", "分组选中 a")
    check(cfg.outlet.sb_node == "a", f"sb_node 镜像同步：{cfg.outlet.sb_node}")
    post("/api/nodes", {"node": ""})
    cfg = st.load()
    check(cfg.outlet.sb_groups[0]["node"] == "", "空字符串 = 自动（延迟最低）")
    check(cfg.outlet.sb_node == "", "sb_node 也清空")

    print("\n[3] 新建分组")
    d = post("/api/groups", {"create": {"name": "CLOUDFLARE", "nodes": ["a", "zzz"]}})
    check(d["ok"], f"创建成功：{d.get('message')}")
    check([g["name"] for g in d["groups"]] == ["PROXY", "CLOUDFLARE"], "分组列表有两项")
    cf = [g for g in d["groups"] if g["name"] == "CLOUDFLARE"][0]
    check(cf["nodes"] == ["a"], f"不存在的节点被过滤：{cf['nodes']}")
    check(d["active"] == "CLOUDFLARE", "创建后自动切到新分组")
    d2 = post("/api/groups", {"create": {"name": "CLOUDFLARE"}})
    check(d2["ok"] is False and "已存在" in d2["message"], f"重名被拒：{d2.get('message')}")

    print("\n[4] 改成员 / 改名")
    post("/api/groups", {"active": "PROXY"})
    d = post("/api/groups", {"update": {"name": "CLOUDFLARE", "nodes": ["b"], "node": ""}})
    cf = [g for g in d["groups"] if g["name"] == "CLOUDFLARE"][0]
    check(cf["nodes"] == ["b"], f"成员已更新：{cf['nodes']}")
    d = post("/api/groups", {"update": {"name": "CLOUDFLARE", "node": "b"}})
    cf = [g for g in d["groups"] if g["name"] == "CLOUDFLARE"][0]
    check(cf["node"] == "b", "分组固定到 b")
    d = post("/api/groups", {"update": {"name": "CLOUDFLARE", "nodes": ["a"]}})
    cf = [g for g in d["groups"] if g["name"] == "CLOUDFLARE"][0]
    check(cf["node"] == "", "固定节点被移出成员后自动回到「自动」")
    d = post("/api/groups", {"rename": {"from": "CLOUDFLARE", "to": "CF"}})
    check([g["name"] for g in d["groups"]] == ["PROXY", "CF"], f"改名成功：{[g['name'] for g in d['groups']]}")
    d = post("/api/groups", {"delete": "CF"})
    check([g["name"] for g in d["groups"]] == ["PROXY"], "删除分组")
    d = post("/api/groups", {"delete": "PROXY"})
    check(d["ok"] is False, f"最后一个分组不许删：{d.get('message')}")

    print("\n[5] 节点改名 / 删除后分组同步")
    post("/api/nodes", {"rename": {"from": "b", "to": "b2"}})
    cfg = st.load()
    check("b2" in cfg.outlet.sb_groups[0]["nodes"] and "b" not in cfg.outlet.sb_groups[0]["nodes"],
          f"成员改名：{cfg.outlet.sb_groups[0]['nodes']}")
    post("/api/nodes", {"delete": ["b2"]})
    cfg = st.load()
    check("b2" not in cfg.outlet.sb_groups[0]["nodes"], "删除节点后分组里没有残留")
    check(cfg.outlet.sb_node in ("", "a"), f"固定节点失效后回落：{cfg.outlet.sb_node!r}")

    print("\n[6] 生成的 sing-box 配置")
    cfg = st.load()
    cfg.outlet.sb_nodes = [node(x) for x in ["a", "b", "c"]]
    cfg.outlet.sb_disabled = []
    cfg.outlet.sb_groups = [
        {"name": "PROXY", "nodes": ["a", "b"], "node": "b"},
        {"name": "ONLY-C", "nodes": ["c"], "node": ""},
    ]
    cfg.outlet.sb_group = "PROXY"
    cfg.outlet.sb_node = "b"
    # 让「走代理」真的落到分组出站上（默认兜底是直连，与分组无关）
    cfg.outlet.sb_rules = [{"id": "r1", "enabled": True, "match": "domain_suffix",
                            "values": ["example.com"], "action": "proxy", "note": ""}]
    cfg.outlet.sb_final = "proxy"
    conf = outlets._sb_config(cfg, version=(1, 11))
    sel = outbound_of(conf)
    check(sel.get("type") == "selector", f"固定节点用 selector：{sel.get('type')}")
    check(sel.get("default") == "b", f"默认节点 b：{sel.get('default')}")
    check(sel.get("outbounds") == ["a", "b"], f"只含当前分组成员：{sel.get('outbounds')}")
    check(conf["route"]["final"] == "select", f"兜底出站指向分组：{conf['route']['final']}")
    check(any(r.get("outbound") == "select" for r in conf["route"]["rules"]),
          "走代理的规则也指向分组")

    cfg.outlet.sb_group = "ONLY-C"
    cfg.outlet.sb_node = ""
    conf = outlets._sb_config(cfg, version=(1, 11))
    grp = outbound_of(conf)
    check(grp.get("type") == "urltest", f"自动分组用 urltest：{grp.get('type')}")
    check(grp.get("outbounds") == ["c"], f"urltest 只测本组节点：{grp.get('outbounds')}")
    check(bool(grp.get("url")), "urltest 带测速地址")

    print("\n[7] 落盘后重新读回一致")
    st.save(cfg)
    saved = json.loads(open(os.path.join(os.environ["UPX_STATE_DIR"], "state.json"),
                            encoding="utf-8").read())["outlet"]
    check([g["name"] for g in saved["sb_groups"]] == ["PROXY", "ONLY-C"], f"分组已落盘：{saved['sb_groups']}")
    check(saved["sb_group"] == "ONLY-C", "当前分组已落盘")

    print("\n[8] 只剩一个分组时，移出的节点不能被自动塞回去")
    cfg = st.load()
    cfg.outlet.sb_nodes = [node(x) for x in ["a", "b", "c"]]
    cfg.outlet.sb_disabled = []
    cfg.outlet.sb_groups = [{"name": "ONLY", "nodes": ["a", "b", "c"], "node": ""}]
    cfg.outlet.sb_group = "ONLY"
    cfg.outlet.sb_group_seen = ["a", "b", "c"]
    st.save(cfg)
    d = post("/api/groups", {"update": {"name": "ONLY", "nodes": ["a"], "node": ""}})
    only = [g for g in d["groups"] if g["name"] == "ONLY"][0]
    check(only["nodes"] == ["a"], f"保存后只剩 1 个成员：{only['nodes']}")
    d = get("/api/groups")     # 再 ensure 一次，数量不能涨回去
    only = [g for g in d["groups"] if g["name"] == "ONLY"][0]
    check(only["nodes"] == ["a"], f"再次读取仍是 1 个：{only['nodes']}")
    check(only["count"] == 1, f"count 与成员数一致：{only['count']}")

    # 订阅里新出现的节点还是要补进第一个组，否则新节点会「消失」
    cfg = st.load()
    cfg.outlet.sb_nodes = cfg.outlet.sb_nodes + [node("d")]
    st.save(cfg)
    d = get("/api/groups")
    only = [g for g in d["groups"] if g["name"] == "ONLY"][0]
    check("d" in only["nodes"], f"新节点自动进第一个组：{only['nodes']}")

    print("\n[9] 分组测速配置（探测 URL / 间隔 / 类型）")
    d = post("/api/groups", {"config": {"name": "ONLY", "url": "https://cp.cloudflare.com/generate_204",
                                        "interval": "30s", "strategy": "urltest"}})
    check(d["ok"], f"保存成功：{d.get('message')}")
    only = [g for g in d["groups"] if g["name"] == "ONLY"][0]
    check(only["url"] == "https://cp.cloudflare.com/generate_204", f"URL 已存：{only['url']}")
    check(only["interval"] == 30, f"间隔已存（秒）：{only['interval']}")
    cfg = st.load()
    conf = outlets._sb_config(cfg, version=(1, 12))
    out = outbound_of(conf)
    check(out.get("type") == "urltest", f"自动分组生成 urltest：{out.get('type')}")
    check(out.get("url") == "https://cp.cloudflare.com/generate_204", f"用自定义探测地址：{out.get('url')}")
    check(out.get("interval") == "30s", f"用自定义间隔：{out.get('interval')}")
    d = post("/api/groups", {"config": {"name": "ONLY", "strategy": "select"}})
    cfg = st.load()
    conf = outlets._sb_config(cfg, version=(1, 12))
    out = outbound_of(conf)
    check(out.get("type") == "selector", f"类型=手动选择生成 selector：{out.get('type')}")
    check(out.get("default") == "a", f"没固定节点时默认第一台：{out.get('default')}")
    d = post("/api/groups", {"config": {"name": "ONLY", "interval": "3"}})
    check(d["ok"] is False and "10" in d["message"], f"间隔太小被拒：{d.get('message')}")
    d = post("/api/groups", {"config": {"name": "ONLY", "url": "ftp://x"}})
    check(d["ok"] is False and "http" in d["message"], f"非 http 地址被拒：{d.get('message')}")
    # 复原成 urltest，免得影响后面的落盘检查
    post("/api/groups", {"config": {"name": "ONLY", "strategy": "urltest", "interval": 60}})

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
