"""分流规则：一张可编辑、有序、命中即停的规则表 → sing-box route 块。

设计取舍
--------
1. **顺序即配置**：规则从前往后匹配，命中即停，和 sing-box 语义一致。
   所以前端必须能上移 / 下移，否则「顺序」这件事就没法表达。
2. **只用长期稳定的匹配字段**：`geoip` / `geosite` 已在 sing-box 1.12 被移除，
   旧版 `outbound: block` 已在 1.13 被移除。这里只生成
   domain / domain_suffix / domain_keyword / domain_regex / ip_cidr / port /
   network / protocol / process_name，再加 `action: reject`（1.11+）或
   兼容的 block 出站（更老版本），不会因为镜像升级整份配置直接失效。
3. **动作可以指定单个节点**：想「Netflix 走日本节点、GitHub 走美国节点」
   不用再开一套工具，规则里直接选节点即可。
4. **能在本地算命中**：test() 用同一套语义把域名跑一遍，告诉用户命中第几条规则。
   改完规则不用起容器就能验证，这是「改得动、也敢改」的前提。
"""
from __future__ import annotations

import ipaddress
import json
import re
import secrets
from typing import Any

from . import state as st

# --------------------------------------------------------------------------
# 匹配类型
# --------------------------------------------------------------------------
# key 同时就是 sing-box 规则里的字段名，省掉一层映射，少一处出错的机会。
MATCH_TYPES: list[dict[str, str]] = [
    {"key": "domain_suffix", "label": "域名后缀", "kind": "domain",
     "hint": "匹配该域名及其所有子域名，最常用", "ph": "github.com"},
    {"key": "domain", "label": "精确域名", "kind": "domain",
     "hint": "只匹配这个域名本身，不含子域名", "ph": "www.google.com"},
    {"key": "domain_keyword", "label": "域名关键词", "kind": "domain",
     "hint": "域名里包含这个词就命中", "ph": "google"},
    {"key": "domain_regex", "label": "域名正则", "kind": "domain",
     "hint": "RE2 正则，写错会导致配置被拒", "ph": r"(^|\.)openai\.com$"},
    {"key": "ip_cidr", "label": "IP 段", "kind": "ip",
     "hint": "按目标 IP 匹配，单个 IP 会自动补 /32", "ph": "1.1.1.0/24"},
    {"key": "port", "label": "目标端口", "kind": "other",
     "hint": "单个端口号，如 22", "ph": "22"},
    {"key": "network", "label": "网络类型", "kind": "other",
     "hint": "tcp 或 udp", "ph": "udp"},
    {"key": "protocol", "label": "识别协议", "kind": "other",
     "hint": "http / tls / quic / dns，需嗅探", "ph": "dns"},
    {"key": "process_name", "label": "进程名", "kind": "other",
     "hint": "仅 TUN 模式能识别", "ph": "curl"},
]
_MATCH = {m["key"]: m for m in MATCH_TYPES}

# 动作：proxy/direct/block 或 node:<节点名>
ACTIONS = [
    {"key": "proxy", "label": "走代理"},
    {"key": "direct", "label": "直连"},
    {"key": "block", "label": "拦截"},
]

FINAL_CHOICES = [
    {"key": "direct", "label": "直连（黑名单式：只有命中的走代理）"},
    {"key": "proxy", "label": "走代理（白名单式：只有命中的走直连）"},
]

# 内置的「出网加速」域名。首次打开规则页会把它铺成一条可编辑的规则，
# 之后用户改它、删它都行——不再有写死在代码里、界面看不到的东西。
BUILTIN_PROXY_SUFFIX = [
    "docker.io", "registry-1.docker.io", "ghcr.io", "quay.io", "gcr.io",
    "registry.k8s.io", "k8s.gcr.io", "github.com", "githubusercontent.com",
    "githubassets.com", "objects.githubusercontent.com",
    "raw.githubusercontent.com", "codeload.github.com",
    "proxy.golang.org", "sum.golang.org", "pypi.org", "files.pythonhosted.org",
    "registry.npmjs.org", "npmjs.org", "nuget.org", "crates.io",
    "releases.hashicorp.com", "download.docker.com", "cloudflareclient.com",
]

