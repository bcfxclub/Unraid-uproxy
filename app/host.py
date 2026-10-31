"""宿主机读写层：备份 -> 原子写 -> 保留原权限。

任何一次写入都会先备份，rc.docker 这类可执行文件必须保留原 mode，
否则 Docker 会启动失败。
"""
from __future__ import annotations

import locale
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import settings as S


class HostError(Exception):
    pass


def _decode(b: bytes) -> str:
    """子进程输出可能是任何本地编码（Windows 的 GBK、老镜像的 latin-1）。
    硬按 UTF-8 解会在遇到非法字节时抛错并把输出整个丢掉，
    让上层把「命令成功」误判成「命令失败」。这里逐层降级，保证总能拿到文本。"""
    if not b:
        return ""
    for enc in ("utf-8", locale.getpreferredencoding(False), "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", errors="replace")


def run(argv: list[str], timeout: int = 25, env: dict[str, str] | None = None) -> dict:
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, env=env)
        return {"rc": p.returncode, "out": _decode(p.stdout).strip(),
                "err": _decode(p.stderr).strip()}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "out": "", "err": f"timeout after {timeout}s"}
    except FileNotFoundError as e:
        return {"rc": 127, "out": "", "err": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"rc": 1, "out": "", "err": str(e)}


def sh(cmd: str, timeout: int = 25) -> dict:
    return run(["/bin/sh", "-c", cmd], timeout=timeout)


def exists(path: str) -> bool:
    return os.path.exists(path)


def read(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        raise HostError(f"读取失败 {path}: {e}")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _gc(limit: int = S.MAX_BACKUPS) -> None:
    if not S.BACKUP_DIR.exists():
        return
    items = sorted(S.BACKUP_DIR.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in items[limit:]:
        try:
            p.unlink()
        except OSError:
            pass


def backup(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    S.ensure_dirs()
    now = datetime.now()
    # 文件名带毫秒：只到秒的话，同一秒内的两次写入会互相覆盖，
    # 「还原到最初备份」拿到的其实是改动后的内容；而且同秒备份排序不分先后，
    # 会让 reset 挑错备份。带毫秒后字典序就等于时间先后。
    ts = now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"
    base = _safe(os.path.basename(path))
    dst = S.BACKUP_DIR / f"{base}.{ts}.bak"
    n = 0
    while dst.exists():   # 兜底：极端情况下仍然撞名就加序号
        n += 1
        dst = S.BACKUP_DIR / f"{base}.{ts}-{n}.bak"
    shutil.copy2(path, dst)
    _gc()
    return str(dst)


def list_backups() -> list[dict]:
    if not S.BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(S.BACKUP_DIR.glob("*.bak"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        out.append(
            {
                "file": p.name,
                "path": str(p),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "kind": p.name.split(".")[0],
            }
        )
    return out


def write_file(path: str, content: str, do_backup: bool = True, mode: int | None = None) -> str | None:
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            raise HostError(f"无法创建目录 {parent}: {e}")

    old_mode = None
    if os.path.exists(path):
        try:
            old_mode = os.stat(path).st_mode & 0o777
        except OSError:
            old_mode = None

    bk = backup(path) if do_backup else None

    tmp = f"{path}.upx.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, path)

    target_mode = old_mode if old_mode is not None else (mode if mode is not None else 0o644)
    try:
        os.chmod(path, target_mode)
    except OSError:
        pass
    return bk


def restore(backup_path: str, target: str) -> str:
    """把备份还原到目标路径（保留目标当前权限）。"""
    if not os.path.exists(backup_path):
        raise HostError(f"备份不存在: {backup_path}")
    mode = None
    if os.path.exists(target):
        try:
            mode = os.stat(target).st_mode & 0o777
        except OSError:
            mode = None
    backup(target)  # 还原前再备份一次，留出后悔药
    shutil.copy2(backup_path, target)
    if mode is not None:
        try:
            os.chmod(target, mode)
        except OSError:
            pass
    return target


def docker(*args: str, timeout: int = 60) -> dict:
    return run(["docker", *args], timeout=timeout)


def curl(
    url: str,
    proxy: str = "",
    timeout: int = 12,
    head: bool = False,
    extra: list[str] | None = None,
) -> dict:
    """返回 {rc, code, time, err}。proxy 为空表示直连。"""
    argv = [
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} %{time_total}",
        "--max-time",
        str(timeout),
    ]
    if head:
        argv.append("-I")
    if proxy:
        argv += ["-x", proxy]
    if extra:
        argv += extra
    argv.append(url)
    r = run(argv, timeout=timeout + 5)
    code, tm = "000", None
    parts = (r["out"] or "").split()
    if len(parts) >= 1:
        code = parts[0]
    if len(parts) >= 2:
        try:
            tm = round(float(parts[1]) * 1000)
        except ValueError:
            tm = None
    return {"rc": r["rc"], "code": code, "ms": tm, "err": r["err"]}
