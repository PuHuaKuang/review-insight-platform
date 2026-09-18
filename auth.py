"""轻量鉴权：口令哈希 + HMAC 签名会话。

配置（环境变量）：
    SECRET_KEY          会话签名密钥（生产必须设置，缺省时随机生成，重启后登录态失效）
    PLATFORM_USER       登录用户名，默认 admin
    PLATFORM_PASSWORD   登录口令（明文，启动时做哈希比对；生产建议用口令哈希）
    PLATFORM_PWHASH     可选的口令哈希（sha256$<salt>$<hash>），优先于 PLATFORM_PASSWORD
    AUTH_ENABLED        设为 0 可关闭鉴权（仅本地开发用）

口令不落库、不进日志。会话以 HttpOnly Cookie 保存，签名含过期时间。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

COOKIE = "rip_session"
TOKEN_TTL = 60 * 60 * 12  # 12 小时
KEY_FILE = Path(__file__).resolve().parent / ".secret_key"

USER = os.environ.get("PLATFORM_USER", "admin")
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "1") != "0"


def _secret() -> bytes:
    k = os.environ.get("SECRET_KEY")
    if k:
        return k.encode()
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip().encode()
    generated = secrets.token_hex(32)
    KEY_FILE.write_text(generated, encoding="utf-8")
    return generated.encode()


SECRET = _secret()


def _b64url(data: bytes) -> str:
    return data.hex()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"sha256${salt}${h}"


def verify_password(password: str, stored: str | None = None) -> bool:
    """stored 形如 sha256$<salt>$<hash>；未提供时回退到环境变量明文口令。"""
    if stored and stored.startswith("sha256$"):
        _, salt, h = stored.split("$", 2)
        calc = hashlib.sha256((salt + password).encode()).hexdigest()
        return hmac.compare_digest(calc, h)
    expected = os.environ.get("PLATFORM_PASSWORD", "")
    if not expected:
        return False
    return hmac.compare_digest(password, expected)


def create_token(username: str) -> str:
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{username}.{exp}"
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str | None) -> str | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    username, exp, sig = parts
    payload = f"{username}.{exp}"
    if not hmac.compare_digest(
        hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest(), sig
    ):
        return None
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return username
