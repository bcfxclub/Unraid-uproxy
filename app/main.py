"""uproxy —— Unraid 出网加速编排工具。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from . import health
from . import host
from . import outlets
from . import persist
from . import probe
from . import registry
from . import settings as S
from . import state as st
from . import stats as stats_mod
from . import subs
from . import targets

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="uproxy", version="1.0.0")


async def guard(request: Request, x_upx_token: str | None = Header(default=None)):
    if not auth.REQUIRED:
        return
    supplied = x_upx_token or request.query_params.get("token") or ""
    if auth.check_session(supplied):
        return
    # 兼容老的「直连口令」方式（显式设置了 UPX_TOKEN 时）
    if auth.LEGACY_TOKEN and secrets.compare_digest(supplied, auth.LEGACY_TOKEN):
        return
    raise HTTPException(status_code=401, detail="未授权")


class ApplyReq(BaseModel):
    keys: list[str] = Field(default_factory=list)
    restart: bool = False
    persist: bool = True


class RestoreReq(BaseModel):
    key: str
    backup: str = ""


class ProbeReq(BaseModel):
    url: str = ""


class SubReq(BaseModel):
    url: str = ""
    ua: str = "clash.meta"
    proxy: str = ""
    raw: str = ""
    nodes: list[dict] = Field(default_factory=list)


class OutletReq(BaseModel):
    action: str = "status"
    node: str = ""


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"ok": False, "message": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# --------------------------------------------------------------------------
# 认证（账号密码登录 / 会话）
# --------------------------------------------------------------------------
@app.get("/api/auth/info", include_in_schema=False)
def api_auth_info():
    return {"ok": True, "required": auth.REQUIRED, "user": auth.USER if auth.REQUIRED else ""}


@app.post("/api/login", include_in_schema=False)
def api_login(data: dict):
    tok = auth.login(str(data.get("username", "")), str(data.get("password", "")))
    if not tok:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"ok": True, "token": tok, "user": auth.USER}


@app.post("/api/logout", dependencies=[Depends(guard)])
def api_logout(x_upx_token: str | None = Header(default=None)):
    auth.logout(x_upx_token)
    return {"ok": True}


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
@app.get("/api/state", dependencies=[Depends(guard)])
def api_state():
    cfg = st.load()
    return {"ok": True, "config": cfg.model_dump(), "probe": probe.snapshot()}


@app.get("/api/outlet/stats", dependencies=[Depends(guard)])
def api_outlet_stats():
    return stats_mod.snapshot()


@app.post("/api/settings", dependencies=[Depends(guard)])
def api_settings(data: dict):
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    if "outlet" in data and isinstance(data["outlet"], dict):
        mode = data["outlet"].get("mode")
        if mode is not None and mode not in st.OUTLET_MODES:
            raise HTTPException(status_code=400, detail=f"出口模式非法: {mode}")
    cfg = st.patch(data)
    return {"ok": True, "config": cfg.model_dump()}


# --------------------------------------------------------------------------
# 下发
# --------------------------------------------------------------------------
@app.get("/api/plan", dependencies=[Depends(guard)])
def api_plan():
    cfg = st.load()
    items = targets.plan(cfg)
    items.append({k: v for k, v in persist.render_go(cfg).items() if k != "_content"})
    return {"ok": True, "plan": items}


@app.post("/api/apply", dependencies=[Depends(guard)])
def api_apply(req: ApplyReq):
    cfg = st.load()
    keys = req.keys or list(targets.RENDERERS)
    res = targets.apply(cfg, keys, restart=req.restart)
    if req.persist:
        res["persist"] = persist.apply_go(cfg)
    return {"ok": True, **res}


@app.get("/api/backups", dependencies=[Depends(guard)])
def api_backups():
    return {"ok": True, "backups": host.list_backups()}


@app.post("/api/restore", dependencies=[Depends(guard)])
def api_restore(req: RestoreReq):
    path = S.resolve(req.key)
    if not path:
        raise HTTPException(status_code=400, detail=f"未知目标: {req.key}")
    bk = req.backup
    if not bk:
        import re as _re
        base = _re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path))
        cands = [b for b in host.list_backups() if b["file"].startswith(base + ".")]
        if not cands:
            raise HTTPException(status_code=404, detail="没有找到该目标的备份")
        bk = cands[0]["path"]
    try:
        host.restore(bk, path)
    except host.HostError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "message": f"已还原 {path}", "backup": bk}


@app.post("/api/uninstall", dependencies=[Depends(guard)])
def api_uninstall():
    return {"ok": True, "go": persist.remove_block(st.load())}


# --------------------------------------------------------------------------
# 体检
# --------------------------------------------------------------------------
@app.get("/api/health", dependencies=[Depends(guard)])
def api_health(proxy: str = "", timeout: int = 12):
    return {"ok": True, **health.compare(proxy=proxy, timeout=timeout)}


@app.post("/api/outlet/probe", dependencies=[Depends(guard)])
def api_outlet_probe(req: ProbeReq):
    return {"ok": True, **health.probe_outlet(req.url)}


@app.get("/api/mirrors/check", dependencies=[Depends(guard)])
def api_mirrors_check(force: bool = False, timeout: int = 8):
    cfg = st.load()
    return {"ok": True, **registry.cached_or_check(cfg.mirrors, force=force)}


@app.post("/api/mirrors/auto", dependencies=[Depends(guard)])
def api_mirrors_auto(limit: int = 3):
    cfg = st.load()
    picked = registry.pick(cfg.mirrors, limit=limit)
    if not picked:
        return {"ok": False, "message": "没有一个镜像源可用，请检查网络或手动指定"}
    cfg.mirrors = picked + [m for m in cfg.mirrors if m not in picked]
    st.save(cfg)
    return {"ok": True, "mirrors": cfg.mirrors, "picked": picked}


# --------------------------------------------------------------------------
# 出口
# --------------------------------------------------------------------------
@app.post("/api/outlet/warp", dependencies=[Depends(guard)])
def api_warp(req: OutletReq):
    cfg = st.load()
    if req.action == "start":
        return {"ok": True, **outlets.warp_start(cfg)}
    if req.action == "stop":
        return {"ok": True, **outlets.warp_stop()}
    if req.action == "test":
        return {"ok": True, **outlets.warp_test(cfg)}
    if req.action == "precheck":
        return {"ok": True, **outlets.warp_precheck(cfg)}
    return {"ok": True, **outlets.warp_status(cfg)}


@app.post("/api/outlet/singbox", dependencies=[Depends(guard)])
def api_singbox(req: OutletReq):
    cfg = st.load()
    if req.node:
        cfg.outlet.sb_node = req.node
        st.save(cfg)
    if req.action == "start":
        try:
            return {"ok": True, **outlets.sb_start(cfg)}
        except ValueError as e:
            return {"ok": False, "message": str(e)}
    if req.action == "stop":
        return {"ok": True, **outlets.sb_stop()}
    return {"ok": True, **outlets.sb_status(cfg)}


# --------------------------------------------------------------------------
# 订阅
# --------------------------------------------------------------------------
@app.post("/api/subs/fetch", dependencies=[Depends(guard)])
def api_subs_fetch(req: SubReq):
    got: dict = {"attempt": 1, "http_code": ""}
    if req.raw.strip():
        parsed = subs.parse(req.raw)
    elif req.url.strip():
        got = subs.fetch(req.url.strip(), req.ua or "clash.meta", proxy=req.proxy or "")
        if not got["ok"]:
            return {"ok": False, "message": got["message"], "http_code": got.get("http_code", ""),
                    "attempt": got.get("attempt", 0)}
        parsed = subs.parse(got["raw"])
    else:
        return {"ok": False, "message": "请填写订阅地址或粘贴订阅内容"}

    cfg = st.load()
    cfg.outlet.sb_nodes = parsed["nodes"]
    if req.url.strip():
        cfg.outlet.sub_url = req.url.strip()
    st.save(cfg)
    retry_note = f"（第 {got['attempt']} 次拉取成功）" if got.get("attempt", 1) > 1 else ""
    return {"ok": True, "message": parsed["message"] + retry_note, "nodes": [
        {"name": n["name"], "type": n["type"], "server": n["server"], "port": n["port"]}
        for n in parsed["nodes"]
    ], "unsupported": parsed["unsupported"], "attempt": got.get("attempt", 1)}


@app.post("/api/subs/test", dependencies=[Depends(guard)])
def api_subs_test(req: SubReq):
    cfg = st.load()
    nodes = req.nodes or cfg.outlet.sb_nodes
    return {"ok": True, "results": subs.test_nodes(nodes)}


@app.post("/api/restart-docker", dependencies=[Depends(guard)])
def api_restart_docker():
    cfg = st.load()
    if not cfg.allow_restart_docker:
        raise HTTPException(status_code=403, detail="未开启「允许重启 Docker」")
    r = targets.restart_docker()
    return {"ok": r["rc"] == 0, "output": (r["out"] or r["err"])[:1500]}
