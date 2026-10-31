"""代理分组：把节点分成若干组，只有「当前分组」参与出口选择。

为什么要单开一层
----------------
节点一多（几十上百个）就没法在一个下拉里挑；实际使用里「香港一组 / 备用一组 /
只留测速好的一组」这种划分是刚需，也是 v2rayA 里「代理组」的含义。

这里刻意做得很薄：分组只影响「出口用哪些节点」。sing-box 里只有当前分组的节点
会被收进 selector；分组内没指定节点时（``node == ""``）改用 urltest 出站，
由 sing-box 自己挑延迟最低的那个 —— 这样「自动（延迟最低）」是真在自动，不是标签。

``sb_node`` 保留为「当前分组选中的节点」的镜像：旧配置、规则里的 ``node:<名字>``
动作、以及各处「默认节点」的判断都还在读它，不必全改一遍。
"""
from __future__ import annotations

from typing import Any

DEFAULT_NAME = "PROXY"
MAX_NAME = 20
MAX_GROUPS = 16


def _clean(name: Any) -> str:
    return " ".join(str(name or "").split())[:MAX_NAME]


def enabled_nodes(cfg) -> list[dict]:
    """当前真正可用的节点：有出站配置、没被停用。"""
    dis = set(cfg.outlet.sb_disabled or [])
    return [n for n in cfg.outlet.sb_nodes
            if n.get("outbound") and n.get("name") and n.get("name") not in dis]


def names(cfg) -> list[str]:
    return [n["name"] for n in enabled_nodes(cfg)]


def active(cfg) -> dict:
    """当前分组（找不到就退回第一个；一个都没有时给个空壳）。"""
    groups = cfg.outlet.sb_groups or []
    for g in groups:
        if g.get("name") == cfg.outlet.sb_group:
            return g
    if groups:
        return groups[0]
    return {"name": DEFAULT_NAME, "nodes": [], "node": ""}


