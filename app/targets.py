"""四个下发目标的 driver。

每个 driver 遵循同一套契约：
    render(cfg) -> {"path", "proposed", "current", "changed", "needs_restart", ...}
写入前统一由 apply() 做备份 + 保留原文件权限。
rc.docker 与 daemon.json 的修改必须幂等——重复应用不得叠加。
"""
from __future__ import annotations

import json
import os
import re

from . import host
from . import persist
from . import settings as S
from . import state as st

ENV_KEYS = (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY",
)

RESTARTER_IMAGE = os.environ.get("UPX_RESTARTER_IMAGE", "alpine:3.20")


# --------------------------------------------------------------------------
# 出口地址解析
# --------------------------------------------------------------------------
def outlet_url(cfg: st.AppCfg) -> str:
    """给 dockerd / 系统环境变量用的正向代理地址（http 优先，兼容 socks5）。"""
    o = cfg.outlet
    if o.mode == "manual":
        return (o.http or o.socks or "").strip()
    if o.mode == "warp":
        return f"http://uproxy-warp:{o.warp_port}"
    if o.mode == "singbox":
        return f"http://uproxy-singbox:{o.sb_port}"
    return ""


def ca_proxy_url(cfg: st.AppCfg) -> str:
    """CA 商店 proxy.cfg 用的地址，socks5 优先（CA 对 socks5 支持更好）。"""
    o = cfg.outlet
    if o.mode == "manual":
        return (o.socks or o.http or "").strip()
    if o.mode == "warp":
        return f"socks5://uproxy-warp:{o.warp_port}"
    if o.mode == "singbox":
        return f"socks5://uproxy-singbox:{o.sb_port}"
    return ""


def _port_of(url: str, default: int = 7890) -> int:
    m = re.search(r":(\d+)\s*$", url.strip())
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return default
    return default


def env_tokens(cfg: st.AppCfg) -> list[str]:
    url = outlet_url(cfg)
    if not url:
        return []
    toks = [f"http_proxy={url}", f"https_proxy={url}"]
    if cfg.no_proxy:
        toks.append(f"no_proxy={cfg.no_proxy}")
    return toks


# --------------------------------------------------------------------------
# rc.docker：给 dockerd 进程单独注入代理
# --------------------------------------------------------------------------
def _find_start_line(lines: list[str]) -> int:
    for i, ln in enumerate(lines):
        if "nohup" in ln and ("$DOCKER" in ln or "dockerd" in ln):
            return i
    for i, ln in enumerate(lines):
        if re.search(r"\bnohup\b", ln):
            return i
    return -1


def _strip_env(tokens: list[str]) -> list[str]:
    return [t for t in tokens if not any(t.startswith(k + "=") for k in ENV_KEYS)]


def _rewrite_line(line: str, envs: list[str]) -> str:
    m = re.match(r"^(\s*nohup\s+)(.*)$", line)
    if not m:
        return line
    prefix, rest = m.group(1), m.group(2)
    tokens = _strip_env(rest.split())
    if tokens and tokens[0] == "env":
        tokens = tokens[1:]
    body = " ".join(tokens)
    if envs:
        body = "env " + " ".join(envs) + " " + body
    body = re.sub(r"\s{2,}", " ", body).rstrip()
    return prefix + body


def render_dockerd(cfg: st.AppCfg) -> dict:
    path = S.resolve("rc_docker")
    cur = host.read(path)
    envs = env_tokens(cfg)
    if not cur:
        return {
            "key": "dockerd", "title": "dockerd 代理", "path": path, "exists": False,
            "changed": False, "current": "", "proposed": "", "needs_restart": False,
            "note": "未找到 rc.docker，请确认挂载 /usr/local/etc/rc.d", "ok": False,
        }
    lines = cur.split("\n")
    idx = _find_start_line(lines)
    if idx < 0:
        return {
            "key": "dockerd", "title": "dockerd 代理", "path": path, "exists": True,
            "changed": False, "current": "", "proposed": "", "needs_restart": False,
            "note": "rc.docker 中未找到 nohup 启动行，已跳过（避免改坏启动脚本）", "ok": False,
        }
    old = lines[idx]
    new = _rewrite_line(old, envs)
    if new == old:
        return {
            "key": "dockerd", "title": "dockerd 代理", "path": path, "exists": True,
            "changed": False, "current": old, "proposed": new, "needs_restart": False,
            "note": "已是目标状态，无需改动", "ok": True,
        }
    proposed_lines = list(lines)
    proposed_lines[idx] = new
    return {
        "key": "dockerd", "title": "dockerd 代理", "path": path, "exists": True,
        "changed": True, "current": old.strip(), "proposed": new.strip(),
        "needs_restart": True, "note": "重启 Docker 服务后生效（会按自动启动设置重启容器）",
        "ok": True, "_content": "\n".join(proposed_lines),
    }


