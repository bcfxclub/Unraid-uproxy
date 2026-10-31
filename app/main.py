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
from . import groups as groupsm
from . import health
from . import host
from . import log as uplog
from . import nettest
from . import outlets
from . import persist
from . import probe
from . import registry
from . import route as routing
from . import settings as S
from . import state as st
from . import stats as stats_mod
from . import subs
from . import targets

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="uproxy", version="1.0.0")


@app.on_event("startup")
async def _startup() -> None:
    if auth.SOURCE == "default":
        uplog.append("warn", "auth",
                     "正在使用初始密码 admin，任何人都能登录；请到「账号」页立即修改")
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


class RouteReq(BaseModel):
    rules: list[dict] = Field(default_factory=list)
    final: str = "direct"
    restart: bool = False       # 保存后重启 sing-box 让它立即生效
    preview: bool = False       # 只生成不保存


class RouteTestReq(BaseModel):
    target: str = ""
    # 带上规则表示「按页面上还没保存的规则算」，方便改一条就立刻验证
    rules: list[dict] | None = None
    final: str | None = None


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
    ids: list[str] = Field(default_factory=list)   # 批量删除


class NodeReq(BaseModel):
    # None = 本次请求没打算改停用表（避免只传 delete / order 时把停用状态清空）
    disabled: list[str] | None = None
    # None = 没打算改选中节点；"" = 改回自动（延迟最低）
    node: str | None = None
    group: str = ""                                    # 指定分组，空 = 当前分组
    rename: dict = Field(default_factory=dict)   # {from: old_name, to: new_name}
    delete: list[str] = Field(default_factory=list)    # 批量删除节点（进黑名单）
    restore: list[str] = Field(default_factory=list)   # 从黑名单恢复
    order: list[str] | None = None                     # 节点显示顺序（测速排序结果）


class NodeTestReq(BaseModel):
    nodes: list[dict] = Field(default_factory=list)   # 空 = 测全部节点
    mode: str = "auto"      # auto（优先真实 HTTP，环境不支持则降级）/ http / tcp
    url: str = ""           # 自定义测试目标，空 = Google 204
    timeout: int = 5000     # 单节点超时（毫秒）


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
    # 分组是按节点集推导出来的：每次取状态先整理一遍，
    # 老配置没有分组时前端也能立刻看到「PROXY / 全部节点」
    groupsm.ensure(cfg)
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
# 分流规则（可编辑、有序、命中即停）
# --------------------------------------------------------------------------
def _route_node_names(cfg: st.AppCfg) -> list[str]:
    disabled = set(cfg.outlet.sb_disabled or [])
    return [n["name"] for n in cfg.outlet.sb_nodes
            if n.get("outbound") and n["name"] not in disabled]


def _route_state(cfg: st.AppCfg) -> dict:
    return {
        "rules": routing.effective(cfg),
        "final": routing.final_action(cfg),
        "types": routing.MATCH_TYPES,
        "actions": routing.ACTIONS,
        "finals": routing.FINAL_CHOICES,
        "nodes": _route_node_names(cfg),
        "presets": [{"key": p["key"], "label": p["label"], "note": p["note"],
                     "count": sum(len(r["values"]) for r in p["rules"]),
                     "rules": p["rules"]} for p in routing.PRESETS],
        # 还没保存过规则表时，页面上显示的是从老配置推出来的默认值
        "seeded": not bool(cfg.outlet.sb_rules),
    }


@app.get("/api/route", dependencies=[Depends(guard)])
def api_route_get():
    return {"ok": True, **_route_state(st.load())}


