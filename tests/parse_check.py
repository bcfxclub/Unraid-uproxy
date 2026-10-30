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
]


def main() -> int:
    res = subs.parse("\n".join(SAMPLES))
    print(res["message"])
    if res["unsupported"]:
        print("跳过不支持：", res["unsupported"])
    print()
    for n in res["nodes"]:
        print(f"# {n['name']}  [{n['type']}]  {n['server']}:{n['port']}")
        print(json.dumps(n["outbound"], ensure_ascii=False, indent=2))
        print()
    return 0 if res["nodes"] else 1


if __name__ == "__main__":
    sys.exit(main())
