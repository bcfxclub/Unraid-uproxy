"""订阅解析：把订阅内容转成 sing-box 出站配置。

支持 vmess / vless / trojan / shadowsocks / hysteria2 / http / socks，
以及 Clash YAML 格式的订阅。ssr 等不再维护的协议会明确标记为不支持。
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import urllib.parse

import yaml

from . import host

SUPPORTED = ("vmess", "vless", "trojan", "ss", "shadowsocks", "hysteria2", "hy2", "http", "socks", "socks5")


def _b64(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    t = s.replace("-", "+").replace("_", "/")
    t += "=" * (-len(t) % 4)
    try:
        return base64.b64decode(t).decode("utf-8", "replace")
    except Exception:
        try:
            return base64.b64decode(s).decode("utf-8", "replace")
        except Exception:
            return ""


FETCH_MARK = "__UPXCODE__"


def _classify(err: str) -> str:
    """把 curl 的原始报错翻译成用户能看懂、能下手排查的原因。"""
    e = (err or "").lower()
    if "could not resolve host" in e or "name or service not known" in e or "resolve" in e:
        return ("DNS 解析失败：容器解析不到该域名。\n"
                "排查：在宿主机执行 `docker exec uproxy getent hosts uuu-eyi.pages.dev`；\n"
                "或改用下面的「通过代理拉取」走已有出口（WARP / 手动节点）。")
    if "connection timed out" in e or "operation timed out" in e or "timeout" in e:
        return ("连接超时：订阅服务器响应慢或网络不通。\n"
                "已自动重试多次仍失败；建议稍后重试，或用「通过代理拉取」走 WARP / 已有节点。")
    if "failed to connect" in e or "connection refused" in e:
        return "连接被拒绝：服务器不在线或端口不对（检查订阅地址是否完整）。"
    if "ssl" in e or "certificate" in e or "handshake" in e:
        return "TLS 握手失败：可能被中间人拦截或证书异常。"
    if "operation not permitted" in e:
        return "权限受限：容器网络被限制（检查 Unraid 防火墙 / 容器网络模式）。"
    if "redirect" in e:
        return "重定向异常：订阅地址被跳转，请确认完整链接。"
    return f"抓取失败：{err[:300]}"


def parse_userinfo(text: str) -> dict:
    """解析订阅响应头 subscription-userinfo，得到流量用量。

    机场普遍按 ``upload=1; download=2; total=100; expire=1730000000`` 的格式给，
    v2rayA / clash 系客户端显示的就是这几个数。拿不到或全是 0 时返回 {}，
    前端据此决定要不要显示这一行（不显示比显示 0 更不误导）。
    """
    val = ""
    for line in (text or "").splitlines():
        if line.lower().startswith("subscription-userinfo:"):
            val = line.split(":", 1)[1].strip()   # 多次重定向时以最后一次为准
    if not val:
        return {}
    got: dict[str, int] = {}
    for part in val.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            got[k.strip().lower()] = int(float(v.strip()))
        except ValueError:
            continue
    used = got.get("upload", 0) + got.get("download", 0)
    total = got.get("total", 0)
    expire = got.get("expire", 0)
    if total <= 0 and used <= 0 and expire <= 0:
        return {}
    return {"used": max(0, used), "total": max(0, total), "expire": max(0, expire)}


def usage_of(url: str, ua: str = "clash.meta", proxy: str = "", timeout: int = 15) -> dict:
    """单独取一次响应头读用量：正文丢弃，失败就返回 {}。

    不复用 ``fetch``（那条路径带重试和正文解析），这里只关心头部，别影响主流程。
    """
    if not url or url.startswith("local://"):
        return {}
    argv = ["curl", "-sSL", "--http1.1", "--connect-timeout", "8",
            "--max-time", str(timeout), "-A", ua or "clash.meta",
            "-D", "-", "-o", os.devnull]
    if proxy:
        argv += ["-x", proxy]
    argv.append(url)
    r = host.run(argv, timeout=timeout + 6)
    if r["rc"] != 0:
        return {}
    return parse_userinfo(r["out"])


def fetch(url: str, ua: str = "clash.meta", timeout: int = 30, proxy: str = "",
          retries: int = 3) -> dict:
    """拉取订阅内容。对不稳定的订阅源做多次重试，并区分失败原因。

    返回 {ok, message, raw, http_code, attempt}。
    """
    if not url:
        return {"ok": False, "message": "订阅地址为空", "raw": "", "http_code": "", "attempt": 0}

    base = [
        "curl", "-sSL", "--http1.1",
        "--connect-timeout", "10",
        "--max-time", str(timeout),
        "-A", ua or "clash.meta",
        "-w", f"\n{FETCH_MARK}%{{http_code}}",
    ]
    last = {"err": "未知错误", "code": ""}
    for attempt in range(1, retries + 1):
        argv = list(base)
        if proxy:
            argv += ["-x", proxy]
        argv.append(url)
        r = host.run(argv, timeout=timeout + 15)
        out = r["out"] or ""
        code = ""
        body = out
        if FETCH_MARK in out:
            body, code = out.rsplit(FETCH_MARK, 1)
            code = code.strip()
        body = body.strip()
        if r["rc"] == 0 and body:
            if code and not code.startswith("2"):
                # 4xx 一般重试无意义（地址错 / 需鉴权），直接返回；5xx 才重试
                if code.startswith("4"):
                    return {"ok": False, "message": f"订阅返回 HTTP {code}（地址错误或需要鉴权）",
                            "raw": "", "http_code": code, "attempt": attempt}
                last = {"err": f"HTTP {code}", "code": code}
                continue
            return {"ok": True, "message": "ok", "raw": body, "http_code": code or "200", "attempt": attempt}
        last = {"err": (r["err"] or "抓取失败"), "code": code}
    return {"ok": False, "message": _classify(last["err"]), "raw": "", "http_code": last["code"], "attempt": retries}


# --------------------------------------------------------------------------
# 单条 URI -> sing-box outbound
# --------------------------------------------------------------------------
def _split_uri(uri: str) -> tuple[str, str, str, dict]:
    uri = uri.strip()
    if "://" not in uri:
        return "", "", "", {}
    scheme, rest = uri.split("://", 1)
    name = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        name = urllib.parse.unquote(frag)
    query: dict[str, str] = {}
    if "?" in rest:
        rest, qs = rest.split("?", 1)
        parsed = urllib.parse.parse_qs(qs)
        query = {k: v[0] for k, v in parsed.items() if v}
    return scheme.lower(), rest, name, query


def _tls_obj(query: dict, host: str = "", default_fp: str = "chrome") -> dict | None:
    sec = (query.get("security") or "").lower()
    if sec not in ("tls", "reality") and not query.get("sni"):
        return None
    t: dict = {
        "enabled": True,
        "server_name": query.get("sni") or query.get("peer") or host or "",
    }
    # 关键默认：TLS 节点**默认放宽证书校验**。
    #
    # 机场下发的 vmess/vless 节点里，SNI（server_name）经常跟实际证书对不上
    # （比如 SNI 是 txb011.xmrhmode.com，但证书只认 ws-aicdn.com），而 URI 里
    # 用户**从不会主动写 allowInsecure=1**。这种节点 sing-box 默认校验证书会直接
    # TLS 握手失败，整条节点全崩、测速全不可用、代理都起不来。
    #
    # 放宽证书校验（insecure: true）后，sing-box 只保留 SNI 匹配，不再校验证书
    # 颁发者/有效期/域名——对「节点能通但 SNI 错位」这种机场常见场景是必须的。
    # 代价是无法防中间人，但代理链路上信任由机场承担，这里优先「能通」。
    #
    # 机场显式写了 allowInsecure=0 才收紧（极少见），否则一律放宽。
    if query.get("allowInsecure", "1") not in ("0", "false", "False"):
        t["insecure"] = True
    fp = query.get("fp") or default_fp
    if fp and fp != "none":
        t["utls"] = {"enabled": True, "fingerprint": fp}
    if sec == "reality":
        t["reality"] = {
            "enabled": True,
            "public_key": query.get("pbk", ""),
            "short_id": query.get("sid", ""),
        }
    # 部分订阅会写 alpn=default / 空值，不是合法 ALPN，必须过滤掉
    vals = [a for a in (query.get("alpn") or "").split(",") if a and a != "default"]
    if vals:
        t["alpn"] = vals
    if not t["server_name"]:
        t.pop("server_name")
    return t


def _transport(query: dict) -> dict | None:
    net = (query.get("type") or query.get("network") or "tcp").lower()
    path = query.get("path") or "/"
    host = query.get("host") or ""
    if net in ("ws", "websocket"):
        tr: dict = {"type": "ws", "path": path}
        if host:
            tr["headers"] = {"Host": host}
        return tr
    if net == "grpc":
        return {"type": "grpc", "service_name": query.get("serviceName") or path}
    if net in ("h2", "http"):
        tr = {"type": "http", "path": path}
        if host:
            tr["host"] = [host]
        return tr
    if net == "httpupgrade":
        tr = {"type": "httpupgrade", "path": path}
        if host:
            tr["headers"] = {"Host": host}
        return tr
    return None


def _vmess(rest: str, name: str) -> dict | None:
    raw = _b64(rest)
    if not raw.startswith("{"):
        return None
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        port = int(d.get("port", 0))
    except (TypeError, ValueError):
        return None
    if not d.get("add") or not port:
        return None
    tag = d.get("ps") or name or f"{d.get('add')}:{port}"
    ob: dict = {
        "type": "vmess",
        "tag": tag,
        "server": d.get("add"),
        "server_port": port,
        "uuid": d.get("id", ""),
        "security": d.get("scy") or "auto",
        "alter_id": int(d.get("aid") or 0),
    }
    net = (d.get("net") or "tcp").lower()
    if net == "ws":
        tr = {"type": "ws", "path": d.get("path") or "/"}
        if d.get("host"):
            tr["headers"] = {"Host": d.get("host")}
        ob["transport"] = tr
    elif net == "grpc":
        ob["transport"] = {"type": "grpc", "service_name": d.get("path") or ""}
    elif net == "h2":
        ob["transport"] = {"type": "http", "path": d.get("path") or "/"}
    if str(d.get("tls", "")).lower() == "tls":
        ob["tls"] = _tls_obj({"security": "tls", "sni": d.get("sni") or d.get("host") or "",
                              "fp": d.get("fp") or "chrome"}, d.get("host") or "")
    return {"name": tag, "type": "vmess", "server": ob["server"], "port": port, "outbound": ob}


def _vless_trojan(scheme: str, rest: str, name: str, query: dict) -> dict | None:
    if "@" not in rest:
        return None
    cred, hostport = rest.rsplit("@", 1)
    if ":" not in hostport:
        return None
    host, _, port_s = hostport.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        return None
    tag = name or f"{host}:{port}"
    if scheme == "vless":
        ob: dict = {"type": "vless", "tag": tag, "server": host, "server_port": port,
                    "uuid": cred, "packet_encoding": "xudp"}
        if query.get("flow"):
            ob["flow"] = query["flow"]
    else:
        ob = {"type": "trojan", "tag": tag, "server": host, "server_port": port,
              "password": urllib.parse.unquote(cred)}
    tls = _tls_obj(query, host)
    if tls:
        ob["tls"] = tls
    tr = _transport(query)
    if tr:
        ob["transport"] = tr
    return {"name": tag, "type": scheme, "server": host, "port": port, "outbound": ob}


def _ss(rest: str, name: str, query: dict) -> dict | None:
    if "@" in rest:
        userinfo, hostport = rest.rsplit("@", 1)
    else:
        dec = _b64(rest)
        if "@" not in dec:
            return None
        userinfo, hostport = dec.rsplit("@", 1)
        userinfo = _b64(userinfo) or userinfo

    if ":" not in hostport:
        return None
    host, _, port_s = hostport.rpartition(":")
    port_s = re.sub(r"\D.*$", "", port_s)
    try:
        port = int(port_s)
    except ValueError:
        return None

    if ":" in userinfo:
        method, password = userinfo.split(":", 1)
    else:
        dec = _b64(userinfo)
        if ":" not in dec:
            return None
        method, password = dec.split(":", 1)

    tag = name or f"{host}:{port}"
    ob: dict = {"type": "shadowsocks", "tag": tag, "server": host, "server_port": port,
                "method": method, "password": password}
    plugin = query.get("plugin")
    if plugin and "obfs" in plugin:
        ob["plugin"] = "obfs"
        ob["plugin_opts"] = query.get("plugin-opts") or ""
    return {"name": tag, "type": "shadowsocks", "server": host, "port": port, "outbound": ob}


def _hysteria2(rest: str, name: str, query: dict) -> dict | None:
    if "@" not in rest:
        return None
    cred, hostport = rest.rsplit("@", 1)
    if ":" not in hostport:
        return None
    host, _, port_s = hostport.rpartition(":")
    try:
        port = int(re.sub(r"\D.*$", "", port_s))
    except ValueError:
        return None
    tag = name or f"{host}:{port}"
    ob: dict = {"type": "hysteria2", "tag": tag, "server": host, "server_port": port,
                "password": urllib.parse.unquote(cred)}
    tls = _tls_obj({"security": "tls", "sni": query.get("sni") or host,
                    "allowInsecure": query.get("insecure") or "1"}, host)
    if tls:
        ob["tls"] = tls
    if query.get("obfs"):
        ob["obfs"] = {"type": query["obfs"], "password": query.get("obfs-password", "")}
    return {"name": tag, "type": "hysteria2", "server": host, "port": port, "outbound": ob}


def _http_socks(scheme: str, rest: str, name: str) -> dict | None:
    user, passwd = "", ""
    if "@" in rest:
        cred, hostport = rest.rsplit("@", 1)
        if ":" in cred:
            user, passwd = cred.split(":", 1)
            user, passwd = urllib.parse.unquote(user), urllib.parse.unquote(passwd)
    else:
        hostport = rest
    if ":" not in hostport:
        return None
    host, _, port_s = hostport.rpartition(":")
    try:
        port = int(re.sub(r"\D.*$", "", port_s))
    except ValueError:
        return None
    tag = name or f"{host}:{port}"
    typ = "http" if scheme == "http" else "socks"
    ob: dict = {"type": typ, "tag": tag, "server": host, "server_port": port}
    if typ == "socks":
        ob["version"] = "5"
    if user:
        ob["username"], ob["password"] = user, passwd
    return {"name": tag, "type": typ, "server": host, "port": port, "outbound": ob}


def _from_clash(doc: dict) -> list[dict]:
    out = []
    for p in (doc.get("proxies") or []):
        t = (p.get("type") or "").lower()
        tag = p.get("name") or f"{p.get('server')}:{p.get('port')}"
        try:
            port = int(p.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        host = p.get("server") or ""
        if not host or not port:
            continue
        if t == "vmess":
            ob = {"type": "vmess", "tag": tag, "server": host, "server_port": port,
                  "uuid": p.get("uuid", ""), "security": p.get("cipher") or "auto",
                  "alter_id": int(p.get("alterId") or 0)}
            if p.get("network") == "ws":
                opts = p.get("ws-opts") or p.get("ws-path") or {}
                tr = {"type": "ws", "path": (opts.get("path") if isinstance(opts, dict) else None) or "/"}
                if isinstance(opts, dict) and (opts.get("headers") or {}).get("Host"):
                    tr["headers"] = {"Host": opts["headers"]["Host"]}
                ob["transport"] = tr
            if p.get("tls") or p.get("servername"):
                ob["tls"] = {"enabled": True, "server_name": p.get("servername") or p.get("sni") or ""}
        elif t == "vless":
            ob = {"type": "vless", "tag": tag, "server": host, "server_port": port,
                  "uuid": p.get("uuid", ""), "packet_encoding": "xudp"}
            if p.get("flow"):
                ob["flow"] = p["flow"]
            if p.get("tls") or p.get("servername"):
                ob["tls"] = {"enabled": True, "server_name": p.get("servername") or p.get("sni") or ""}
        elif t == "trojan":
            ob = {"type": "trojan", "tag": tag, "server": host, "server_port": port,
                  "password": p.get("password", "")}
            if p.get("sni"):
                ob["tls"] = {"enabled": True, "server_name": p.get("sni")}
        elif t in ("ss", "shadowsocks"):
            ob = {"type": "shadowsocks", "tag": tag, "server": host, "server_port": port,
                  "method": p.get("cipher", ""), "password": p.get("password", "")}
        elif t in ("hysteria2", "hy2"):
            ob = {"type": "hysteria2", "tag": tag, "server": host, "server_port": port,
                  "password": p.get("password", "")}
            if p.get("sni"):
                ob["tls"] = {"enabled": True, "server_name": p.get("sni")}
        else:
            continue
        out.append({"name": tag, "type": ob["type"], "server": host, "port": port, "outbound": ob})
    return out


def parse(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {"nodes": [], "unsupported": [], "message": "内容为空"}

    # Clash YAML
    if raw.startswith("{") or "\nproxies:" in raw or raw.startswith("proxies:"):
        try:
            doc = yaml.safe_load(raw)
            if isinstance(doc, dict) and doc.get("proxies"):
                nodes = _from_clash(doc)
                return {"nodes": _dedupe(nodes), "unsupported": [],
                        "message": f"Clash 订阅解析成功，{len(nodes)} 个节点"}
        except Exception:
            pass

    body = raw
    if "://" not in body:
        dec = _b64(body)
        if "://" in dec:
            body = dec

    nodes, unsupported = [], []
    for line in body.replace("\r", "\n").split("\n"):
        line = line.strip()
        if "://" not in line:
            continue
        scheme, rest, name, query = _split_uri(line)
        if scheme not in SUPPORTED:
            unsupported.append(scheme)
            continue
        try:
            if scheme == "vmess":
                n = _vmess(rest, name)
            elif scheme in ("vless", "trojan"):
                n = _vless_trojan(scheme, rest, name, query)
            elif scheme in ("ss", "shadowsocks"):
                n = _ss(rest, name, query)
            elif scheme in ("hysteria2", "hy2"):
                n = _hysteria2(rest, name, query)
            else:
                n = _http_socks(scheme, rest, name)
        except Exception:
            n = None
        if n:
            nodes.append(n)
        else:
            unsupported.append(scheme)

    msg = f"解析 {len(nodes)} 个节点" + (f"，跳过不支持：{','.join(sorted(set(unsupported)))}" if unsupported else "")
    # 内容里没有任何已知节点协议标记 -> 多半是网页/错误页/错误地址
    if not nodes:
        looks_like_sub = any(
            s in raw for s in ("vmess://", "vless://", "trojan://", "ss://",
                               "socks://", "hysteria2://", "hy2://", "proxies:"))
        if not looks_like_sub:
            msg = "订阅内容不是节点配置（看起来像网页或错误页）。请确认订阅地址是否完整/正确。"
    return {
        "nodes": _dedupe(nodes),
        "unsupported": sorted(set(unsupported)),
        "message": msg,
    }


def _dedupe(nodes: list[dict]) -> list[dict]:
    seen: dict[str, int] = {}
    out = []
    for n in nodes:
        tag = n["outbound"].get("tag") or n["name"]
        if tag in seen:
            seen[tag] += 1
            n["outbound"]["tag"] = f"{tag}-{seen[tag]}"
            n["name"] = n["outbound"]["tag"]
        else:
            seen[tag] = 0
        out.append(n)
    return out


def sync_subscriptions(subscriptions: list[dict], ua: str = "clash.meta",
                       previous: list[dict] | None = None,
                       refresh: list[str] | None = None) -> dict:
    """拉取并合并所有「启用」的订阅源。

    返回 {ok, nodes, summary, unsupported}。单个源失败不影响其他源，
    summary 里逐条标注每个源的结果，便于前端展示。

    ``previous`` 传入上一次的节点集：某个源这次拉取失败时，会沿用该源上次的节点，
    避免「网络抖一下就把节点全清空」。只有拉取成功才替换。

    ``refresh`` 只重新联网拉这几个源（给 id / 地址 / 备注名），其余源直接沿用
    ``previous`` 里的节点、不发请求。单独点某个订阅的「更新」时用它：既不会为了
    一个源把全部订阅重下一遍（等十几秒），也不会动到别的订阅的节点。
    """
    summary: list[dict] = []
    merged: list[dict] = []
    unsupported: set[str] = set()
    any_ok = False
    prev = previous or []
    only = None if refresh is None else {str(x).strip() for x in refresh if str(x).strip()}

    def _keep_prev(source_name: str) -> list[dict]:
        return [dict(n) for n in prev if (n.get("source") or "") == source_name]

    for sub in subscriptions:
        if not sub.get("enabled", True):
            summary.append({"name": sub.get("name", ""), "url": sub.get("url", ""),
                            "ok": None, "message": "已禁用", "count": 0})
            continue
        url = (sub.get("url") or "").strip()
        if not url:
            summary.append({"name": sub.get("name", ""), "url": "", "ok": False,
                            "message": "地址为空", "count": 0})
            continue
        # 粘贴型订阅：内容存在 sub["nodes"] 里，不走网络，重新同步时必须保活
        if url.startswith("local://"):
            stored = [n for n in (sub.get("nodes") or [])
                      if isinstance(n, dict) and n.get("outbound")]
            source_name = sub.get("name") or "本地粘贴"
            for n in stored:
                n["source"] = source_name
            merged += stored
            if stored:
                any_ok = True
            summary.append({"name": sub.get("name", ""), "url": url, "ok": bool(stored),
                            "message": f"本地粘贴 {len(stored)} 个节点" if stored else "本地内容为空",
                            "count": len(stored)})
            continue
        # 只更新指定源时，别的源不联网：直接沿用它上次的节点。
        # 有 id 的按 id 认（同一个地址挂两遍也只更新点的那条），早期没 id 的按地址/备注名认
        skip_name = sub.get("name") or url
        if only is not None:
            hit = ((sub.get("id") or "") in only) if sub.get("id") else bool(
                {(sub.get("url") or ""), (sub.get("name") or "")} & only)
            if not hit:
                kept = _keep_prev(skip_name)
                merged += kept
                summary.append({"name": sub.get("name", ""), "url": url, "ok": None,
                                "reused": True, "message": f"沿用上次的 {len(kept)} 个节点",
                                "count": len(kept)})
                continue
        ua_one = sub.get("ua") or ua
        px = sub.get("proxy") or ""
        # 正文和用量头是两次 curl：并发跑，别先等正文再等头（每条订阅省一半时间）
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_usage = pool.submit(usage_of, url, ua_one, px)
            got = fetch(url, ua_one, proxy=px, retries=3)
            usage = {}
            if got["ok"]:
                try:
                    usage = fut_usage.result()
                except Exception:
                    usage = {}
        source_name = sub.get("name") or url
        if not got["ok"]:
            kept = _keep_prev(source_name)
            merged += kept
            msg = got["message"]
            if kept:
                # 拉取失败但有历史节点：保留，避免节点凭空消失
                msg = f"{msg}（沿用上次的 {len(kept)} 个节点）"
            summary.append({"name": sub.get("name", ""), "url": url, "ok": False,
                            "message": msg, "count": len(kept), "stale": bool(kept),
                            "http_code": got.get("http_code", "")})
            continue
        parsed = parse(got["raw"])
        # 给本订阅所有节点打上来源标记，前端按订阅分组/过滤
        for n in parsed["nodes"]:
            n["source"] = source_name
        merged += parsed["nodes"]
        unsupported.update(parsed["unsupported"])
        any_ok = True
        summary.append({"name": sub.get("name", ""), "url": url, "ok": True,
                        "message": parsed["message"], "count": len(parsed["nodes"]),
                        "attempt": got.get("attempt", 1), "usage": usage})
    nodes = _dedupe(merged)
    live = sum(1 for s in summary if s.get("ok"))
    reused = sum(1 for s in summary if s.get("reused"))
    tail = f"来自 {live} 个可用订阅" + (f"，{reused} 个沿用上次" if reused else "")
    return {
        "ok": any_ok,
        "nodes": nodes,
        "summary": summary,
        "unsupported": sorted(unsupported),
        "message": f"合并 {len(nodes)} 个节点（{tail}）",
    }



def to_uri(node: dict) -> str:
    """把内部节点还原成可分享的 URI（vless/vmess/trojan/ss/hysteria2）。

    不支持的协议返回空串，由调用方给出提示。
    """
    ob = node.get("outbound") or {}
    kind = str(ob.get("type") or node.get("type") or "").lower()
    server = ob.get("server") or node.get("server") or ""
    port = ob.get("server_port") or node.get("port") or 0
    label = urllib.parse.quote(str(node.get("name") or ob.get("tag") or f"{server}:{port}"), safe="")
    tls = ob.get("tls") or {}
    tr = ob.get("transport") or {}

    if kind in ("vless", "trojan"):
        q: dict[str, str] = {}
        if tls.get("enabled"):
            real = (tls.get("reality") or {})
            q["security"] = "reality" if real.get("enabled") else "tls"
            if tls.get("server_name"):
                q["sni"] = str(tls["server_name"])
            if tls.get("insecure"):
                q["allowInsecure"] = "1"
            fp = (tls.get("utls") or {}).get("fingerprint")
            if fp:
                q["fp"] = str(fp)
            if tls.get("alpn"):
                q["alpn"] = ",".join(tls["alpn"])
            if real.get("enabled"):
                q["pbk"] = str(real.get("public_key") or "")
                q["sid"] = str(real.get("short_id") or "")
        tt = tr.get("type")
        if tt:
            q["type"] = str(tt)
            if tt in ("ws", "httpupgrade"):
                q["path"] = str(tr.get("path") or "/")
                host = (tr.get("headers") or {}).get("Host")
                if host:
                    q["host"] = str(host)
            elif tt == "grpc":
                q["serviceName"] = str(tr.get("service_name") or "")
            elif tt == "http":
                q["path"] = str(tr.get("path") or "/")
                hosts = tr.get("host") or []
                if hosts:
                    q["host"] = str(hosts[0])
        if kind == "vless":
            q = {"encryption": str(ob.get("encryption") or "none"), **q}
            if ob.get("flow"):
                q["flow"] = str(ob["flow"])
            cred = str(ob.get("uuid") or "")
        else:
            cred = urllib.parse.quote(str(ob.get("password") or ""), safe="")
        qs = urllib.parse.urlencode(q, quote_via=urllib.parse.quote, safe="")
        return f"{kind}://{cred}@{server}:{port}" + (f"?{qs}" if qs else "") + f"#{label}"

    if kind == "vmess":
        v = {
            "v": "2",
            "ps": str(node.get("name") or ""),
            "add": server,
            "port": str(port),
            "id": str(ob.get("uuid") or ""),
            "aid": str(ob.get("alter_id") or 0),
            "scy": str(ob.get("security") or "auto"),
            "net": str(tr.get("type") or "tcp"),
            "type": "none",
            "host": str(((tr.get("headers") or {}).get("Host")) or ""),
            "path": str(tr.get("path") or ""),
            "tls": "tls" if tls.get("enabled") else "",
            "sni": str(tls.get("server_name") or ""),
        }
        blob = json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return "vmess://" + base64.b64encode(blob).decode("ascii")

    if kind == "shadowsocks":
        userinfo = f"{ob.get('method', '')}:{ob.get('password', '')}"
        b = base64.urlsafe_b64encode(userinfo.encode("utf-8")).decode("ascii").rstrip("=")
        return f"ss://{b}@{server}:{port}#{label}"

    if kind == "hysteria2":
        cred = urllib.parse.quote(str(ob.get("password") or ""), safe="")
        q2: dict[str, str] = {}
        if tls.get("server_name"):
            q2["sni"] = str(tls["server_name"])
        if tls.get("insecure"):
            q2["insecure"] = "1"
        if ob.get("obfs"):
            q2["obfs"] = str((ob["obfs"] or {}).get("type") or "")
            q2["obfs-password"] = str((ob["obfs"] or {}).get("password") or "")
        qs2 = urllib.parse.urlencode(q2, quote_via=urllib.parse.quote, safe="")
        return f"hysteria2://{cred}@{server}:{port}" + (f"?{qs2}" if qs2 else "") + f"#{label}"

    return ""