# 常用模板：一键往规则表里塞一组，塞进去之后照样可以逐条改
PRESETS: list[dict[str, Any]] = [
    {
        "key": "cn-direct",
        "label": "国内站点直连",
        "note": "白名单模式下必加：国内的常见站点不由代理绕一圈",
        "rules": [
            {"match": "domain_suffix", "action": "direct", "values": [
                "cn",
                "baidu.com", "bdstatic.com", "bilibili.com", "bilivideo.com", "hdslb.com",
                "qq.com", "qpic.cn", "gtimg.com", "tencent.com", "weixin.com",
                "aliyun.com", "alicdn.com", "alipay.com", "taobao.com", "tmall.com",
                "jd.com", "360buyimg.com", "163.com", "126.net", "netease.com",
                "sina.com.cn", "weibo.com", "weibo.cn", "sohu.com", "zhihu.com", "zhimg.com",
                "douban.com", "douyin.com", "bytedance.com", "toutiao.com", "ixigua.com",
                "kuaishou.com", "meituan.com", "dianping.com", "ctrip.com", "qunar.com",
                "12306.cn", "xiaomi.com", "mi.com", "huawei.com", "honor.com",
                "csdn.net", "cnblogs.com", "gitee.com", "oschina.net", "juejin.cn",
                "cctv.com", "cnzz.com", "umeng.com", "wscdns.com", "deepin.org",
            ]},
            {"match": "domain_suffix", "action": "direct", "values": [
                "gov.cn", "edu.cn", "mil.cn", "ac.cn", "org.cn", "net.cn",
            ]},
        ],
    },
    {
        "key": "docker-dev",
        "label": "开发源加速",
        "note": "docker / GitHub / 各语言包仓库，拉镜像和依赖不再卡",
        "rules": [
            {"match": "domain_suffix", "action": "proxy", "values": list(BUILTIN_PROXY_SUFFIX)},
        ],
    },
    {
        "key": "ai",
        "label": "AI 服务走代理",
        "note": "OpenAI / Claude / Gemini 等国内直连基本不可用",
        "rules": [
            {"match": "domain_suffix", "action": "proxy", "values": [
                "openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com",
                "anthropic.com", "claude.ai", "gemini.google.com", "ai.google.dev",
                "generativelanguage.googleapis.com", "aistudio.google.com",
                "perplexity.ai", "poe.com", "openrouter.ai", "huggingface.co",
                "midjourney.com", "grok.com", "x.ai", "mistral.ai", "together.ai",
                "groq.com", "cursor.com", "cursor.sh", "suno.com", "elevenlabs.io",
            ]},
        ],
    },
    {
        "key": "stream",
        "label": "流媒体走代理",
        "note": "Netflix / YouTube / Spotify 等",
        "rules": [
            {"match": "domain_suffix", "action": "proxy", "values": [
                "netflix.com", "nflxvideo.net", "nflximg.net", "nflxext.com",
                "youtube.com", "youtu.be", "ytimg.com", "googlevideo.com", "ggpht.com",
                "spotify.com", "scdn.co", "twitch.tv", "ttvnw.net", "disneyplus.com",
                "hbomax.com", "max.com", "primevideo.com", "media-amazon.com",
                "vimeo.com", "soundcloud.com", "deezer.com", "tidal.com", "hulu.com",
                "iq.com", "bahamut.akamaized.net",
            ]},
        ],
    },
    {
        "key": "ads",
        "label": "拦截常见广告域名",
        "note": "直接 reject，比走代理更快也更省流量；可能误伤靠广告生存的小站",
        "rules": [
            {"match": "domain_suffix", "action": "block", "values": [
                "doubleclick.net", "googlesyndication.com", "googleadservices.com",
                "adservice.google.com", "2mdn.net", "scorecardresearch.com",
                "adnxs.com", "taboola.com", "outbrain.com", "criteo.com", "criteo.net",
                "adsrvr.org", "rubiconproject.com", "pubmatic.com", "openx.net",
                "casalemedia.com", "moatads.com", "applovin.com",
            ]},
        ],
    },
    {
        "key": "lan",
        "label": "NAS / 局域网不走代理",
        "note": "访问局域网设备时明显更快",
        "rules": [
            {"match": "ip_cidr", "action": "direct", "values": [
                "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                "169.254.0.0/16", "fc00::/7", "fe80::/10",
            ]},
        ],
    },
]


