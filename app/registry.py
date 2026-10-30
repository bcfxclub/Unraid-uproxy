"""镜像源体检池。

公共镜像加速源时效性极强（半年一换血），所以不能写死依赖：
每次应用前先探测 /v2/ 接口，按响应耗时排序，剔除不可用的。
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import host
from . import settings as S

ALIVE_CODES = {"200", "401", "403"}
CACHE_TTL = 15 * 60


def check_one(mirror: str, timeout: int = 8) -> dict:
    url = mirror.rstrip("/") + "/v2/"
    r = host.curl(url, timeout=timeout, head=True)
    if r["code"] in ("000", "400", "405"):
        r = host.curl(url, timeout=timeout)
    return {
        "mirror": mirror,
        "alive": r["code"] in ALIVE_CODES,
        "code": r["code"],
        "ms": r["ms"],
    }


def check_all(mirrors: list[str], timeout: int = 8) -> list[dict]:
    uniq = list(dict.fromkeys([m.strip() for m in mirrors if m and m.strip()]))
    if not uniq:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(uniq))) as ex:
        res = list(ex.map(lambda m: check_one(m, timeout), uniq))
    res.sort(key=lambda x: (not x["alive"], x["ms"] if x["ms"] is not None else 999999))
    return res


def load_cache() -> dict:
    if not os.path.exists(S.MIRROR_CACHE):
        return {}
    try:
        with open(S.MIRROR_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(results: list[dict]) -> None:
    S.ensure_dirs()
    tmp = str(S.MIRROR_CACHE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": int(time.time()), "results": results}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, S.MIRROR_CACHE)


def cached_or_check(mirrors: list[str], force: bool = False) -> dict:
    cache = load_cache()
    if not force and cache.get("results") and time.time() - cache.get("ts", 0) < CACHE_TTL:
        known = {r["mirror"] for r in cache["results"]}
        if known == set(m.strip() for m in mirrors if m and m.strip()):
            return {"results": cache["results"], "ts": cache["ts"], "cached": True}
    res = check_all(mirrors)
    save_cache(res)
    return {"results": res, "ts": int(time.time()), "cached": False}


def pick(mirrors: list[str], limit: int = 3, timeout: int = 8) -> list[str]:
    """返回按速度排序的可用源，供写入 daemon.json。"""
    res = check_all(mirrors, timeout=timeout)
    return [r["mirror"] for r in res if r["alive"]][:limit]
