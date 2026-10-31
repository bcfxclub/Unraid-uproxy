"""全局配置模型与持久化。"""
from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel, Field

from . import settings as S

DEFAULT_MIRRORS: list[str] = [
    "https://docker.xuanyuan.me",
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://hub-mirror.c.163.com",
    "https://docker.mirrors.ustc.edu.cn",
    "https://docker.nju.edu.cn",
    "https://docker.mirrors.sjtug.sjtu.edu.cn",
    "https://mirror.baidubce.com",
]

DEFAULT_NO_PROXY = "localhost,127.0.0.1,::1,.local,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"

OUTLET_MODES = ("none", "manual", "warp", "singbox")


class OutletCfg(BaseModel):
    mode: str = "none"
    http: str = ""
    socks: str = ""

    warp_image: str = "ghcr.io/mon-ius/docker-warp-socks"
    warp_port: int = 9091
    warp_alt_image: str = "ghcr.io/ccbkkb/microwarp:latest"
    # WARP 的 endpoint 域名常被解析到黑洞 IP，填 IP 可强制绕过（走 --add-host）
    warp_endpoint_ip: str = ""

    sb_image: str = "ghcr.io/sagernet/sing-box:latest"
    sb_port: int = 7890
    sb_node: str = ""
    sb_nodes: list[dict[str, Any]] = Field(default_factory=list)
    # 节点显示顺序（测速后按延迟自动排序的结果，低延迟在前、超时垫底）。
    # 订阅重新同步会按来源重排 sb_nodes，所以顺序单独存一份，同步后据此还原。
    sb_order: list[str] = Field(default_factory=list)
    # 被用户停用的节点 name 列表（仍在订阅里，但不进 selector / 不分流）
    sb_disabled: list[str] = Field(default_factory=list)
    # 被用户删除的节点 name 黑名单（订阅里还有，但不再出现在列表/配置里）
    sb_removed: list[str] = Field(default_factory=list)

    # 多订阅源：{id,name,url,ua,proxy,enabled,updated_at,count}
    subscriptions: list[dict[str, Any]] = Field(default_factory=list)

    # 分流模式：auto(默认，国内外分流，保护局域网/国内镜像) / all(全部走代理) / custom(自定义)
    # 仅用于兼容旧配置：一旦 sb_rules 里有内容，就以规则表为唯一事实来源。
    sb_route: str = "auto"
    sb_direct: list[str] = Field(default_factory=list)   # 始终直连的域名/后缀/IP段
    sb_proxy: list[str] = Field(default_factory=list)    # 始终走代理的域名/后缀

    # 分流规则表（可编辑、有序、命中即停）：{id,enabled,match,values,action,note}
    sb_rules: list[dict[str, Any]] = Field(default_factory=list)
    sb_final: str = "direct"      # 全部规则都没命中时的兜底：direct / proxy

    sb_tun: bool = False        # TUN 透明代理（需 NET_ADMIN，影响整台主机出网，默认关）
    sb_auto_update: int = 0     # 订阅自动更新间隔（分钟），0=关闭

    sub_url: str = ""           # 兼容旧版单订阅；迁移后由 subscriptions 驱动
    sub_ua: str = "clash.meta"


class AppCfg(BaseModel):
    version: int = 1
    outlet: OutletCfg = Field(default_factory=OutletCfg)
    targets: dict[str, bool] = Field(
        default_factory=lambda: {
            "dockerd": True,
            "mirror": True,
            "ca": True,
            "system": False,
        }
    )
    mirrors: list[str] = Field(default_factory=lambda: list(DEFAULT_MIRRORS))
    auto_mirror: bool = True
    ca_tunnel: int = 1
    emhttp_proxy: bool = False
    no_proxy: str = DEFAULT_NO_PROXY
    allow_restart_docker: bool = False


_cfg: AppCfg | None = None


def load() -> AppCfg:
    global _cfg
    if _cfg is not None:
        return _cfg
    S.ensure_dirs()
    if os.path.exists(S.STATE_FILE):
        try:
            with open(S.STATE_FILE, "r", encoding="utf-8") as f:
                _cfg = AppCfg.model_validate(json.load(f))
            _migrate(_cfg)
            return _cfg
        except Exception:
            pass
    _cfg = AppCfg()
    save(_cfg)
    return _cfg


def _migrate(cfg: AppCfg) -> None:
    """兼容旧版单订阅字段 sub_url -> 多订阅 subscriptions。"""
    if not cfg.outlet.subscriptions and cfg.outlet.sub_url:
        import time
        cfg.outlet.subscriptions = [{
            "id": f"sub-{int(time.time())}",
            "name": "默认订阅",
            "url": cfg.outlet.sub_url,
            "ua": cfg.outlet.sub_ua or "clash.meta",
            "proxy": "",
            "enabled": True,
            "updated_at": "",
            "count": len(cfg.outlet.sb_nodes),
        }]


def save(cfg: AppCfg) -> None:
    global _cfg
    S.ensure_dirs()
    tmp = str(S.STATE_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg.model_dump(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, S.STATE_FILE)
    _cfg = cfg


def patch(data: dict[str, Any]) -> AppCfg:
    """按前端提交的字段做浅合并更新。"""
    cfg = load()
    for k, v in (data or {}).items():
        if not hasattr(cfg, k):
            continue
        cur = getattr(cfg, k)
        if isinstance(cur, BaseModel) and isinstance(v, dict):
            merged = cur.model_dump()
            merged.update(v)
            setattr(cfg, k, type(cur).model_validate(merged))
        elif isinstance(cur, dict) and isinstance(v, dict):
            merged = dict(cur)
            merged.update(v)
            setattr(cfg, k, merged)
        else:
            setattr(cfg, k, v)
    save(cfg)
    return cfg
