"""用给定的节点 URI 验证订阅解析器，并打印生成的 sing-box 出站配置。"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("UPX_HOST_ROOT", os.path.join(ROOT, ".fakehost"))
os.environ.setdefault("UPX_STATE_DIR", os.path.join(ROOT, ".fakestate"))

from app import subs  # noqa: E402

SAMPLES = [
    "vless://761e8be3-f4fb-4ec0-a8fe-9a7fead005bb@104.20.23.31:8443"
    "?host=fq.bcfx.dpdns.org&path=%2F%3Fed%3D2560&type=ws&encryption=none"
    "&fp=random&security=tls&alpn=default&sni=fq.bcfx.dpdns.org#fq.bcfx.dpdns.org",
    "vmess://ewogICJ2IjogIjIiLAogICJwcyI6ICJ0ZXN0IiwKICAiYWRkIjogIjEuMi4zLjQiLAogICJwb3J0IjogIjQ0MyIsCiAgImlkIjogIjExMTExMTExLTExMTEtMTExMS0xMTExLTExMTExMTExMTExMSIsCiAgImFpZCI6ICIwIiwKICAibmV0IjogIndzIiwKICAidHlwZSI6ICJub25lIiwKICAiaG9zdCI6ICJ0ZXN0LmNvbSIsCiAgInBhdGgiOiAiL3dzIiwKICAidGxzIjogInRscyIKfQ==",
    "trojan://password123@example.com:443?sni=example.com&type=ws&path=%2Ftrojan#trojan-node",
    "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@1.2.3.4:8388#ss-node",
    # SNI 错位场景：机场 SNI 是 txb011.xmrhmode.com，但服务器证书只认 ws-aicdn.com。
    # 没写 allowInsecure，默认必须放宽证书校验，否则节点全崩。
    "vmess://ewogICJ2IjogIjIiLAogICJwcyI6ICJTSU5f6L+35pWw5L2cIiwKICAiYWRkIjogInR4YjAxMS54bXJobW9kZS5jb20iLAogICJwb3J0IjogIjQ0MyIsCiAgImlkIjogImEiLAogICJzY3kiOiAidGNwIiwKICAidGxzIjogInRscyIKfQ==",
]

# 上面最后一行的 vmess base64 解码后是：
# ps=SNI错位, add=txb011.xmrhmode.com, port=443, id=a, scy=tcp, tls=tls


def main() -> int:
    fails = []
    res = subs.parse("\n".join(SAMPLES))
    print(res["message"])
    if res["unsupported"]:
        print("跳过不支持：", res["unsupported"])
    print()
    for n in res["nodes"]:
        print(f"# {n['name']}  [{n['type']}]  {n['server']}:{n['port']}")
        print(json.dumps(n["outbound"], ensure_ascii=False, indent=2))
        print()
    if not res["nodes"]:
        fails.append("订阅解析 0 个节点")
        return _finish(fails)

    # 回归：TLS 节点默认必须放宽证书校验（insecure: true），机场没写 allowInsecure 也生效。
    tls_nodes = [n for n in res["nodes"] if (n["outbound"].get("tls") or {}).get("enabled")]
    for n in tls_nodes:
        tls = n["outbound"]["tls"]
        if tls.get("insecure") is not True:
            fails.append(f"TLS 节点「{n['name']}」未默认放宽证书校验（insecure 缺失）")
    print(f"[TLS 默认 insecure] {len(tls_nodes)} 个 TLS 节点全部 insecure: "
          f"{all(n['outbound']['tls'].get('insecure') is True for n in tls_nodes)}")
    return _finish(fails)


def _finish(fails: list[str]) -> int:
    if fails:
        print("\n失败：")
        for f in fails:
            print(" -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
