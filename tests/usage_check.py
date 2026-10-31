"""订阅流量用量（subscription-userinfo）回归测试。

机场把「已用 / 总量 / 到期」放在订阅响应头里，格式是
``Subscription-Userinfo: upload=1; download=2; total=100; expire=1730000000``。
WebUI 首页卡片和「订阅」页靠这几个数画额度条，所以这里盯三件事：

  1. 头部解析：正常值 / 缺字段 / 全零（视为没给）/ 多次重定向只认最后一次
  2. usage_of 真去请求一次，能从头里取到用量（没有 curl 就跳过）
  3. 同步订阅后 usage 落到 cfg.outlet.subscriptions，且下次机场没给时保留旧值

用法：python tests/usage_check.py
"""
from __future__ import annotations

import base64
import http.server
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="uproxy-usage-")
os.environ["UPX_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["UPX_HOST_ROOT"] = os.path.join(TMP, "host")
for _k in ("UPX_TOKEN", "UPX_PASSWORD", "UPX_USER"):
    os.environ.pop(_k, None)
sys.path.insert(0, ROOT)

from app import subs  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


NODE = "vless://11111111-2222-3333-4444-555555555555@127.0.0.1:443?security=tls&type=tcp#测试节点"


HITS: dict[str, int] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    """给一次订阅内容：头部带用量，正文是 base64 节点列表。"""

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        HITS[path] = HITS.get(path, 0) + 1
        # 每个路径给一个不同名字的节点，用来验证「只更新指定订阅」不会把别的源写乱
        tag = "节点" + (path.strip("/").replace("/", "_") or "root")
        body = base64.b64encode(NODE.replace("测试节点", tag).encode()).decode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        if not path.startswith("/nouinfo"):
            self.send_header("Subscription-Userinfo",
                             "upload=1048576; download=524288; total=104857600; expire=1767225600")
        payload = body.encode()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # 安静点
        return


def main() -> int:
    import time as _t

    print("\n[1] 头部解析")
    raw = ("HTTP/1.1 200 OK\r\n"
           "Content-Type: text/plain\r\n"
           "Subscription-Userinfo: upload=10; download=1000; total=10000; expire=1767225600\r\n"
           "\r\n")
    u = subs.parse_userinfo(raw)
    check(u.get("used") == 1010, f"已用 = upload + download（{u.get('used')}）")
    check(u.get("total") == 10000, f"总量 = total（{u.get('total')}）")
    check(u.get("expire") == 1767225600, f"到期时间戳原样保留（{u.get('expire')}）")

    two_hops = ("HTTP/1.1 302 Found\r\nSubscription-Userinfo: upload=0; download=0; total=0\r\n\r\n"
                + raw)
    u2 = subs.parse_userinfo(two_hops)
    check(u2.get("total") == 10000, f"多次重定向只认最后一次（{u2.get('total')}）")

    check(subs.parse_userinfo("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n") == {},
          "没有这个头时返回空（前端不显示那一行）")
    check(subs.parse_userinfo("Subscription-Userinfo: upload=0; download=0; total=0; expire=0") == {},
          "全零当成没给（不显示 0 / 0）")
    odd = subs.parse_userinfo("Subscription-Userinfo: upload=abc; download=2048")
    check(odd.get("used") == 2048, f"坏字段跳过、其余照收（{odd}）")

    if not shutil.which("curl"):
        print("\n[2] 跳过：本机没有 curl（usage_of 依赖它）")
        return 0 if not FAILED else 1

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print("\n[2] usage_of 真请求")
    got = subs.usage_of(f"http://127.0.0.1:{port}/sub")
    check(got.get("total") == 104857600, f"从头里读到总量（{got}）")
    check(got.get("used") == 1048576 + 524288, "已用 = 上传 + 下载")
    check(subs.usage_of(f"http://127.0.0.1:{port}/nouinfo") == {}, "机场没给头时返回空")

    print("\n[3] 同步后落到订阅上")
    from fastapi.testclient import TestClient  # noqa: E402

    from app import main as M  # noqa: E402
    from app import state as st  # noqa: E402

    client = TestClient(M.app)
    tok = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
    hdr = {"X-UPX-Token": tok}
    r = client.post("/api/subs/add", json={"url": f"http://127.0.0.1:{port}/sub", "name": "用量测试"},
                    headers=hdr)
    check(r.status_code == 200, f"新增订阅成功（HTTP {r.status_code}）")
    cfg = st.load()
    one = cfg.outlet.subscriptions[0]
    check(one.get("usage", {}).get("total") == 104857600, f"usage 写进了订阅（{one.get('usage')}）")

    # 再同步一次：机场这次不给头 → 用量不再显示（不能拿上次的数字继续糊弄）
    one["url"] = f"http://127.0.0.1:{port}/nouinfo"
    st.save(cfg)
    M._resync(st.load())
    again = st.load().outlet.subscriptions[0]
    check("usage" not in again, f"这次没给头就不显示用量（{again.get('usage')}）")

    # 拉取失败（连不上）不算「没给」：旧的用量先留着，别因为网络抖一下就把数字清空
    cfg = st.load()
    cfg.outlet.subscriptions[0]["url"] = f"http://127.0.0.1:{port}/sub"
    M._resync(cfg)
    check("usage" in st.load().outlet.subscriptions[0], "有机场给用量时重新记上")
    cfg = st.load()
    cfg.outlet.subscriptions[0]["url"] = "http://127.0.0.1:9/none"
    M._resync(cfg)
    broken = st.load().outlet.subscriptions[0]
    check("usage" in broken, f"拉取失败时保留上次的用量（{broken.get('usage')}）")

    print("\n[4] 只更新指定订阅时不动其它订阅")
    cfg = st.load()
    cfg.outlet.subscriptions[0]["url"] = f"http://127.0.0.1:{port}/sub"
    cfg.outlet.subscriptions.append({
        "id": "sub-other", "name": "旁观者", "url": f"http://127.0.0.1:{port}/other",
        "ua": "clash.meta", "proxy": "", "enabled": True, "count": 0,
    })
    st.save(cfg)
    M._resync(st.load())
    HITS.clear()
    cfg = st.load()
    res = subs.sync_subscriptions(cfg.outlet.subscriptions, previous=cfg.outlet.sb_nodes,
                                  refresh=[cfg.outlet.subscriptions[0]["id"]])
    check(HITS.get("/other", 0) == 0, f"没被点到的订阅一次都没请求（{dict(HITS)}）")
    check(HITS.get("/sub", 0) >= 1, f"被点到的订阅确实重新拉了（{dict(HITS)}）")
    check(any(s.get("reused") for s in res["summary"]), "没被点到的订阅标成「沿用上次」")
    check(len(res["nodes"]) == 2, f"两条订阅的节点都还在（{len(res['nodes'])}）")

    srv.shutdown()
    if FAILED:
        print("\n失败：")
        for f in FAILED:
            print(" -", f)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
