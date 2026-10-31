"""账号密码登录：用户名 + 密码，登录成功后签发会话令牌（对前端透明，用户不感知）。

设计要点（面向家庭局域网部署，够用且不臃肿）：
- 凭据来源优先级：``STATE_DIR/auth.json``（界面里改过的密码）> 环境变量
  ``UPX_PASSWORD`` / 老部署的 ``UPX_TOKEN`` > **默认密码** ``admin``（用户名 ``admin``）。
  默认密码只是兜底，避免装好就裸奔；首次登录后请到「账号」页改掉。
- 默认用户名 ``admin``，可用 ``UPX_USER`` 覆盖。
- 密码只存 PBKDF2-SHA256 哈希 + 随机盐，不存明文；auth.json 权限 0600。
- 登录后签发随机会话令牌，存内存字典并带过期时间（默认 7 天），前端只持有这个令牌，
  不再直接接触密码。改密码后旧会话全部失效。
- 兼容老的「直连口令」方式：仍显式设了 ``UPX_TOKEN`` 时，旧书签 ``?token=`` /
  ``X-UPX-Token`` 直接等于旧口令也能用，避免升级后老链接失效。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time

from . import settings as S

CRED_FILE = S.STATE_DIR / "auth.json"

ALGO = "pbkdf2_sha256"
ITERS = 240_000
MIN_LEN = 8          # 新密码最短长度
MAX_LEN = 200

ENV_USER = (os.environ.get("UPX_USER") or "admin").strip() or "admin"

# 密码优先级：UPX_PASSWORD > UPX_TOKEN（老部署）。都为空 -> 用默认密码（见下）。
ENV_PASSWORD = (os.environ.get("UPX_PASSWORD") or os.environ.get("UPX_TOKEN") or "").strip()

# 初始默认密码：既没设环境变量、也没在界面改过时使用，强制登录避免局域网裸奔。
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "admin"

# 老的「直连口令」：仅当显式设置了 UPX_TOKEN 才接受旧的 ?token= 方式。
LEGACY_TOKEN = os.environ.get("UPX_TOKEN", "").strip()

# 默认 7 天；可用 UPX_SESSION_TTL（秒）调整。
TTL = int(os.environ.get("UPX_SESSION_TTL") or 60 * 60 * 24 * 7)

# token -> 过期时间戳
SESSIONS: dict[str, float] = {}

# ---------------------------------------------------------------------------
# 运行时生效的凭据状态（由 _refresh() 维护，改密码后即时切换）
# ---------------------------------------------------------------------------
USER = ENV_USER                      # 当前生效用户名
REQUIRED = True                      # 是否要求登录（默认密码也要求登录）
SOURCE = "env" if ENV_PASSWORD else "default"   # file / env / default
_CRED: dict | None = None            # auth.json 内容（存在时优先生效）


def _hash(password: str, salt: bytes, iters: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters).hex()


def _load() -> dict | None:
    """读 auth.json，格式不合法当作不存在（退回环境变量）。"""
    try:
        raw = CRED_FILE.read_text("utf-8")
    except OSError:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("hash") or not d.get("salt"):
        return None
    return d


def _refresh() -> None:
    global _CRED, USER, REQUIRED, SOURCE
    _CRED = _load()
    if _CRED:
        USER = str(_CRED.get("user") or ENV_USER)
        REQUIRED = True
        SOURCE = "file"
    elif ENV_PASSWORD:
        USER = ENV_USER
        REQUIRED = True
        SOURCE = "env"
    else:
        # 什么都没配：用默认密码 admin/admin，仍然要求登录
        USER = DEFAULT_USER
        REQUIRED = True
        SOURCE = "default"


_refresh()


def verify(password: str) -> bool:
    """校验密码（不校验用户名）。"""
    if not REQUIRED:
        return False
    if _CRED is not None:
        try:
            salt = bytes.fromhex(_CRED["salt"])
            iters = int(_CRED.get("iter") or ITERS)
        except Exception:
            return False
        return hmac.compare_digest(_hash(password, salt, iters), _CRED["hash"])
    if SOURCE == "default":
        return hmac.compare_digest(password, DEFAULT_PASSWORD)
    return hmac.compare_digest(password, ENV_PASSWORD)


def login(username: str, password: str):
    """校验账号密码，成功返回会话令牌，失败返回 None。"""
    if not REQUIRED:
        return None
    if username == USER and verify(password):
        return _issue()
    return None


def _issue() -> str:
    tok = secrets.token_hex(24)
    SESSIONS[tok] = time.time() + TTL
    return tok


def change(username: str, password: str) -> str:
    """改用户名和/或密码，写 auth.json 并即时生效，返回新令牌。

    密码不合格抛 ValueError（由接口转成 400）。
    """
    username = (username or "").strip() or ENV_USER
    if not username:
        raise ValueError("用户名不能为空")
    if len(username) > 64:
        raise ValueError("用户名最长 64 字符")
    if len(password) < MIN_LEN:
        raise ValueError(f"新密码至少 {MIN_LEN} 位")
    if len(password) > MAX_LEN:
        raise ValueError(f"新密码最长 {MAX_LEN} 位")

    salt = secrets.token_bytes(16)
    payload = {
        "user": username,
        "algo": ALGO,
        "iter": ITERS,
        "salt": salt.hex(),
        "hash": _hash(password, salt, ITERS),
        "updated": int(time.time()),
    }
    S.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CRED_FILE.with_name(CRED_FILE.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    try:
        os.chmod(tmp, 0o600)   # Windows 上不生效，忽略
    except OSError:
        pass
    tmp.replace(CRED_FILE)

    _refresh()
    SESSIONS.clear()          # 旧会话全部作废，强制重新登录
    return _issue()           # 给当前操作者发一个新令牌，避免自己被踢下线


def check_session(token: str | None) -> bool:
    if not token:
        return False
    exp = SESSIONS.get(token)
    if not exp:
        return False
    if exp < time.time():
        SESSIONS.pop(token, None)
        return False
    return True


def logout(token: str | None) -> None:
    if token:
        SESSIONS.pop(token, None)