@app.post("/api/route", dependencies=[Depends(guard)])
def api_route_save(req: RouteReq):
    cfg = st.load()
    try:
        rules, warn = routing.normalize(req.rules, _route_node_names(cfg))
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    final = req.final if req.final in ("direct", "proxy") else "direct"

    # 用同一份 cfg 结构算预览，保证「看到的」就是「浏览器里存的」
    tmp = cfg.model_copy(deep=True)
    tmp.outlet.sb_rules = rules
    tmp.outlet.sb_final = final
    tmp.outlet.sb_route = "rules"

    if req.preview:
        ver = outlets.sb_version(tmp.outlet.sb_image)
        built = routing.build(tmp, modern=ver >= (1, 11))
        return {"ok": True, "preview": routing.describe(tmp, modern=ver >= (1, 11)),
                "route": built["route"], "warnings": warn,
                "version": ".".join(str(x) for x in ver)}

    cfg.outlet.sb_rules = rules
    cfg.outlet.sb_final = final
    cfg.outlet.sb_route = "rules"   # 之后一律以规则表为准
    st.save(cfg)
    uplog.append("info", "route",
                 f"分流规则已保存：{len(rules)} 条，未命中时"
                 f"{'走代理' if final == 'proxy' else '直连'}")

    msg = f"已保存 {len(rules)} 条规则（{sum(len(r['values']) for r in rules)} 个匹配项）"
    restarted = False
    if req.restart:
        try:
            r = outlets.sb_start(cfg)
        except ValueError as e:
            r = {"ok": False, "message": str(e)}
        restarted = bool(r.get("ok"))
        if restarted:
            msg += "，sing-box 已按新规则重启"
            uplog.append("info", "singbox", msg)
        elif r.get("nodocker"):
            # 本地预览 / 没有 docker 的环境：规则存下来了，别吓唬用户说「配置被拒」
            msg += "；当前环境没有 Docker，无法立刻重启出口（规则已存盘，部署到 Unraid 后生效）"
            uplog.append("warn", "singbox", "分流规则已保存；本机无 docker，未重启 sing-box")
        else:
            msg += f"；但 sing-box 没能重启：{r.get('message', '')}"
            uplog.append("error", "singbox", msg)
    return {"ok": True, "message": msg, "warnings": warn, "rules": rules,
            "restarted": restarted}


@app.post("/api/route/test", dependencies=[Depends(guard)])
def api_route_test(req: RouteTestReq):
    cfg = st.load()
    if req.rules is not None:
        cfg = cfg.model_copy(deep=True)
        try:
            rules, _ = routing.normalize(req.rules, _route_node_names(cfg))
        except ValueError as e:
            return {"ok": False, "message": str(e)}
        cfg.outlet.sb_rules = rules
        cfg.outlet.sb_route = "rules"
        if req.final in ("direct", "proxy"):
            cfg.outlet.sb_final = req.final
    try:
        return {"ok": True, **routing.test(req.target, cfg)}
    except ValueError as e:
        return {"ok": False, "message": str(e)}



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
        try:
            groupsm.set_node(cfg, req.node)
        except ValueError as e:
            return {"ok": False, "message": str(e)}
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
    _apply_order(cfg)
    groupsm.ensure(cfg)
    if req.url.strip():
        cfg.outlet.sub_url = req.url.strip()
    st.save(cfg)
    retry_note = f"（第 {got['attempt']} 次拉取成功）" if got.get("attempt", 1) > 1 else ""
    return {"ok": True, "message": parsed["message"] + retry_note, "nodes": [
        _node_view(n) for n in parsed["nodes"]
    ], "unsupported": parsed["unsupported"], "attempt": got.get("attempt", 1)}


@app.get("/api/nodes/test-targets", dependencies=[Depends(guard)])
def api_node_test_targets():
    """预置的测速目标站点（前端渲染成菜单项）。"""
    return {"ok": True, "targets": nettest.targets(), "default": nettest.DEFAULT_TARGET}


@app.post("/api/subs/test", dependencies=[Depends(guard)])
def api_subs_test(req: NodeTestReq):
    """节点测速。

    ``mode=http`` 才是真的「能不能出去」：会起一个一次性 sing-box，
    让它用每个节点亲自访问 Google/GitHub，量应用层往返耗时；
    ``mode=tcp`` 只做端口握手，快但没有参考价值。
    """
    cfg = st.load()
    # 不指定节点就测「当前代理组」：首页的节点延迟卡片测的就是这一组
    nodes = req.nodes or groupsm.enabled_nodes(cfg)
    r = nettest.test_nodes(nodes, mode=req.mode, url=req.url, timeout_ms=req.timeout)
    up = sum(1 for x in r["results"] if x.get("reachable"))
    total = len(r["results"])
    if total:
        tag = "HTTP 实测" if r["mode"] == "http" else "TCP 握手测速"
        target = r["url"] or "节点端口"
        note = r.get("note") or ""
        # 只有「降级」这种意外才写进日志，正常提示就不占日志行了
        extra = f"；{note}" if "降级" in note else ""
        uplog.append("info" if up else "warn", "nodes",
                     f"{tag} {total} 个节点：{up} 个可用（目标 {target}）{extra}")
    return {"ok": True, **r}


