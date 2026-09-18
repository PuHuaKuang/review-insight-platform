"""平台配置。

数据库通过环境变量切换后端：
  REVIEW_DB_URL=sqlite:///C:/path/reviews.db      （本地原型，默认）
  REVIEW_DB_URL=postgresql+psycopg2://user:pw@host/db   （云上）
"""
from __future__ import annotations

import os
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE = WORKDIR / "work" / "reviews.db"

DB_URL = os.environ.get("REVIEW_DB_URL") or f"sqlite:///{DEFAULT_SQLITE.as_posix()}"

PACKAGE = os.environ.get("REVIEW_PACKAGE", "com.tcl.browser")

# 基线差评率与告警参数（后续改为自动计算，先用当前实测值）
BASELINE_NEGATIVE_RATE = 0.0728
ALERT_WINDOW_DAYS = 7
ALERT_MIN_SAMPLE = 500
ALERT_NEGATIVE_RATE = 0.065

# 正文脱敏：作者名不落库原文，仅保留哈希用于识别同一用户
AUTHOR_HASH_SALT = os.environ.get("REVIEW_AUTHOR_SALT", "app-review-insight")

def load_env(path: str | None = None) -> None:
    """读取 .env（若存在）注入环境变量；已有环境变量优先。

    必须在 import auth 之前调用——auth 模块加载时会读取 SECRET_KEY。
    """
    f = Path(path) if path else Path(__file__).resolve().parent / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k:
            os.environ.setdefault(k, v.strip().strip('"'))

