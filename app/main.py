"""uproxy —— Unraid 出网加速编排工具。"""
from __future__ import annotations

import asyncio
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
from . import log as uplog
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


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_auto_update_loop())


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


class SubAddReq(BaseModel):
    name: str = ""
    url: str = ""
    ua: str = "clash.meta"
    proxy: str = ""
    enabled: bool = True
    raw: str = ""        # 直接粘贴的内容（与 url 二选一）


class SubUpdateReq(BaseModel):
    id: str = ""          # 空 = 更新所有启用源
    name: str = ""
    url: str = ""
    ua: str = ""
    proxy: str = ""
    enabled: bool | None = None   # 仅改启用状态时可传


class SubDelReq(BaseModel):
    id: str = ""


class NodeReq(BaseModel):
    disabled: list[str] = Field(default_factory=list)
    node: str = ""
    rename: dict = Field(default_factory=dict)   # {from: old_name, to: new_name}


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
    return {
        "ok": True,
        "required": auth.REQUIRED,
        "user": auth.USER if auth.REQUIRED else "",
        "source": auth.SOURCE,          # file（界面改过）/ env（环境变量）/ open（未设密码）
        "min_len": auth.MIN_LEN,
    }


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


class PassReq(BaseModel):
    old: str = ""        # 已启用登录时必填，用于校验是本人操作
    user: str = ""       # 不传 = 沿用当前用户名
    password: str = ""