# --------------------------------------------------------------------------
# 订阅管理（多订阅源）
# --------------------------------------------------------------------------
def _apply_order(cfg: st.AppCfg) -> None:
    """按已保存的顺序（``sb_order``）重排 ``sb_nodes``。

    顺序是「测速后按延迟自动排序」的产物：延迟低的在前，超时/失败垫底。
    订阅重新同步会按来源重排节点，所以这里在每次同步后把顺序还原，
    同时顺手清掉已删除节点的名字；``order`` 里没有的新节点保持原有相对
    顺序排在末尾（稳定排序）。
    """
    order = [x for x in (cfg.outlet.sb_order or []) if x]
    if not order:
        return
    rank = {name: i for i, name in enumerate(order)}
    tail = len(order)
    cfg.outlet.sb_nodes.sort(key=lambda n: rank.get(n.get("name", ""), tail))
    cfg.outlet.sb_order = [n.get("name", "") for n in cfg.outlet.sb_nodes]


def _node_view(n: dict) -> dict:
    """统一的节点对外字段（含 source，供前端按订阅分组/过滤）。"""
    return {"name": n["name"], "type": n["type"], "server": n["server"], "port": n["port"],
            "source": n.get("source", "")}


def _resync(cfg: st.AppCfg) -> dict:
    """用当前所有「启用」的订阅源重新拉取并合并节点，写回 sb_nodes。"""
    import time as _t
    res = subs.sync_subscriptions(cfg.outlet.subscriptions, previous=cfg.outlet.sb_nodes)
    removed = set(cfg.outlet.sb_removed or [])
    nodes = [n for n in res["nodes"] if n["name"] not in removed]
    if len(nodes) != len(res["nodes"]):
        res = {**res, "nodes": nodes,
               "message": f"{res['message']}（已排除 {len(res['nodes']) - len(nodes)} 个手动删除的节点）"}
    cfg.outlet.sb_nodes = nodes
    _apply_order(cfg)
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
    groupsm.ensure(cfg)
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
            # 本地粘贴的节点存进订阅里，后续重新同步不会丢
            "nodes": parsed["nodes"],
        }
        cfg.outlet.subscriptions.append(sub)
        # 仅把本批节点并入（不清空其它源），并打上来源标记，方便按订阅删除/分组
        for n in parsed["nodes"]:
            n["source"] = sub["name"]
        cfg.outlet.sb_nodes = subs._dedupe(cfg.outlet.sb_nodes + parsed["nodes"])
        groupsm.ensure(cfg)
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
            # 兼容早期没有 id 的订阅：也接受用地址或备注名定位
            if s.get("id") == req.id or s.get("url") == req.id or s.get("name") == req.id:
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
        if req.proxy:
            target["proxy"] = req.proxy
        if req.enabled is not None:
            target["enabled"] = req.enabled
        # 单纯「停用」一个订阅：本地剔除它带来的节点即可，不必联网重拉其它订阅
        if req.enabled is False and not req.url and not req.name:
            src = target.get("name") or ""
            before = len(cfg.outlet.sb_nodes)
            cfg.outlet.sb_nodes = [n for n in cfg.outlet.sb_nodes
                                   if (n.get("source") or "") != src]
            names = {n["name"] for n in cfg.outlet.sb_nodes}
            if cfg.outlet.sb_node and cfg.outlet.sb_node not in names:
                cfg.outlet.sb_node = ""
            cfg.outlet.sb_disabled = [d for d in cfg.outlet.sb_disabled if d in names]
            groupsm.ensure(cfg)
            st.save(cfg)
            gone = before - len(cfg.outlet.sb_nodes)
            uplog.append("info", "subs", f"停用订阅「{src}」，移除 {gone} 个节点")
            return {"ok": True, "message": f"已停用「{src}」，移除 {gone} 个节点",
                    "subscriptions": cfg.outlet.subscriptions,
                    "nodes": [_node_view(n) for n in cfg.outlet.sb_nodes],
                    "unsupported": []}
        st.save(cfg)
    res = _resync(cfg)
    return {"ok": res["ok"], "message": res["message"], "summary": res["summary"],
            "subscriptions": cfg.outlet.subscriptions,
            "nodes": [_node_view(n) for n in cfg.outlet.sb_nodes],
            "unsupported": res["unsupported"]}