# --------------------------------------------------------------------------
# daemon.json：registry-mirrors
# --------------------------------------------------------------------------
def render_mirror(cfg: st.AppCfg) -> dict:
    path = S.resolve("daemon_json")
    cur = host.read(path)
    mirrors = [m.strip() for m in cfg.mirrors if m and m.strip()]
    if cur.strip():
        try:
            data = json.loads(cur)
        except json.JSONDecodeError as e:
            return {
                "key": "mirror", "title": "镜像加速 registry-mirrors", "path": path,
                "exists": True, "changed": False, "current": cur, "proposed": cur,
                "needs_restart": False, "ok": False,
                "note": f"daemon.json 不是合法 JSON（{e}），已拒绝写入以免覆盖你的配置",
            }
        if not isinstance(data, dict):
            return {
                "key": "mirror", "title": "镜像加速 registry-mirrors", "path": path,
                "exists": True, "changed": False, "current": cur, "proposed": cur,
                "needs_restart": False, "ok": False,
                "note": "daemon.json 顶层不是对象，已拒绝写入",
            }
    else:
        data = {}

    old_mirrors = list(data.get("registry-mirrors") or [])
    if mirrors:
        data["registry-mirrors"] = mirrors
    else:
        data.pop("registry-mirrors", None)

    proposed = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    changed = proposed.strip() != cur.strip()
    return {
        "key": "mirror", "title": "镜像加速 registry-mirrors", "path": path,
        "exists": os.path.exists(path), "changed": changed,
        "current": cur.strip() or "(空)", "proposed": proposed.strip(),
        "needs_restart": changed, "ok": True,
        "note": f"源数量 {len(old_mirrors)} → {len(mirrors)}；重启 Docker 后生效",
        "_content": proposed,
    }


# --------------------------------------------------------------------------
# CA 商店 proxy.cfg
# --------------------------------------------------------------------------
def render_ca(cfg: st.AppCfg) -> dict:
    path = S.resolve("ca_proxy")
    cur = host.read(path)
    url = ca_proxy_url(cfg)
    if url:
        proposed = f"port={_port_of(url)}\ntunnel={cfg.ca_tunnel}\nproxy={url}\n"
    else:
        proposed = ""
    changed = proposed.strip() != cur.strip()
    return {
        "key": "ca", "title": "CA 应用商店代理", "path": path,
        "exists": os.path.exists(path), "changed": changed,
        "current": cur.strip() or "(空/不存在)", "proposed": proposed.strip() or "(清空)",
        "needs_restart": False, "ok": True,
        "note": "改完需重启 Unraid 或重启 emhttp 才生效；浏览器端的图标不走这里",
        "_content": proposed,
    }


# --------------------------------------------------------------------------
# 系统环境变量 /etc/profile.d/uproxy.sh
# --------------------------------------------------------------------------
def render_system(cfg: st.AppCfg) -> dict:
    path = S.resolve("profile_env")
    cur = host.read(path)
    url = outlet_url(cfg)
    if url:
        proposed = (
            f"export http_proxy={_q(url)}\n"
            f"export https_proxy={_q(url)}\n"
            f"export no_proxy={_q(cfg.no_proxy)}\n"
        )
    else:
        proposed = ""
    changed = proposed.strip() != cur.strip()
    return {
        "key": "system", "title": "系统环境变量（wget/git/命令行的出网）", "path": path,
        "exists": os.path.exists(path), "changed": changed,
        "current": cur.strip() or "(空/不存在)", "proposed": proposed.strip() or "(清空)",
        "needs_restart": False, "ok": True,
        "note": "对新开的登录 shell 生效；注意全局代理会影响 PT 等直连业务",
        "_content": proposed,
    }


def _q(v: str) -> str:
    return "'" + v.replace("'", "'\\''") + "'"


def _patch_emhttp(content: str, envs: list[str]) -> str:
    """给 go 里的 emhttp 启动行加代理前缀（仅影响 WebGUI 自身出网，默认关闭）。"""
    out = []
    for ln in content.split("\n"):
        m = re.match(r"^(\s*)(.*)$", ln)
        indent, body = m.group(1), m.group(2)
        hit = re.match(r"^\s*(env\s+)?.*emhttp", body) and "emhttp" in body
        if not hit:
            out.append(ln)
            continue
        if body.startswith("env "):
            toks = _strip_env(body.split()[1:])
        else:
            toks = _strip_env(body.split())
        new_body = " ".join(toks)
        if envs:
            new_body = "env " + " ".join(envs) + " " + new_body
        out.append(indent + re.sub(r"\s{2,}", " ", new_body).rstrip())
    return "\n".join(out)


RENDERERS = {
    "dockerd": render_dockerd,
    "mirror": render_mirror,
    "ca": render_ca,
    "system": render_system,
}

TITLES = {
    "dockerd": "dockerd 代理",
    "mirror": "镜像加速",
    "ca": "CA 商店代理",
    "system": "系统环境变量",
}


def plan(cfg: st.AppCfg) -> list[dict]:
    out = []
    for key, fn in RENDERERS.items():
        item = fn(cfg)
        item["enabled"] = bool(cfg.targets.get(key, False))
        item.pop("_content", None)
        out.append(item)
    return out