@app.post("/api/auth/password", dependencies=[Depends(guard)])
def api_auth_password(req: PassReq):
    """改用户名/密码。未启用登录（open）时可直接设置，作为首次加固入口。"""
    if auth.REQUIRED and not auth.verify(req.old):
        raise HTTPException(status_code=400, detail="原密码不正确")
    try:
        tok = auth.change(req.user or auth.USER, req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    uplog.append("warn", "auth", f"账号凭据已更新（用户 {auth.USER}），旧会话全部失效")
    return {"ok": True, "token": tok, "user": auth.USER, "source": auth.SOURCE}


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


# --------------------------------------------------------------------------
# 日志（仿 v2rayA「日志」页）
# --------------------------------------------------------------------------
@app.get("/api/logs", dependencies=[Depends(guard)])
def api_logs(since: int = 0, level: str = "", limit: int = 200):
    """前端增量拉取：传上次最后一条的 seq，取之后的日志；seq=0 取最近 limit 条。"""
    return {"ok": True, "last_seq": uplog.last_seq(), "logs": uplog.since(since, level, limit)}


@app.get("/api/logs/scopes", dependencies=[Depends(guard)])
def api_logs_scopes():
    return {"ok": True, "scopes": uplog.scopes()}


@app.get("/api/logs/export", dependencies=[Depends(guard)])
def api_logs_export(level: str = "", limit: int = 2000):
    text = uplog.export_text(level, limit)
    import time as _t
    return JSONResponse(
        content={"ok": True, "text": text},
        headers={
            "Content-Disposition": f"attachment; filename=uproxy-logs-{int(_t.time())}.txt",
            "Cache-Control": "no-store",
        },
    )


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
        r = {"ok": True, **outlets.warp_start(cfg)}
        uplog.append("info" if r["ok"] else "error", "warp", f"启动 WARP：{r.get('message', '')}")
        return r
    if req.action == "stop":
        r = {"ok": True, **outlets.warp_stop()}
        uplog.append("info", "warp", "已停止 WARP")
        return r
    if req.action == "test":
        r = {"ok": True, **outlets.warp_test(cfg)}
        uplog.append("info", "warp", f"WARP 验证：出口 IP {r.get('ip') or '-'}，warp={r.get('warp')}")
        return r
    if req.action == "precheck":
        r = {"ok": True, **outlets.warp_precheck(cfg)}
        uplog.append("info" if r.get("ok") else "warn", "warp",
                     f"WARP 预检：{r.get('verdict') or ('可用' if r.get('ok') else '不建议使用')}")
        return r
    return {"ok": True, **outlets.warp_status(cfg)}


@app.post("/api/outlet/singbox", dependencies=[Depends(guard)])
def api_singbox(req: OutletReq):
    cfg = st.load()
    if req.node:
        cfg.outlet.sb_node = req.node
        st.save(cfg)
    if req.action == "start":
        try:
            r = {"ok": True, **outlets.sb_start(cfg)}
            uplog.append("info", "singbox", f"启动 sing-box（节点 {cfg.outlet.sb_node or '-'}）：{r.get('message', '')}")
            return r
        except ValueError as e:
            uplog.append("error", "singbox", f"启动 sing-box 失败：{e}")
            return {"ok": False, "message": str(e)}
    if req.action == "stop":
        r = {"ok": True, **outlets.sb_stop()}
        uplog.append("info", "singbox", "已停止 sing-box")
        return r
    return {"ok": True, **outlets.sb_status(cfg)}


# --------------------------------------------------------------------------
# 订阅
# --------------------------------------------------------------------------
@app.post("/api/subs/fetch", dependencies=[Depends(guard)])
def api_subs_fetch(req: SubReq):
    got: dict = {"attempt": 1, "http_code": ""}
    if req.raw.strip():
        parsed = subs.parse(req.raw)
        uplog.append("info", "subs", f"解析粘贴订阅内容：{parsed['message']}")
    elif req.url.strip():
        uplog.append("debug", "subs", f"拉取订阅 {req.url.strip()}")
        got = subs.fetch(req.url.strip(), req.ua or "clash.meta", proxy=req.proxy or "")
        if not got["ok"]:
            uplog.append("error", "subs", f"拉取订阅失败：{got['message']}")
            return {"ok": False, "message": got["message"], "http_code": got.get("http_code", ""),
                    "attempt": got.get("attempt", 0)}
        parsed = subs.parse(got["raw"])
        uplog.append("info", "subs", f"拉取订阅成功：{parsed['message']}"
                  + (f"（第 {got['attempt']} 次）" if got.get("attempt", 1) > 1 else ""))
    else:
        return {"ok": False, "message": "请填写订阅地址或粘贴订阅内容"}

    cfg = st.load()
    cfg.outlet.sb_nodes = parsed["nodes"]
    if req.url.strip():
        cfg.outlet.sub_url = req.url.strip()
    st.save(cfg)
    retry_note = f"（第 {got['attempt']} 次拉取成功）" if got.get("attempt", 1) > 1 else ""
    return {"ok": True, "message": parsed["message"] + retry_note, "nodes": [
        _node_view(n) for n in parsed["nodes"]
    ], "unsupported": parsed["unsupported"], "attempt": got.get("attempt", 1)}


@app.post("/api/subs/test", dependencies=[Depends(guard)])
def api_subs_test(req: SubReq):
    cfg = st.load()
    nodes = req.nodes or cfg.outlet.sb_nodes
    return {"ok": True, "results": subs.test_nodes(nodes)}


# --------------------------------------------------------------------------
# 订阅管理（多订阅源）
# --------------------------------------------------------------------------
def _node_view(n: dict) -> dict:
    """统一的节点对外字段（含 source，供前端按订阅分组/过滤）。"""
    return {"name": n["name"], "type": n["type"], "server": n["server"], "port": n["port"],
            "source": n.get("source", "")}


def _resync(cfg: st.AppCfg) -> dict:
    """用当前所有「启用」的订阅源重新拉取并合并节点，写回 sb_nodes。"""
    import time as _t
    res = subs.sync_subscriptions(cfg.outlet.subscriptions)
    cfg.outlet.sb_nodes = res["nodes"]
    # 回填每个源的 count / updated_at / 最近结果
    by_url = {s.get("url"): s for s in cfg.outlet.subscriptions}
    now = _t.strftime("%Y-%m-%d %H:%M", _t.localtime())
    for s in res["summary"]:
        sub = by_url.get(s.get("url"))
        if sub is not None:
            sub["count"] = s.get("count", 0)
            if s.get("ok"):
                sub["updated_at"] = now
    # 清掉已不存在于新节点集的默认节点引用
    names = {n["name"] for n in res["nodes"]}
    if cfg.outlet.sb_node and cfg.outlet.sb_node not in names:
        cfg.outlet.sb_node = ""
    if cfg.outlet.sb_disabled:
        cfg.outlet.sb_disabled = [d for d in cfg.outlet.sb_disabled if d in names]
    st.save(cfg)
    # 汇总日志
    ok_names = [s.get("name") for s in res["summary"] if s.get("ok")]
    fail_names = [s.get("name") for s in res["summary"] if s.get("ok") is False]
    uplog.append("info" if res["ok"] else "warn", "subs",
                 f"同步订阅：合并 {len(res['nodes'])} 个节点"
                 + (f"（{len(ok_names)} 个源成功）" if ok_names else "")
                 + (f"；失败：{','.join(fail_names)}" if fail_names else ""))
    return res


@app.get("/api/subs", dependencies=[Depends(guard)])
def api_subs_list():
    cfg = st.load()
    return {"ok": True, "subscriptions": cfg.outlet.subscriptions,
            "nodes": len(cfg.outlet.sb_nodes)}


@app.post("/api/subs/add", dependencies=[Depends(guard)])
def api_subs_add(req: SubAddReq):
    import time as _t
    cfg = st.load()
    name = req.name.strip()
    if req.raw.strip():
        # 直接粘贴内容：本地解析，不抓网络
        parsed = subs.parse(req.raw)
        sub = {
            "id": f"sub-{int(_t.time()*1000)}",
            "name": name or "本地粘贴",
            "url": "local://pasted",
            "ua": req.ua or "clash.meta",
            "proxy": req.proxy or "",
            "enabled": req.enabled,
            "updated_at": _t.strftime("%Y-%m-%d %H:%M", _t.localtime()),
            "count": len(parsed["nodes"]),
        }
        cfg.outlet.subscriptions.append(sub)
        # 仅把本批节点并入（不清空其它源）
        cfg.outlet.sb_nodes = subs._dedupe(cfg.outlet.sb_nodes + parsed["nodes"])
        st.save(cfg)
        return {"ok": True, "message": parsed["message"], "summary": [{"name": sub["name"], "ok": True, "count": len(parsed["nodes"])}],
                "subscriptions": cfg.outlet.subscriptions,
                "nodes": [_node_view(n) for n in cfg.outlet.sb_nodes],
                "unsupported": parsed["unsupported"]}
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="请填写订阅地址或粘贴内容")
    sub = {
        "id": f"sub-{int(_t.time()*1000)}",
        "name": name or req.url.strip(),
        "url": req.url.strip(),
        "ua": req.ua or "clash.meta",
        "proxy": req.proxy or "",
        "enabled": req.enabled,
        "updated_at": "",
        "count": 0,
    }
    cfg.outlet.subscriptions.append(sub)
    res = _resync(cfg)
    return {"ok": res["ok"], "message": res["message"], "summary": res["summary"],
            "subscriptions": cfg.outlet.subscriptions,
            "nodes": [_node_view(n) for n in cfg.outlet.sb_nodes],
            "unsupported": res["unsupported"]}