@app.post("/api/subs/del", dependencies=[Depends(guard)])
def api_subs_del(req: SubDelReq):
    cfg = st.load()
    drop = {i for i in ([req.id] if req.id else []) + list(req.ids) if i}
    if not drop:
        raise HTTPException(status_code=400, detail="请指定要删除的订阅")
    # 兼容早期没有 id 的订阅：按名称/地址兜底匹配
    keep, gone = [], []
    for s in cfg.outlet.subscriptions:
        key = s.get("id") or s.get("url") or s.get("name") or ""
        (gone if key in drop else keep).append(s)
    if not gone:
        raise HTTPException(status_code=404, detail="订阅不存在")
    cfg.outlet.subscriptions = keep

    # 删除必须是「即时」的：直接把该订阅带来的节点本地剔除，
    # 不重新联网拉取（否则别的订阅一超时，删除就跟着卡住）。
    gone_src = {(s.get("name") or s.get("url") or "") for s in gone}
    before = len(cfg.outlet.sb_nodes)
    cfg.outlet.sb_nodes = [n for n in cfg.outlet.sb_nodes
                           if (n.get("source") or "") not in gone_src]
    removed = before - len(cfg.outlet.sb_nodes)
    names = {n["name"] for n in cfg.outlet.sb_nodes}
    if cfg.outlet.sb_node and cfg.outlet.sb_node not in names:
        cfg.outlet.sb_node = ""
    cfg.outlet.sb_disabled = [d for d in cfg.outlet.sb_disabled if d in names]
    groupsm.ensure(cfg)
    st.save(cfg)
    uplog.append("info", "subs",
                 f"删除 {len(gone)} 个订阅，同步移除 {removed} 个节点（剩余 {len(keep)} 个订阅）")
    return {"ok": True, "deleted": len(gone), "removed_nodes": removed,
            "message": f"已删除 {len(gone)} 个订阅、{removed} 个节点",
            "subscriptions": cfg.outlet.subscriptions,
            "nodes": [_node_view(n) for n in cfg.outlet.sb_nodes]}


# --------------------------------------------------------------------------
# 代理分组（v2rayA 风格的「代理组」：分组 = 出口可用的节点集合）
# --------------------------------------------------------------------------
def _groups_view(cfg: st.AppCfg) -> dict:
    gs = groupsm.ensure(cfg)
    act = groupsm.active(cfg)
    return {
        "groups": [{"name": g["name"], "count": len(g["nodes"]), "nodes": g["nodes"],
                    "node": g["node"], "auto": not g["node"]} for g in gs],
        "active": act["name"],
        "group_node": act["node"],
        "nodes": [_node_view(n) for n in groupsm.enabled_nodes(cfg)],
        "sb_node": cfg.outlet.sb_node,
    }


class GroupReq(BaseModel):
    active: str = ""
    create: dict = Field(default_factory=dict)    # {name, nodes}
    rename: dict = Field(default_factory=dict)    # {from, to}
    delete: str = ""
    update: dict = Field(default_factory=dict)    # {name, nodes, node}


@app.get("/api/groups", dependencies=[Depends(guard)])
def api_groups_get():
    return {"ok": True, **_groups_view(st.load())}


