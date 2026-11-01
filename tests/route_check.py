"""验证分流规则引擎：整理校验、生成 sing-box 规则、命中测试、保存接口。

不需要 Docker：版本探测走不到（本机没有 docker CLI 时默认按 1.11+ 的语法生成），
正好也覆盖了「探测失败」这条分支。

用法：python tests/route_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["UPX_STATE_DIR"] = tempfile.mkdtemp(prefix="upx-route-")
os.environ["UPX_HOST_ROOT"] = os.environ["UPX_STATE_DIR"]

from fastapi.testclient import TestClient  # noqa: E402

from app import main, route, state as st  # noqa: E402

FAIL = []


def ck(name: str, cond: bool, extra: str = "") -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"  -- {extra}" if extra and not cond else ""))
    if not cond:
        FAIL.append(name)


def fresh() -> st.AppCfg:
    cfg = st.AppCfg()
    cfg.outlet.sb_nodes = [
        {"name": "香港 01", "type": "vless", "server": "a", "port": 1,
         "outbound": {"type": "vless", "tag": "香港 01"}},
        {"name": "日本 02", "type": "trojan", "server": "b", "port": 2,
         "outbound": {"type": "trojan", "tag": "日本 02"}},
    ]
    return cfg


# --------------------------------------------------------------------------
print("[1] 规则整理：清洗用户输入")
rules, warn = route.normalize([
    {"match": "domain_suffix", "action": "proxy",
     "values": [" GitHub.com ", "https://docker.io/v2/", "*.pypi.org", "#注释", "github.com"]},
    {"match": "ip_cidr", "action": "direct", "values": ["1.1.1.1", "10.0.0.0/8"]},
    {"match": "port", "action": "direct", "values": ["22"]},
])
ck("网址被清成域名后缀", rules[0]["values"][:3] == ["github.com", "docker.io", "pypi.org"], rules[0]["values"])
ck("重复项被去掉", rules[0]["values"].count("github.com") == 1)
ck("裸 IP 自动补 /32", "1.1.1.1/32" in rules[1]["values"], rules[1]["values"])

for bad, token in [
    ({"match": "domain_regex", "action": "proxy", "values": ["(unclosed"]}, "(unclosed"),
    ({"match": "ip_cidr", "action": "direct", "values": ["999.1.1.1"]}, "999.1.1.1"),
    ({"match": "port", "action": "direct", "values": ["70000"]}, "70000"),
]:
    try:
        route.normalize([bad])
        ck(f"{token} 应被拒", False)
    except ValueError as e:
        ck(f"{token} 会被拒并说明原因", token in str(e), str(e))

# --------------------------------------------------------------------------
print("\n[2] 未保存过规则时，按老配置铺出等价规则")
cfg = fresh()
cfg.outlet.sb_route = "auto"
cfg.outlet.sb_direct = [".cn"]
cfg.outlet.sb_proxy = ["example.com"]
r = route.effective(cfg)
ck("老配置被铺成 3 条（直连 + 内置清单 + 自定义代理）", len(r) == 3, [x["action"] for x in r])
ck("直连在前，且 .cn 规范成 cn", r[0]["action"] == "direct" and r[0]["values"] == ["cn"], r[0])
ck("内置清单也进来了", len(r[1]["values"]) > 10 and "github.com" in r[1]["values"])
ck("兜底走代理（默认全局模式，未命中规则也出网）", route.final_action(cfg) == "proxy")

cfg_direct = fresh()
cfg_direct.outlet.sb_final = "direct"
ck("显式选大陆白名单：兜底是直连", route.final_action(cfg_direct) == "direct")

cfg2 = fresh()
cfg2.outlet.sb_route = "all"
ck("全部代理模式：没有规则、兜底走代理",
   route.effective(cfg2) == [] and route.final_action(cfg2) == "proxy")

# --------------------------------------------------------------------------
print("\n[3] 生成 sing-box 规则（新版 / 老版语法）")
cfg3 = fresh()
cfg3.outlet.sb_rules = [
    {"id": "a", "enabled": True, "match": "domain_suffix", "action": "direct",
     "values": [".cn"]},
    {"id": "b", "enabled": True, "match": "domain_suffix", "action": "block",
     "values": ["doubleclick.net"]},
    {"id": "c", "enabled": False, "match": "domain_suffix", "action": "proxy",
     "values": ["ignored.com"]},
    {"id": "d", "enabled": True, "match": "domain_regex", "action": "node:日本 02",
     "values": [r"^netflix\."]},
]
cfg3.outlet.sb_final = "proxy"
new = route.build(cfg3, modern=True)
old = route.build(cfg3, modern=False)
new_rules = new["route"]["rules"]
ck("固定三条安全规则在最前面",
   new_rules[0] == {"outbound": "direct", "protocol": "dns"}
   and new_rules[1] == {"outbound": "direct", "ip_is_private": True}
   and new_rules[2].get("action") == "sniff", new_rules[:3])
ck("停用的规则没有下发", all("ignored.com" not in str(x) for x in new_rules))
ck("拦截用 action: reject（1.11+）",
   any(x.get("action") == "reject" and x.get("domain_suffix") == ["doubleclick.net"] for x in new_rules))
ck("老版语法退回 block 出站并要求加 block outbound",
   old["need_block"] and any(x.get("outbound") == "block" for x in old["route"]["rules"])
   and not new["need_block"])
ck("指定节点直接指向出站 tag",
   any(x.get("outbound") == "日本 02" for x in new_rules))
ck("兜底跟随设置", new["route"]["final"] == "select")

# --------------------------------------------------------------------------
print("\n[4] 命中测试")
cfg4 = fresh()
cfg4.outlet.sb_rules = [
    {"match": "domain_suffix", "action": "direct", "values": ["github.com"]},
    {"match": "domain_suffix", "action": "proxy", "values": ["google.com"]},
    {"match": "ip_cidr", "action": "block", "values": ["1.2.3.0/24"]},
]
cfg4.outlet.sb_final = "proxy"
ck("后缀命中本域", route.test("github.com", cfg4)["action"] == "direct")
ck("后缀命中子域", route.test("api.github.com", cfg4)["index"] == 1)
ck("不是后缀就不算", route.test("notgithub.com", cfg4)["hit"] is False)
ck("先匹配先生效", route.test("google.com", cfg4)["index"] == 2)
ck("IP 段命中", route.test("1.2.3.4", cfg4)["action"] == "block")
ck("都不中就走兜底", route.test("random.example", cfg4)["action"] == "proxy")
ck("带协议的网址也能测", route.test("https://api.github.com/x", cfg4)["index"] == 1)

# --------------------------------------------------------------------------
print("\n[5] 接口：读取 / 保存 / 用未保存的规则试算")
c = TestClient(main.app)
tok = c.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
H = {"X-UPX-Token": tok}

got = c.get("/api/route", headers=H).json()
ck("GET 返回规则表与类型清单",
   got["ok"] and "rules" in got and len(got["types"]) >= 8 and got["seeded"] is True)

r = c.post("/api/route", json={
    "rules": [{"match": "domain_suffix", "action": "proxy", "values": ["netflix.com"]}],
    "final": "direct",
}, headers=H).json()
ck("保存成功", r["ok"], r.get("message"))
saved = st.load()
ck("落盘到 sb_rules", saved.outlet.sb_rules and saved.outlet.sb_rules[0]["values"] == ["netflix.com"])
ck("保存后不再是 seeded", c.get("/api/route", headers=H).json()["seeded"] is False)

t = c.post("/api/route/test", json={"target": "www.netflix.com"}, headers=H).json()
ck("保存后能命中", t["hit"] and t["action"] == "proxy", t)

t2 = c.post("/api/route/test", json={
    "target": "github.com", "final": "proxy",
    "rules": [{"match": "domain_suffix", "action": "direct", "values": [".cn"]}],
}, headers=H).json()
ck("带未保存规则试算（不影响已存配置）",
   t2["hit"] is False and t2["action"] == "proxy", t2)
ck("试算没写进配置", st.load().outlet.sb_rules[0]["values"] == ["netflix.com"])

bad = c.post("/api/route", json={
    "rules": [{"match": "domain_regex", "action": "proxy", "values": ["(bad"]}],
}, headers=H).json()
ck("非法正则在接口层被拒", bad["ok"] is False and "正" in bad["message"] or "错" in bad["message"], bad)

pv = c.post("/api/route", json={
    "rules": [{"match": "domain_suffix", "action": "proxy", "values": ["a.com"]}],
    "final": "direct", "preview": True,
}, headers=H).json()
ck("预览返回人话与 JSON", pv["ok"] and pv["preview"] and pv["route"]["rules"], pv.get("version"))
ck("预览不落盘", len(st.load().outlet.sb_rules) == 1)

# --------------------------------------------------------------------------
print("\n[6] sing-box 配置能带上规则表")
from app import outlets as ol  # noqa: E402

live = st.load()
live.outlet.sb_nodes = fresh().outlet.sb_nodes
live.outlet.sb_disabled = []
conf = ol._sb_config(live, version=(1, 12))
ck("clash_api 放在 experimental 里（旧版的顶层 api 字段会被 sing-box 拒掉）",
   "experimental" in conf and "api" not in conf)
ck("1.12 用新的 DNS 服务器写法",
   conf["dns"]["servers"][0].get("type") == "udp" and conf["dns"]["servers"][0].get("server") == "223.5.5.5")
ck("route 里有我们的规则",
   any(r.get("domain_suffix") == ["netflix.com"] for r in conf["route"]["rules"]))
conf_old = ol._sb_config(live, version=(1, 10))
ck("1.10 退回旧 DNS 写法", conf_old["dns"]["servers"][0].get("address") == "223.5.5.5")
ck("1.12 必须带 route.default_domain_resolver（否则启动直接 FATAL）",
   conf["route"].get("default_domain_resolver") == "ali", conf["route"].get("default_domain_resolver"))
ck("1.10 不应带 default_domain_resolver（旧版字段不存在会被拒）",
   "default_domain_resolver" not in conf_old["route"])

# --------------------------------------------------------------------------
print("\n[7] 配置落盘到状态目录（进程内启动用，不再 docker cp）")
import json as _json  # noqa: E402
import unittest.mock as mock  # noqa: E402
from app import host as _host, settings as S  # noqa: E402

orig_run = _host.run


def fake_run(argv, **kw):
    # 假装 sing-box check 通过，避免测试机真的要有 sing-box 二进制
    if len(argv) >= 2 and argv[0].endswith("sing-box") and "check" in argv:
        return {"rc": 0, "out": "", "err": ""}
    return orig_run(argv, **kw)


with mock.patch.object(ol, "_sb_ok", lambda: (True, "/usr/local/bin/sing-box")), \
     mock.patch.object(_host, "run", fake_run):
    chk = ol.sb_check(live)
ck("sb_check 通过（check.json 已生成）", chk["ok"], chk.get("message"))
sb_dir = S.STATE_DIR / "singbox"
ck("check.json 落在状态目录下的 singbox 子目录", (sb_dir / "check.json").exists())

# 真正落盘 config.json 并验证内容与路径
path = ol._stage_conf(conf, "config.json")
ck("落盘路径在状态目录的 singbox 下", str(path).startswith(str(sb_dir)), str(path))
data = _json.loads(path.read_text(encoding="utf-8"))
ck("落盘内容含 inbounds / outbounds / route",
   all(k in data for k in ("inbounds", "outbounds", "route")), list(data.keys()))
ck("Clash API 只在 127.0.0.1:9099（不 publish 到宿主）",
   "experimental" in data
   and data["experimental"]["clash_api"]["external_controller"].startswith("127.0.0.1"),
   data.get("experimental"))
ck("监听地址是 0.0.0.0（publish 给宿主，供 OPM 用）",
   any(ib.get("listen") == "0.0.0.0" for ib in data["inbounds"]),
   [ib.get("listen") for ib in data["inbounds"]])

# --------------------------------------------------------------------------
print("\n[8] WARP 配置（wireguard outbound）在 1.12+ 也要补 default_domain_resolver")
_wreg = {"ok": True, "private_key": "x", "address_v4": "172.16.0.2/32",
         "peer_public_key": "y", "server": "162.159.193.1:51820", "reserved": "AAAA"}
with mock.patch.object(ol, "sb_version", lambda *a, **k: (1, 12)):
    w12 = ol._warp_conf(live, _wreg)
ck("1.12 带 dns 块与 default_domain_resolver",
   w12.get("dns") and w12["route"].get("default_domain_resolver") == "ali", w12.get("route"))
ck("WARP 的 DNS 服务器裸 IP 直连、不带 detour（1.12 合法，避免 empty direct FATAL）",
   w12["dns"]["servers"][0].get("detour") in (None, ""), w12["dns"]["servers"][0])
with mock.patch.object(ol, "sb_version", lambda *a, **k: (1, 10)):
    w10 = ol._warp_conf(live, _wreg)
ck("1.10 不带 dns 块与 default_domain_resolver（保持旧版兼容）",
   "dns" not in w10 and "default_domain_resolver" not in w10["route"], w10.get("route"))

print()
if FAIL:
    print(f"✗ {len(FAIL)} 项失败：")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("✓ 全部通过")