def apply(cfg: st.AppCfg, keys: list[str] | None = None, restart: bool = False) -> dict:
    keys = keys or list(RENDERERS)
    results = []
    for key in keys:
        fn = RENDERERS.get(key)
        if not fn:
            continue
        item = fn(cfg)
        content = item.pop("_content", None)
        if not item.get("ok"):
            results.append({**item, "status": "skipped"})
            continue
        if not item.get("changed"):
            results.append({**item, "status": "unchanged"})
            continue
        if content is None:
            results.append({**item, "status": "skipped", "note": "无内容可写"})
            continue
        try:
            bk = host.write_file(item["path"], content)
        except Exception as e:  # noqa: BLE001
            results.append({**item, "status": "error", "note": str(e)})
            continue
        results.append({**item, "status": "written", "backup": bk})

    restarted = False
    need_restart = any(r.get("needs_restart") and r.get("status") == "written" for r in results)
    if restart and need_restart:
        if not cfg.allow_restart_docker:
            results.append(
                {
                    "key": "restart", "title": "重启 Docker 服务", "status": "blocked",
                    "note": "需在设置里勾选「允许重启 Docker」；该操作会按自动启动设置重启所有容器",
                }
            )
        else:
            r = restart_docker()
            restarted = r.get("rc") == 0
            results.append(
                {
                    "key": "restart", "title": "重启 Docker 服务",
                    "status": "ok" if restarted else "error",
                    "note": (r.get("out") or r.get("err") or "")[:500],
                }
            )
    return {"results": results, "need_restart": need_restart, "restarted": restarted}


def restart_docker() -> dict:
    """在宿主机命名空间里重启 Docker 服务（容器内无法直接操作宿主机服务）。"""
    cmd = (
        "docker run --rm --privileged --pid=host --net=host --uts=host "
        f"{RESTARTER_IMAGE} nsenter -t 1 -m -u -n -i -- "
        "/etc/rc.d/rc.docker restart"
    )
    return host.sh(cmd, timeout=180)


# --------------------------------------------------------------------------
# 恢复默认配置：把某个下发目标还原到 uproxy 改动前的状态
# --------------------------------------------------------------------------
def reset_target(key: str, cfg) -> dict:
    """把某个下发目标恢复到 uproxy 改动前的默认状态。

    优先用**最早**的那份备份（也就是 uproxy 第一次写入前、原系统的文件）；
    没有备份说明这文件是 uproxy 新建的，或者只改过一次没留底，这种情况直接
    撤掉 uproxy 自己加的改动即可。
    """
    if key not in S.CANDIDATES:
        raise ValueError(f"未知目标: {key}")
    path = S.resolve(key)
    base = host._safe(os.path.basename(path))         # rc.docker / daemon.json / proxy.cfg / go / uproxy.sh
    cands = [b for b in host.list_backups() if b["file"].startswith(base + ".")]
    if cands:
        cands.sort(key=lambda b: b["mtime"])          # 升序：最旧 = 改动前
        host.restore(cands[0]["path"], path)
        return {"status": "restored", "path": path, "backup": cands[0]["file"],
                "note": "已还原到 uproxy 改动前的最初备份"}

    # 没有备份：撤掉 uproxy 自己写入的内容（按目标类型处理）
    if key == "go_file":
        return persist.remove_block(cfg)
    if key == "daemon_json":
        cur = host.read(path)
        if not cur.strip():
            return {"status": "unchanged", "path": path, "note": "文件为空，无需处理"}
        try:
            data = json.loads(cur)
        except json.JSONDecodeError:
            return {"status": "skipped", "path": path,
                    "note": "daemon.json 不是合法 JSON，请手动删除 registry-mirrors"}
        if not isinstance(data, dict) or "registry-mirrors" not in data:
            return {"status": "unchanged", "path": path, "note": "本来就没有 registry-mirrors"}
        data.pop("registry-mirrors", None)
        host.write_file(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return {"status": "written", "path": path, "note": "已移除 registry-mirrors"}
    if key in ("ca_proxy", "profile_env"):
        if os.path.exists(path):
            host.write_file(path, "")                 # 清空：uproxy 不写即默认
            return {"status": "written", "path": path, "note": "已清空 uproxy 写入的内容"}
        return {"status": "unchanged", "path": path, "note": "文件不存在，无需处理"}
    if key == "rc_docker":
        return _strip_rc_docker(path)
    return {"status": "skipped", "path": path, "note": "暂不支持该目标的恢复"}


def _strip_rc_docker(path: str) -> dict:
    """撤掉 rc.docker 启动行里 uproxy 注入的代理环境变量（build_block 的反操作）。"""
    cur = host.read(path)
    if not cur.strip():
        return {"status": "unchanged", "path": path, "note": "rc.docker 为空"}
    new = cur
    new = re.sub(r"nohup env\s+", "nohup ", new)
    new = re.sub(r"http_proxy=[^\s]*\s*", "", new)
    new = re.sub(r"https_proxy=[^\s]*\s*", "", new)
    new = re.sub(r"no_proxy=[^\s]*\s*", "", new)
    if new == cur:
        return {"status": "unchanged", "path": path, "note": "未发现 uproxy 注入的代理环境变量"}
    host.write_file(path, new)
    return {"status": "written", "path": path, "note": "已移除 uproxy 注入的代理环境变量"}