# --------------------------------------------------------------------------
# 规则整理与校验
# --------------------------------------------------------------------------
def new_id() -> str:
    return "r" + secrets.token_hex(3)


def _clean_value(kind: str, raw: str) -> str:
    v = (raw or "").strip()
    if not v or v.startswith("#") or v.startswith("//"):
        return ""
    if kind == "domain":
        v = v.strip().strip("\"'").lower()
        # 容忍用户直接粘网址：https://github.com/xxx -> github.com
        v = re.sub(r"^[a-z]+://", "", v)
        v = v.split("/")[0].split("?")[0].split(":")[0]
        # 容忍 *.example.com / .example.com 这类写错的前缀
        v = re.sub(r"^\*?\.", "", v)
        v = v.strip(".")
    elif kind == "ip":
        v = v.strip().strip("\"'")
        try:
            net = ipaddress.ip_network(v, strict=False)
            v = str(net)
        except ValueError:
            return v  # 原样留着，下面统一报错，别悄悄丢掉用户写的东西
    else:
        v = v.strip().strip("\"'").lower()
    return v


def normalize(rules: list[dict] | None, node_names: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """整理成干净的规则表；返回 (rules, warnings)。硬错误抛 ValueError。"""
    out: list[dict] = []
    warn: list[str] = []
    nodes = set(node_names or [])
    errs: list[str] = []
    for i, r in enumerate(rules or [], 1):
        if not isinstance(r, dict):
            continue
        mt = str(r.get("match") or "").strip()
        if mt not in _MATCH:
            errs.append(f"第 {i} 条：不认识的匹配类型「{mt}」")
            continue
        kind = _MATCH[mt]["kind"]
        raw_vals = r.get("values")
        if isinstance(raw_vals, str):
            # 容错：万一是「一行一个」的整段文本也照收
            raw_vals = re.split(r"[\n,;]+", raw_vals)
        vals: list[str] = []
        for raw in (raw_vals or []):
            v = _clean_value(kind, str(raw))
            if not v:
                continue
            if v in vals:
                continue
            if mt == "domain_regex":
                try:
                    re.compile(v)
                except re.error as e:
                    errs.append(f"第 {i} 条：正则「{v}」写错了（{e}）")
                    continue
            if mt == "ip_cidr":
                try:
                    ipaddress.ip_network(v, strict=False)
                except ValueError:
                    errs.append(f"第 {i} 条：「{v}」不是合法的 IP 或网段")
                    continue
            if mt == "port":
                if not v.isdigit() or not (0 < int(v) < 65536):
                    errs.append(f"第 {i} 条：端口「{v}」应在 1~65535 之间")
                    continue
            if mt == "network" and v not in ("tcp", "udp"):
                errs.append(f"第 {i} 条：网络类型只能是 tcp 或 udp")
                continue
            vals.append(v)
        if not vals:
            warn.append(f"第 {i} 条没有有效内容，已跳过")
            continue
        act = str(r.get("action") or "proxy").strip()
        if act.startswith("node:"):
            name = act[5:]
            if name not in nodes:
                warn.append(f"第 {i} 条指定的节点「{name}」已不存在，改为走当前默认节点")
                act = "proxy"
            else:
                act = "node:" + name
        elif act not in ("proxy", "direct", "block"):
            errs.append(f"第 {i} 条：不认识的动作「{act}」")
            continue
        out.append({
            "id": str(r.get("id") or new_id()),
            "enabled": bool(r.get("enabled", True)),
            "match": mt,
            "values": vals,
            "action": act,
            "note": str(r.get("note") or "").strip(),
        })
    if errs:
        raise ValueError("；".join(errs[:6]))
    return out, warn


def effective(cfg: st.AppCfg) -> list[dict]:
    """当前真正生效的规则表。

    用户还没动过规则页（sb_rules 为空）时，按旧字段和内置清单现场铺一份出来，
    这样升级上来的老配置行为完全不变，而且第一次打开页面就看得见、改得动。
    """
    if cfg.outlet.sb_rules:
        return [dict(r) for r in cfg.outlet.sb_rules]
    mode = (cfg.outlet.sb_route or "auto").lower()
    if mode == "all":
        return []
    seeded: list[dict] = []
    if cfg.outlet.sb_direct:
        seeded.append({"match": "domain_suffix", "action": "direct",
                       "values": list(cfg.outlet.sb_direct), "note": "旧的「始终直连」设置"})
    if mode == "auto":
        seeded.append({"match": "domain_suffix", "action": "proxy",
                       "values": list(BUILTIN_PROXY_SUFFIX), "note": "内置加速清单"})
    if cfg.outlet.sb_proxy:
        seeded.append({"match": "domain_suffix", "action": "proxy",
                       "values": list(cfg.outlet.sb_proxy), "note": "旧的「始终走代理」设置"})
    fixed, _ = normalize(seeded)
    return fixed


def final_action(cfg: st.AppCfg) -> str:
    """未命中任何规则时的兜底动作，是「全局代理模式 / 大陆白名单模式」开关的唯一真相来源。

    历史上这里只在 ``sb_rules`` 非空时才读 ``sb_final``，导致没写自定义规则的用户
    点开「全局代理模式」开关却依然直连——开关形同虚设。现在无论是否写过规则，
    一律以 ``sb_final`` 为准（写错才回退到 proxy），开关始终生效。
    """
    return cfg.outlet.sb_final if cfg.outlet.sb_final in ("direct", "proxy") else "proxy"


# --------------------------------------------------------------------------
# 生成 sing-box route 块
# --------------------------------------------------------------------------
def _tag_for(action: str, select_tag: str = "select") -> str:
    if action.startswith("node:"):
        return action[5:]
    return {"direct": "direct", "proxy": select_tag}.get(action, select_tag)


def build(cfg: st.AppCfg, modern: bool = True, select_tag: str = "select") -> dict:
    """返回 {"route": {...}, "need_block": bool}。

    modern=True 用 sing-box 1.11+ 的 `action: reject`；更老的镜像退回 block 出站。
    """
    rules: list[dict] = []
    # 这两条是安全底线，不给改：DNS 走直连防路由环路，私有网段走直连防断内网
    rules.append({"outbound": "direct", "protocol": "dns"})
    rules.append({"outbound": "direct", "ip_is_private": True})
    if modern:
        rules.append({"action": "sniff", "timeout": "500ms"})

    need_block = False
    for r in effective(cfg):
        if not r.get("enabled", True):
            continue
        vals = r.get("values") or []
        if not vals:
            continue
        act = r.get("action") or "proxy"
        item: dict[str, Any] = {}
        if act == "block":
            if modern:
                item["action"] = "reject"
            else:
                item["outbound"] = "block"
                need_block = True
        else:
            item["outbound"] = _tag_for(act, select_tag)
        item[r["match"]] = list(vals)
        rules.append(item)

    return {
        "route": {
            "rules": rules,
            "final": _tag_for(final_action(cfg), select_tag),
            "auto_detect_interface": True,
        },
        "need_block": need_block,
    }


def describe(cfg: st.AppCfg, modern: bool = True) -> list[str]:
    """把生成的规则说成人话，给「预览」用。"""
    lines: list[str] = []
    lines.append(f"兜底：规则都不命中时 → {'走代理' if final_action(cfg) == 'proxy' else '直连'}")
    lines.append("固定规则 1：DNS 流量 → 直连（防路由环）")
    lines.append("固定规则 2：私有/局域网 IP → 直连")
    if modern:
        lines.append("固定规则 3：嗅探协议与域名（TUN 下域名规则要靠它才生效）")
    n = 0
    for r in effective(cfg):
        if not r.get("enabled", True) or not r.get("values"):
            if not r.get("enabled", True):
                n += 1
                lines.append(f"  #{n} 已停用 → {_zh_action(r.get('action', ''))}")
            continue
        n += 1
        label = _MATCH.get(r["match"], {}).get("label", r["match"])
        vals = r["values"]
        head = "、".join(vals[:4]) + ("…" if len(vals) > 4 else "")
        lines.append(f"  #{n} {label} [{head}] → {_zh_action(r.get('action', ''))}")
    return lines


def _zh_action(a: str) -> str:
    if a.startswith("node:"):
        return f"走节点 {a[5:]}"
    return {"proxy": "走代理", "direct": "直连", "block": "拦截"}.get(a, a)


# --------------------------------------------------------------------------
# 命中测试（离线，按同一套语义算）
# --------------------------------------------------------------------------
def test(target: str, cfg: st.AppCfg) -> dict:
    t = (target or "").strip().lower()
    t = re.sub(r"^[a-z]+://", "", t).split("/")[0]
    if not t:
        raise ValueError("请填写要测试的域名或 IP")
    host, _, port_s = t.partition(":")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None and not re.match(r"^[a-z0-9_.\-\[\]:*]+$", host):
        raise ValueError(f"「{host}」看起来不像域名或 IP")

    rules = effective(cfg)
    for idx, r in enumerate(rules, 1):
        if not r.get("enabled", True):
            continue
        mt = r["match"]
        hit = None
        if ip is None:
            if mt == "domain":
                hit = host in r["values"]
            elif mt == "domain_suffix":
                hit = any(host == v or host.endswith("." + v) for v in r["values"])
            elif mt == "domain_keyword":
                hit = any(v in host for v in r["values"])
            elif mt == "domain_regex":
                try:
                    hit = any(re.search(v, host) for v in r["values"])
                except re.error:
                    hit = False
            elif mt in ("port", "network", "protocol", "process_name"):
                continue
            else:
                continue
        else:
            if mt == "ip_cidr":
                hit = any(_in_net(ip, v) for v in r["values"])
            elif mt in ("domain", "domain_suffix", "domain_keyword", "domain_regex"):
                continue
            else:
                continue
        if hit:
            return {"ok": True, "hit": True, "index": idx, "action": r["action"],
                    "action_text": _zh_action(r["action"]),
                    "match": mt, "label": _MATCH[mt]["label"],
                    "values": r["values"], "note": r.get("note", ""),
                    "message": f"命中第 {idx} 条（{_MATCH[mt]['label']}）→ {_zh_action(r['action'])}"}
    fa = final_action(cfg)
    return {"ok": True, "hit": False, "index": 0, "action": fa,
            "action_text": _zh_action(fa), "match": "", "label": "", "values": [],
            "message": f"没有命中任何规则 → 按兜底设置{_zh_action(fa)}"}


def _in_net(ip, cidr: str) -> bool:
    try:
        return ip in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def dump(cfg: st.AppCfg, modern: bool = True) -> str:
    return json.dumps(build(cfg, modern)["route"], ensure_ascii=False, indent=2)
