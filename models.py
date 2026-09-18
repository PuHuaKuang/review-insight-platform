"""SQLAlchemy 模型。

reviews 表沿用 app-review-insight 技能已建好的结构（不改动，只读+增量写），
平台自建 daily_metric / topic_daily / sync_log 视图表用于加速。
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from config import DB_URL

Base = declarative_base()


class Review(Base):
    """统一评论记录（与技能脚本共用同一张表）。"""

    __tablename__ = "reviews"

    review_id = Column(Text, primary_key=True)
    author_name = Column(Text)
    star_rating = Column(Integer)
    review_text = Column(Text)
    reviewer_lang = Column(Text)
    device = Column(Text)
    android_version = Column(Text)
    app_version_code = Column(Text)
    app_version_name = Column(Text)
    submitted_at = Column(Text)
    last_modified = Column(Text)
    dev_reply_text = Column(Text)
    dev_replied_at = Column(Text)
    source = Column(Text)
    fetched_at = Column(Text)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id = Column(Integer, primary_key=True)
    source = Column(Text)
    started_at = Column(Text)
    finished_at = Column(Text)
    rows_seen = Column(Integer)
    rows_upsert = Column(Integer)
    status = Column(Text)
    detail = Column(Text)


class DailyMetric(Base):
    """预聚合快照：stat_date × dim_type × dim_value。"""

    __tablename__ = "daily_metric"

    id = Column(Integer, primary_key=True)
    stat_date = Column(Date, index=True)
    dim_type = Column(String(16), index=True)  # all / version / device / lang
    dim_value = Column(Text, index=True)
    total = Column(Integer)
    avg_rating = Column(Float)
    negative = Column(Integer)
    negative_rate = Column(Float)


Index("idx_daily_metric_dim", DailyMetric.dim_type, DailyMetric.dim_value, DailyMetric.stat_date)


class ReviewTranslation(Base):
    """持久化评论译文缓存，以原文哈希避免重复调用翻译服务。"""
    __tablename__ = "review_translation"

    source_hash = Column(String(64), primary_key=True)
    source_text = Column(Text, nullable=False)
    translated_text = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False)


class TopicDaily(Base):
    __tablename__ = "topic_daily"

    id = Column(Integer, primary_key=True)
    stat_date = Column(Date, index=True)
    topic = Column(Text, index=True)
    mentions = Column(Integer)
    negative_mentions = Column(Integer)
    negative_ratio = Column(Float)


def _engine():
    kwargs = {}
    if DB_URL.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(DB_URL, future=True, **kwargs)


engine = _engine()
SessionLocal = sessionmaker(bind=engine, future=True)

class Release(Base):
    """发版记录。released_at 取自版本名中的构建日期（YYMMDD）。"""

    __tablename__ = "release"

    version = Column(Text, primary_key=True)
    released_at = Column(Text, index=True)
    rollout_pct = Column(Integer)
    note = Column(Text)
    source = Column(Text, default="auto")
    first_seen = Column(Text)
    review_count = Column(Integer)
    updated_at = Column(Text)


class AlertRule(Base):
    __tablename__ = "alert_rule"

    code = Column(Text, primary_key=True)
    name = Column(Text)
    enabled = Column(Integer, default=1)
    params = Column(Text)
    updated_at = Column(Text)


class AlertEvent(Base):
    """告警事件。同一 code+subject 的持续问题合并为一行（不再每次评估都插新行）。

    status: open（待处理）/ acked（已确认）/ muted（已静默）/ resolved（已解决）。
    occurrences 记录该问题连续被判定为触发的次数；notified_at 为空表示尚未通知，用于邮件去重。
    """

    __tablename__ = "alert_event"

    id = Column(Integer, primary_key=True)
    code = Column(Text, index=True)
    level = Column(Text)
    stat_date = Column(Text, index=True)
    subject = Column(Text)
    metric = Column(Float)
    threshold = Column(Float)
    sample = Column(Integer)
    evidence = Column(Text)
    created_at = Column(Text)
    status = Column(Text, default="open", index=True)
    occurrences = Column(Integer, default=1)
    last_seen_at = Column(Text)
    acked_by = Column(Text)
    acked_at = Column(Text)
    assignee = Column(Text)
    mute_until = Column(Text)
    notified_at = Column(Text)
    resolved_at = Column(Text)

