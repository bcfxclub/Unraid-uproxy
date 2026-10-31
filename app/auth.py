"""账号密码登录：用户名 + 密码，登录成功后签发会话令牌（对前端透明，用户不感知）。

设计要点（面向家庭局域网部署，够用且不臃肿）：
- 默认用户名 ``admin``，密码来自环境变量 ``UPX_PASSWORD``；为兼容老部署，
  若只设了 ``UPX_TOKEN`` 也当作密码用。两者都为空则**不鉴权**（开放访问）。
- 登录后签发随机会话令牌，存内存字典并带过期时间（默认 7 天），前端只持有这个令牌，
  不再直接接触密码。
- 兼容老的「直连口令」方式：仍显式设了 ``UPX_TOKEN`` 时，旧书签 ``?token=`` /
  ``X-UPX-Token`` 直接等于旧口令也能用，避免升级后老链接失效。
"""
from __future__ import annotations

import os
import secrets
import time

USER = (os.environ.get("UPX_USER") or "admin").strip() or "admin"

# 密码优先级：UPX_PASSWORD > UPX_TOKEN（老部署）。都为空 -> 开放访问。
PASSWORD = (os.environ.get("UPX_PASSWORD") or os.environ.get("UPX_TOKEN") or "").strip()

# 老的「直连口令」：仅当显式设置了 UPX_TOKEN 才接受旧的 ?token= 方式。
LEGACY_TOKEN = os.environ.get("UPX_TOKEN", "").strip()

REQUIRED = bool(PASSWORD)

# token -> 过期时间戳
SESSIONS: dict[str, float] = {}

# 默认 7 天；可用 UPX_SESSION_TTL（秒）调整。
TTL = int(os.environ.get("UPX_SESSION_TTL") or 60 * 60 * 24 * 7)


def login(username: str, password: str):
    """校验账号密码，成功返回会话令牌，失败返回 None。"""
    if not REQUIRED:
        return None
    if username == USER and secrets.compare_digest(password, PASSWORD):
        tok = secrets.token_hex(24)
        SESSIONS[tok] = time.time() + TTL
        return tok
    return None


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
