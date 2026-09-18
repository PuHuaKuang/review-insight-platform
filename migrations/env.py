"""Alembic 迁移环境。

reviews 与 sync_log 由 app-review-insight 技能脚本创建维护，
不纳入迁移（include_object 过滤），只管理平台自建表：
daily_metric / topic_daily / release / alert_rule / alert_event。
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as app_config  # noqa: E402
from models import Base  # noqa: E402

app_config.load_env()

this = context.config
if this.config_file_name is not None:
    fileConfig(this.config_file_name)

target_metadata = Base.metadata
EXCLUDED_TABLES = {"reviews", "sync_log"}


def include_object(object_, name, type_, reflected, compare_to):
    if type_ == "table" and name in EXCLUDED_TABLES:
        return False
    return True


def run_migrations_online() -> None:
    kwargs = {}
    if app_config.DB_URL.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    connectable = create_engine(app_config.DB_URL, poolclass=None, **kwargs)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            render_as_batch=app_config.DB_URL.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
