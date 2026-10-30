"""冒烟测试：在一棵伪造的 Unrail 目录树上跑通 计划→应用→持久化→还原 全流程。

重点验证三件最容易出事的事：
  1. rc.docker 注入必须幂等（重复应用不得叠加 env）
  2. rc.docker 的可执行权限必须保留（否则 Docker 起不来）
  3. daemon.json 合并时不得丢失已有字段

用法：python tests/smoke.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="uproxy-smoke-")
HOST = os.path.join(TMP, "host")

RC_REL = os.path.join("usr", "local", "etc", "rc.d", "rc.docker")
DJ_REL = os.path.join("etc", "docker", "daemon.json")
CA_REL = os.path.join("boot", "config", "plugins", "community.applications", "proxy.cfg")
GO_REL = os.path.join("boot", "config", "go")

RC_BODY = "\n".join([
    "#!/bin/bash",
    "# Start the docker daemon",
    "DOCKER=/usr/bin/dockerd",
    "DOCKER_PIDFILE=/var/run/docker.pid",
    "DOCKER_OPTS=",
    "DOCKER_LOG=/var/log/docker.log",
    "UNSHARE=/usr/bin/unshare",
    "  nohup $UNSHARE --propagation slave -- $DOCKER -p $DOCKER_PIDFILE $DOCKER_OPTS >>$DOCKER_LOG 2>&1 &",
    "",
])

GO_BODY = "#!/bin/bash\n/usr/local/sbin/emhttp &\n"

DJ_BODY = json.dumps({"log-driver": "json-file", "registry-mirrors": ["https://old.example"]}, indent=2)

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


def w(rel: str, body: str, mode: int = 0o644) -> str:
    p = os.path.join(HOST, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.chmod(p, mode)
    return p


def r(rel: str) -> str:
    with open(os.path.join(HOST, rel), "r", encoding="utf-8") as f:
        return f.read()


def main() -> int:
    w(RC_REL, RC_BODY, 0o755)
    w(DJ_REL, DJ_BODY)
    w(GO_REL, GO_BODY, 0o755)

    os.environ["UPX_HOST_ROOT"] = HOST
    os.environ["UPX_STATE_DIR"] = os.path.join(TMP, "state")
    sys.path.insert(0, ROOT)

    from app import host as H
    from app import persist
    from app import settings as S
    from app import state as st
    from app import targets

    print("\n[1] 路径解析")
    check(os.path.exists(S.resolve("rc_docker")), f"找到 rc.docker: {S.resolve('rc_docker')}")
    check(os.path.exists(S.resolve("go_file")), f"找到 go: {S.resolve('go_file')}")

    cfg = st.load()
    cfg.outlet.mode = "manual"
    cfg.outlet.http = "http://192.168.1.5:7890"
    cfg.targets = {"dockerd": True, "mirror": True, "ca": True, "system": True}
    cfg.mirrors = ["https://docker.xuanyuan.me", "https://docker.1ms.run"]
    st.save(cfg)

    print("\n[2] 生成变更预览")
    plan = targets.plan(cfg)
    keys = [p["key"] for p in plan]
    check(set(keys) == {"dockerd", "mirror", "ca", "system"}, "四个目标都产出计划")
    check(all(p["ok"] for p in plan), "全部目标渲染成功")
    check(next(p for p in plan if p["key"] == "dockerd")["changed"], "dockerd 检测到需要变更")

    print("\n[3] 应用变更")
    res = targets.apply(cfg, ["dockerd", "mirror", "ca", "system"], restart=False)
    check(all(x["status"] in ("written", "unchanged") for x in res["results"]),
          "全部写入成功：" + ", ".join(f'{x["key"]}={x["status"]}' for x in res["results"]))

    rc_text = r(RC_REL)
    check("nohup env http_proxy=http://192.168.1.5:7890" in rc_text, "rc.docker 已注入 http_proxy")
    check("https_proxy=http://192.168.1.5:7890" in rc_text, "rc.docker 已注入 https_proxy")
    check("$UNSHARE --propagation slave" in rc_text, "rc.docker 原始启动参数完整保留")
    mode = stat.S_IMODE(os.stat(os.path.join(HOST, RC_REL)).st_mode)
    if os.name == "posix":
        # Windows 没有 POSIX 权限位（chmod 只能切只读属性），该断言只在 Linux/Unraid 上有意义
        check(mode == 0o755, f"rc.docker 可执行权限保留为 {oct(mode)}")
    else:
        print(f"  SKIP  rc.docker 权限位校验（{os.name} 无 POSIX 权限，实际值 {oct(mode)}）")

    dj = json.loads(r(DJ_REL))
    check(dj.get("log-driver") == "json-file", "daemon.json 原有字段 log-driver 未丢失")
    check(dj.get("registry-mirrors") == cfg.mirrors, "registry-mirrors 已更新")

    ca_text = r(CA_REL)
    check("proxy=http://192.168.1.5:7890" in ca_text, "CA proxy.cfg 已写入")
    check("port=7890" in ca_text, "CA proxy.cfg 端口号正确")

    print("\n[4] 幂等性（重复应用不得叠加）")
    res2 = targets.apply(cfg, ["dockerd", "mirror", "ca", "system"], restart=False)
    check(all(x["status"] == "unchanged" for x in res2["results"]), "第二次应用全部为 unchanged")
    check(r(RC_REL).count("http_proxy=") == 1, "rc.docker 中 http_proxy 只出现一次")

    print("\n[5] /boot/config/go 开机重放")
    g1 = persist.apply_go(cfg)
    check(g1["status"] == "written", "go 托管块已写入")
    go_text = r(GO_REL)
    check(persist.BEGIN in go_text and persist.END in go_text, "go 含完整标记块")
    check("/usr/local/sbin/emhttp &" in go_text, "go 原有 emhttp 行未被破坏")
    check("cat > /etc/docker/daemon.json" in go_text, "go 会重建 daemon.json")
    check("sed -i 's|nohup \\$UNSHARE|" in go_text, "go 会注入 rc.docker 代理")
    g2 = persist.apply_go(cfg)
    check(g2["status"] == "unchanged", "重复写入 go 不产生变更")
    check(go_text.count(persist.BEGIN) == 1, "go 中标记块不重复")

    print("\n[6] 关闭出口后应清除注入")
    cfg.outlet.mode = "none"
    st.save(cfg)
    targets.apply(cfg, ["dockerd", "ca", "system"], restart=False)
    check("http_proxy" not in r(RC_REL), "rc.docker 代理已清除")
    check(r(CA_REL).strip() == "", "CA proxy.cfg 已清空")

    print("\n[7] 还原")
    backups = H.list_backups()
    check(len(backups) > 0, f"产生了 {len(backups)} 份备份")
    rc_bak = next((b for b in backups if b["file"].startswith("rc.docker.")), None)
    check(rc_bak is not None, "存在 rc.docker 备份")
    if rc_bak:
        H.restore(rc_bak["path"], S.resolve("rc_docker"))
        check(r(RC_REL) == RC_BODY, "rc.docker 已还原为原始内容")

    print("\n[8] 卸载移除托管块")
    out = persist.remove_block(cfg)
    check(out["status"] in ("written", "unchanged"), "卸载完成")
    check(persist.BEGIN not in r(GO_REL), "go 中托管块已移除")

    print("\n" + ("=" * 46))
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for m in FAILED:
            print("  - " + m)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
