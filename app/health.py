"""连通性体检 + 出口能力探测。

出口能力探测是这个工具的关键：Cloudflare Workers 那类「前缀反代」
不实现 CONNECT，填进 proxy.cfg 必然失败。这里直接判定并给出结论，
省得用户填错地址白折腾。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import host

TESTS: list[dict] = [
    {"key": "dockerhub", "name": "Docker Hub 仓库",
     "url": "https://registry-1.docker.io/v2/", "ok": ("200", "401", "403")},
    {"key": "ghcr", "name": "GHCR 仓库",
     "url": "https://ghcr.io/v2/", "ok": ("200", "401", "403")},
    {"key": "github_raw", "name": "GitHub Raw（CA 模板 / 插件）",
     "url": "https://raw.githubusercontent.com/github/gitignore/main/Node.gitignore", "ok": ("200",)},
    {"key": "github_api", "name": "GitHub API",
     "url": "https://api.github.com", "ok": ("200", "401", "403")},
    {"key": "ca_store", "name": "CA 应用商店",
     "url": "https://ca.unraid.net/apps/", "ok": ("200", "301", "302", "403")},
]


def _one(test: dict, proxy: str, timeout: int) -> dict:
    r = host.curl(test["url"], proxy=proxy, timeout=timeout)
    return {
        "key": test["key"], "name": test["name"], "url": test["url"],
        "code": r["code"], "ms": r["ms"], "ok": r["code"] in test["ok"],
    }


def run_all(proxy: str = "", timeout: int = 12) -> list[dict]:
    with ThreadPoolExecutor(max_workers=len(TESTS)) as ex:
        return list(ex.map(lambda t: _one(t, proxy, timeout), TESTS))


def compare(proxy: str = "", timeout: int = 12) -> dict:
    direct = run_all("", timeout)
    via = run_all(proxy, timeout) if proxy else []
    return {"direct": direct, "via_proxy": via, "proxy": proxy}


def probe_outlet(url: str, timeout: int = 12) -> dict:
    """判定一个地址是「标准正向代理」还是「前缀反代」还是连不上。"""
    url = (url or "").strip()
    if not url:
        return {"verdict": "empty", "message": "地址为空"}

    probe_target = "https://raw.githubusercontent.com/github/gitignore/main/Node.gitignore"
    fwd = host.curl(probe_target, proxy=url, timeout=timeout)
    forward_ok = fwd["code"] == "200"

    prefix_ok = False
    prefix_code = "000"
    if url.startswith("http://") or url.startswith("https://"):
        r2 = host.curl(url.rstrip("/") + "/" + probe_target, timeout=timeout)
        prefix_code = r2["code"]
        prefix_ok = prefix_code == "200"

    if forward_ok:
        return {
            "verdict": "forward", "ok": True, "code": fwd["code"], "ms": fwd["ms"],
            "message": "标准正向代理（支持 CONNECT），可直接用于 proxy.cfg / dockerd",
        }
    if prefix_ok:
        return {
            "verdict": "prefix", "ok": False, "code": prefix_code,
            "message": (
                "这是「路径前缀式反代」，不实现 CONNECT 方法。"
                "无法当作正向代理使用，填进 proxy.cfg 或 http_proxy 必然失败。"
            ),
        }
    return {
        "verdict": "unreachable", "ok": False, "code": fwd["code"],
        "message": f"既不支持 CONNECT，前缀方式也不可达（HTTP {fwd['code']}）。请检查地址、端口与网络。",
    }
