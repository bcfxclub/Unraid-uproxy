"""进程内环形日志缓冲，供 WebUI「日志」页展示（仿 v2rayA）。

只保留最近 N 条（内存），不写文件，重启即清空。
每条记录：seq、ts（ISO8601 本地时间）、level、scope、msg。
level 取值：info / debug / warn / error / trace
"""
from __future__ import annotations

import threading
import time
from collections import deque

_MAX = 2000  # 内存中保留的最大日志条数

# seq -> 记录，seq 单调递增，便于前端做增量拉取
_records: "deque[dict]" = deque(maxlen=_MAX)
_lock = threading.Lock()
_seq = 0

LEVELS = ("debug", "info", "warn", "error", "trace")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def append(level: str, scope: str, msg: str) -> int:
    """写一条日志，返回该条的 seq。未知 level 归一为 info。"""
    global _seq
    if level not in LEVELS:
        level = "info"
    with _lock:
        _seq += 1
        _records.append({
            "seq": _seq,
            "ts": _now(),
            "level": level,
            "scope": scope or "",
            "msg": msg,
        })
    return _seq


def last_seq() -> int:
    with _lock:
        return _seq


def since(seq: int, level: str = "", limit: int = 200) -> list[dict]:
    """返回 seq 之后的日志（增量拉取用），可按 level 过滤。seq<=0 返回最近 limit 条。"""
    with _lock:
        if seq <= 0:
            items = list(_records)[-limit:]
        else:
            items = [r for r in _records if r["seq"] > seq][:limit]
        if level and level not in ("all", ""):
            items = [r for r in items if r["level"] == level]
        return items


def export_text(level: str = "", limit: int = 2000) -> str:
    """导出为纯文本（每行：[ts] level scope msg），供「导出」按钮下载。"""
    items = since(0, level, limit) if not level else since(0, level, limit)
    return "\n".join(
        f"[{r['ts']}] {r['level'].upper():<5} [{r['scope']}] {r['msg']}"
        for r in items
    ) + ("\n" if items else "")


def scopes() -> list[str]:
    with _lock:
        return sorted({r["scope"] for r in _records if r["scope"]})