def ensure(cfg) -> list[dict]:
    """就地整理分组，返回整理后的列表。幂等，随便调。

    - 一个分组都没有 → 建一个 ``PROXY``，把所有可用节点放进去
    - 已不存在的节点名（被删 / 被停用）从各组里剔掉
    - 没被任何组收录的新节点，追加到第一个组，不然它们会「消失」
    - 当前分组名失效 → 回落到第一个组
    - 当前组选中的节点失效 → 置空，改回自动
    """
    valid = set(names(cfg))
    raw = [g for g in (cfg.outlet.sb_groups or []) if isinstance(g, dict)]
    groups: list[dict] = []
    seen: set[str] = set()
    for g in raw[:MAX_GROUPS]:
        name = _clean(g.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        members = [x for x in dict.fromkeys(str(v) for v in (g.get("nodes") or [])) if x]
        # 节点列表暂时为空（订阅还没拉回来 / 全被停用）时不要清成员，
        # 否则一次网络抖动就会把用户手工分好的组抹掉
        if valid:
            members = [x for x in members if x in valid]
        node = str(g.get("node") or "")
        groups.append({"name": name, "nodes": members,
                       "node": node if node in members else ""})

    if not groups:
        legacy = str(cfg.outlet.sb_node or "")
        groups = [{"name": DEFAULT_NAME, "nodes": list(names(cfg)),
                   "node": legacy if legacy in valid else ""}]

    assigned = {x for g in groups for x in g["nodes"]}
    extra = [n for n in valid if n not in assigned] if valid else []
    if extra:
        groups[0]["nodes"].extend(extra)

    cfg.outlet.sb_groups = groups
    names_set = {g["name"] for g in groups}
    if cfg.outlet.sb_group not in names_set:
        cfg.outlet.sb_group = groups[0]["name"]
    # 兼容旧字段：sb_node 永远是「当前分组选中的节点」
    cfg.outlet.sb_node = active(cfg)["node"]
    return groups


def _find(cfg, name: str) -> dict | None:
    for g in cfg.outlet.sb_groups or []:
        if g.get("name") == name:
            return g
    return None


def _valid_nodes(cfg, nodes) -> list[str]:
    ok = set(names(cfg))
    out = [x for x in dict.fromkeys(str(v) for v in (nodes or [])) if x in ok]
    return out


def create(cfg, name: Any, nodes=None) -> str:
    ensure(cfg)
    n = _clean(name)
    if not n:
        raise ValueError("分组名称不能为空")
    if _find(cfg, n):
        raise ValueError(f"分组「{n}」已存在")
    if len(cfg.outlet.sb_groups) >= MAX_GROUPS:
        raise ValueError(f"最多只能有 {MAX_GROUPS} 个分组")
    members = _valid_nodes(cfg, nodes)
    cfg.outlet.sb_groups.append({"name": n, "nodes": members, "node": ""})
    # 新建就是为了用：直接切过去，卡片上立刻能看见
    cfg.outlet.sb_group = n
    cfg.outlet.sb_node = ""
    return f"已创建分组「{n}」（{len(members)} 个节点），并切到该分组"


def rename(cfg, frm: Any, to: Any) -> str:
    ensure(cfg)
    old = _clean(frm)
    new = _clean(to)
    g = _find(cfg, old)
    if g is None:
        raise ValueError(f"分组「{old}」不存在")
    if not new:
        raise ValueError("分组名称不能为空")
    if new != old and _find(cfg, new):
        raise ValueError(f"分组「{new}」已存在")
    g["name"] = new
    if cfg.outlet.sb_group == old:
        cfg.outlet.sb_group = new
    return f"分组「{old}」已改名为「{new}」"


def delete(cfg, name: Any) -> str:
    ensure(cfg)
    n = _clean(name)
    if _find(cfg, n) is None:
        raise ValueError(f"分组「{n}」不存在")
    if len(cfg.outlet.sb_groups) <= 1:
        raise ValueError("至少要留一个分组")
    cfg.outlet.sb_groups = [g for g in cfg.outlet.sb_groups if g["name"] != n]
    if cfg.outlet.sb_group == n:
        cfg.outlet.sb_group = cfg.outlet.sb_groups[0]["name"]
    return f"已删除分组「{n}」"


def update(cfg, name: Any, nodes=None, node: Any = None) -> str:
    """改成员 / 改选中节点。``node=None`` 表示不动；``node=""`` 表示改回自动。"""
    ensure(cfg)
    n = _clean(name)
    g = _find(cfg, n)
    if g is None:
        raise ValueError(f"分组「{n}」不存在")
    if nodes is not None:
        g["nodes"] = _valid_nodes(cfg, nodes)
    if node is not None:
        want = str(node or "")
        if want and want not in g["nodes"]:
            raise ValueError(f"节点「{want}」不在分组「{n}」里")
        g["node"] = want
    if g["node"] and g["node"] not in g["nodes"]:
        g["node"] = ""
    return f"分组「{n}」已保存（{len(g['nodes'])} 个节点）"


def set_active(cfg, name: Any) -> str:
    ensure(cfg)
    n = _clean(name)
    if _find(cfg, n) is None:
        raise ValueError(f"分组「{n}」不存在")
    cfg.outlet.sb_group = n
    return f"已切到分组「{n}」"


def set_node(cfg, node: Any, group: str = "") -> str:
    """给分组指定出口节点；``node=""`` = 自动（延迟最低）。

    从「订阅」页点节点卡片时也会走这里：节点若不在当前分组，顺手收进来，
    否则用户会看到「选了但没生效」。
    """
    ensure(cfg)
    g = _find(cfg, _clean(group)) if group else active(cfg)
    if g is None:
        raise ValueError(f"分组「{group}」不存在")
    want = str(node or "")
    if want:
        if want not in names(cfg):
            raise ValueError(f"节点「{want}」不可用（可能已停用或删除）")
        if want not in g["nodes"]:
            g["nodes"].append(want)
        g["node"] = want
    else:
        g["node"] = ""
    cfg.outlet.sb_node = active(cfg)["node"]
    return f"「{g['name']}」已{'固定到 ' + want if want else '改回自动（延迟最低）'}"
