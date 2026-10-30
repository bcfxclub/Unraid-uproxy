"""/boot/config/go 开机重放块。

Unraid 的 /etc 与 /usr/local/etc/rc.d 都在内存里，重启即还原；
必须把配置生成过程写进 U 盘的 go 脚本，开机时重放一次。
整块用标记包裹，重复写入只替换旧块，不会叠加。
"""
from __future__ import annotations

import os

from . import host
from . import settings as S
from . import state as st
from . import targets

BEGIN = "# >>> uproxy managed begin (do not edit) >>>"
END = "# <<< uproxy managed end <<<"


def true_path(key: str) -> str:
    """把容器挂载路径换算成宿主机真实路径。"""
    p = S.resolve(key)
    prefix = S.HOST_ROOT + "/"
    if p.startswith(prefix):
        return "/" + p[len(prefix):]
    return p


def strip_block(content: str) -> str:
    if BEGIN not in content:
        return content
    lines = content.split("\n")
    out, inside = [], False
    for ln in lines:
        if ln.strip() == BEGIN:
            inside = True
            continue
        if ln.strip() == END:
            inside = False
            continue
        if not inside:
            out.append(ln)
    return "\n".join(out).rstrip() + ("\n" if out else "")


def _sed_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("&", "\\&")


def build_block(cfg: st.AppCfg) -> str:
    parts: list[str] = [BEGIN, "# 由 uproxy 自动生成，修改请通过 WebUI，手动改动会在下次应用时被覆盖"]

    if cfg.targets.get("mirror"):
        text = targets.render_mirror(cfg).get("_content") or ""
        if text.strip():
            parts += [
                "mkdir -p /etc/docker",
                "cat > /etc/docker/daemon.json <<'UPXDAEMON'",
                text.rstrip("\n"),
                "UPXDAEMON",
            ]

    if cfg.targets.get("ca"):
        text = targets.render_ca(cfg).get("_content") or ""
        d = os.path.dirname(true_path("ca_proxy"))
        parts += [
            f"mkdir -p {d}",
            f"cat > {true_path('ca_proxy')} <<'UPXCA'",
            text.rstrip("\n"),
            "UPXCA",
        ]

    if cfg.targets.get("system"):
        text = targets.render_system(cfg).get("_content") or ""
        d = os.path.dirname(true_path("profile_env"))
        parts += [
            f"mkdir -p {d}",
            f"cat > {true_path('profile_env')} <<'UPXENV'",
            text.rstrip("\n"),
            "UPXENV",
        ]

    if cfg.targets.get("dockerd"):
        envs = targets.env_tokens(cfg)
        rc = true_path("rc_docker")
        clean = (
            f"sed -i -e 's|http_proxy=[^ ]* ||g' -e 's|https_proxy=[^ ]* ||g' "
            f"-e 's|no_proxy=[^ ]* ||g' -e 's|nohup env[ ]*|nohup |g' {rc}"
        )
        if envs:
            inject = "nohup env " + _sed_escape(" ".join(envs)) + r" \$UNSHARE"
            parts += [
                f"if [ -f {rc} ]; then",
                f"  {clean}",
                f"  sed -i 's|nohup \\$UNSHARE|{inject}|' {rc}",
                "fi",
            ]
        else:
            parts += [
                f"if [ -f {rc} ]; then",
                f"  {clean}",
                "fi",
            ]

    parts.append(END)
    return "\n".join(parts) + "\n"


def render_go(cfg: st.AppCfg) -> dict:
    path = S.resolve("go_file")
    if not os.path.exists(path):
        return {
            "key": "persist", "title": "开机重放 (/boot/config/go)", "path": path,
            "exists": False, "changed": False, "current": "", "proposed": "",
            "ok": False, "note": "未找到 /boot/config/go，请确认挂载 /boot",
        }
    cur = host.read(path)
    base = strip_block(cur)
    block = build_block(cfg)
    if not cfg.targets.get("dockerd") and not cfg.targets.get("mirror") \
            and not cfg.targets.get("ca") and not cfg.targets.get("system"):
        proposed = base
    else:
        proposed = base.rstrip("\n") + "\n\n" + block

    if cfg.emhttp_proxy:
        url = targets.outlet_url(cfg)
        if url:
            envs = [f"http_proxy={url}", f"https_proxy={url}"]
            if cfg.no_proxy:
                envs.append(f"no_proxy={cfg.no_proxy}")
            proposed = targets._patch_emhttp(proposed, envs)

    changed = proposed.strip() != cur.strip()
    return {
        "key": "persist", "title": "开机重放 (/boot/config/go)", "path": path,
        "exists": True, "changed": changed,
        "current": cur[-2000:], "proposed": block,
        "needs_restart": False, "ok": True,
        "note": "重启 Unraid 后自动重建全部配置；卸载时该块会被完整移除",
        "_content": proposed,
    }


def apply_go(cfg: st.AppCfg) -> dict:
    item = render_go(cfg)
    content = item.pop("_content", None)
    if not item.get("ok"):
        return {**item, "status": "skipped"}
    if not item.get("changed"):
        return {**item, "status": "unchanged"}
    try:
        bk = host.write_file(item["path"], content, mode=0o755)
    except Exception as e:  # noqa: BLE001
        return {**item, "status": "error", "note": str(e)}
    return {**item, "status": "written", "backup": bk}


def remove_block(cfg: st.AppCfg) -> dict:
    """卸载：把托管块从 go 文件里完整移除。"""
    path = S.resolve("go_file")
    if not os.path.exists(path):
        return {"status": "skipped", "note": "未找到 /boot/config/go"}
    cur = host.read(path)
    new = strip_block(cur)
    if new.strip() == cur.strip():
        return {"status": "unchanged"}
    bk = host.write_file(path, new, mode=0o755)
    return {"status": "written", "backup": bk, "path": path}