@app.post("/api/groups", dependencies=[Depends(guard)])
def api_groups_post(req: GroupReq):
    cfg = st.load()
    try:
        if req.create:
            msg = groupsm.create(cfg, req.create.get("name"), req.create.get("nodes"))
        elif req.rename:
            msg = groupsm.rename(cfg, req.rename.get("from"), req.rename.get("to"))
        elif req.delete:
            msg = groupsm.delete(cfg, req.delete)
        elif req.update:
            msg = groupsm.update(cfg, req.update.get("name"),
                                 req.update.get("nodes"), req.update.get("node"))
        elif req.active:
            msg = groupsm.set_active(cfg, req.active)
        else:
            return {"ok": False, "message": "没有指定要做什么（active / create / rename / delete / update）"}
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    groupsm.ensure(cfg)
    st.save(cfg)
    uplog.append("info", "nodes", msg)
    # 分组变了 = 出口用的节点集变了，正在跑的 sing-box 要重启才生效
    need = False
    try:
        need = cfg.outlet.mode == "singbox" and bool(outlets.sb_status(cfg).get("running"))
    except Exception:
        need = False
    return {"ok": True, "message": msg, "need_restart": need, **_groups_view(cfg)}


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
            if cfg.outlet.sb_disabled:
                cfg.outlet.sb_disabled = [to if d == frm else d for d in cfg.outlet.sb_disabled]
            cfg.outlet.sb_removed = [to if d == frm else d for d in cfg.outlet.sb_removed]
            cfg.outlet.sb_order = [to if d == frm else d for d in cfg.outlet.sb_order]
            # 分组里引用的也是节点名，一起改名
            for g in cfg.outlet.sb_groups:
                g["nodes"] = [to if x == frm else x for x in (g.get("nodes") or [])]
                if g.get("node") == frm:
                    g["node"] = to
    if req.delete:
        drop = {d for d in req.delete if d}
        cfg.outlet.sb_nodes = [n for n in cfg.outlet.sb_nodes if n["name"] not in drop]
        # 记进黑名单，订阅再次同步时不会复活
        cfg.outlet.sb_removed = list(dict.fromkeys(list(cfg.outlet.sb_removed) + sorted(drop)))
        cfg.outlet.sb_disabled = [d for d in cfg.outlet.sb_disabled if d not in drop]
        if cfg.outlet.sb_node in drop:
            cfg.outlet.sb_node = ""
        uplog.append("info", "subs", f"删除 {len(drop)} 个节点（{','.join(sorted(drop)[:3])}"
                     + ("…）" if len(drop) > 3 else "）"))
    if req.restore:
        back = {d for d in req.restore if d}
        cfg.outlet.sb_removed = [d for d in cfg.outlet.sb_removed if d not in back]
        st.save(cfg)
        res = _resync(cfg)
        uplog.append("info", "subs", f"恢复 {len(back)} 个节点")
        return {"ok": True, "message": res["message"], "nodes": [
            {**_node_view(n), "disabled": n["name"] in cfg.outlet.sb_disabled}
            for n in cfg.outlet.sb_nodes
        ], "sb_node": cfg.outlet.sb_node, "sb_disabled": cfg.outlet.sb_disabled,
            "sb_removed": cfg.outlet.sb_removed}
    if req.order is not None:
        # 测速排序结果：只改显示顺序，不动节点本身
        cfg.outlet.sb_order = [x for x in req.order if x]
    if req.disabled is not None:
        cfg.outlet.sb_disabled = [d for d in req.disabled if d]
    if req.node is not None:
        # 选中节点现在挂在分组上："" = 分组内自动（延迟最低）
        try:
            groupsm.set_node(cfg, req.node, req.group)
        except ValueError as e:
            return {"ok": False, "message": str(e)}
    _apply_order(cfg)
    groupsm.ensure(cfg)
    st.save(cfg)
    return {"ok": True, **_groups_view(cfg), "nodes": [
        {**_node_view(n), "disabled": n["name"] in cfg.outlet.sb_disabled}
        for n in cfg.outlet.sb_nodes
    ], "sb_node": cfg.outlet.sb_node, "sb_disabled": cfg.outlet.sb_disabled,
        "sb_removed": cfg.outlet.sb_removed}


class ShareReq(BaseModel):
    names: list[str] = Field(default_factory=list)


@app.post("/api/nodes/share", dependencies=[Depends(guard)])
def api_nodes_share(req: ShareReq):
    """把选中节点导出成分享链接（一行一个）。"""
    cfg = st.load()
    want = {n for n in req.names if n}
    if not want:
        raise HTTPException(status_code=400, detail="请选择节点")
    found = [n for n in cfg.outlet.sb_nodes if n["name"] in want]
    if not found:
        raise HTTPException(status_code=404, detail="节点不存在（可能已被删除）")
    items, un = [], []
    for n in found:
        uri = subs.to_uri(n)
        if uri:
            items.append({"name": n["name"], "uri": uri})
        else:
            un.append(n["name"])
    text = "\n".join(i["uri"] for i in items)
    return {"ok": True, "items": items, "text": text, "unsupported": un,
            "message": f"导出 {len(items)} 条链接" + (f"；{len(un)} 个协议暂不支持" if un else "")}


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