@app.post("/api/subs/update", dependencies=[Depends(guard)])
def api_subs_update(req: SubUpdateReq):
    cfg = st.load()
    if req.id:
        target = None
        for s in cfg.outlet.subscriptions:
            if s.get("id") == req.id:
                target = s
                break
        if target is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        # 应用字段修改（编辑 / 启用开关）
        if req.name:
            target["name"] = req.name
        if req.url:
            target["url"] = req.url
        if req.ua:
            target["ua"] = req.ua
        if req.proxy is not None:
            target["proxy"] = req.proxy
        if req.enabled is not None:
            target["enabled"] = req.enabled
        st.save(cfg)
    res = _resync(cfg)
    return {"ok": res["ok"], "message": res["message"], "summary": res["summary"],
            "subscriptions": cfg.outlet.subscriptions,
            "nodes": [_node_view(n) for n in cfg.outlet.sb_nodes],
            "unsupported": res["unsupported"]}


@app.post("/api/subs/del", dependencies=[Depends(guard)])
def api_subs_del(req: SubDelReq):
    cfg = st.load()
    before = len(cfg.outlet.subscriptions)
    cfg.outlet.subscriptions = [s for s in cfg.outlet.subscriptions if s.get("id") != req.id]
    if len(cfg.outlet.subscriptions) == before:
        raise HTTPException(status_code=404, detail="订阅不存在")
    res = _resync(cfg)
    return {"ok": True, "message": res["message"],
            "subscriptions": cfg.outlet.subscriptions,
            "nodes": [_node_view(n) for n in cfg.outlet.sb_nodes]}


# --------------------------------------------------------------------------
# 节点管理（启停 / 选默认 / 重命名）
# --------------------------------------------------------------------------
@app.post("/api/nodes", dependencies=[Depends(guard)])
def api_nodes(req: NodeReq):
    cfg = st.load()
    if req.rename:
        frm = req.rename.get("from", "")
        to = (req.rename.get("to") or "").strip()
        if frm and to:
            for n in cfg.outlet.sb_nodes:
                if n["name"] == frm:
                    n["name"] = to
                    if n.get("outbound"):
                        n["outbound"]["tag"] = to
            if cfg.outlet.sb_node == frm:
                cfg.outlet.sb_node = to
            cfg.outlet.sb_disabled = [to if d == frm else d for d in cfg.outlet.sb_disabled]
    if req.disabled is not None:
        cfg.outlet.sb_disabled = [d for d in req.disabled if d]
    if req.node:
        cfg.outlet.sb_node = req.node
    st.save(cfg)
    return {"ok": True, "nodes": [
        {**_node_view(n), "disabled": n["name"] in cfg.outlet.sb_disabled}
        for n in cfg.outlet.sb_nodes
    ], "sb_node": cfg.outlet.sb_node, "sb_disabled": cfg.outlet.sb_disabled}


# --------------------------------------------------------------------------
# 订阅自动更新（后台循环）
# --------------------------------------------------------------------------
import time as _time

_last_auto = 0.0


async def _auto_update_loop() -> None:
    global _last_auto
    while True:
        await asyncio.sleep(30)
        try:
            cfg = st.load()
            mins = cfg.outlet.sb_auto_update or 0
            if mins <= 0:
                continue
            now = _time.time()
            if now - _last_auto < mins * 60:
                continue
            _last_auto = now
            if not any(s.get("enabled", True) for s in cfg.outlet.subscriptions):
                continue
            res = _resync(cfg)
            if res["ok"]:
                print(f"[auto-update] 订阅已刷新：{res['message']}")
        except Exception as e:  # noqa: BLE001
            print(f"[auto-update] 失败：{e}")


@app.post("/api/restart-docker", dependencies=[Depends(guard)])
def api_restart_docker():
    cfg = st.load()
    if not cfg.allow_restart_docker:
        raise HTTPException(status_code=403, detail="未开启「允许重启 Docker」")
    r = targets.restart_docker()
    return {"ok": r["rc"] == 0, "output": (r["out"] or r["err"])[:1500]}
