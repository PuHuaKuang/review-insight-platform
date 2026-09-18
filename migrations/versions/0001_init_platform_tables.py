import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_metric",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stat_date", sa.Date(), nullable=True),
        sa.Column("dim_type", sa.String(length=16), nullable=True),
        sa.Column("dim_value", sa.Text(), nullable=True),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("avg_rating", sa.Float(), nullable=True),
        sa.Column("negative", sa.Integer(), nullable=True),
        sa.Column("negative_rate", sa.Float(), nullable=True),
    )
    op.create_index("ix_daily_metric_stat_date", "daily_metric", ["stat_date"])
    op.create_index("ix_daily_metric_dim_type", "daily_metric", ["dim_type"])
    op.create_index("ix_daily_metric_dim_value", "daily_metric", ["dim_value"])
    op.create_index(
        "idx_daily_metric_dim", "daily_metric", ["dim_type", "dim_value", "stat_date"]
    )

    op.create_table(
        "topic_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stat_date", sa.Date(), nullable=True),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column("mentions", sa.Integer(), nullable=True),
        sa.Column("negative_mentions", sa.Integer(), nullable=True),
        sa.Column("negative_ratio", sa.Float(), nullable=True),
    )
    op.create_index("ix_topic_daily_stat_date", "topic_daily", ["stat_date"])
    op.create_index("ix_topic_daily_topic", "topic_daily", ["topic"])

    op.create_table(
        "release",
        sa.Column("version", sa.Text(), primary_key=True),
        sa.Column("released_at", sa.Text(), nullable=True),
        sa.Column("rollout_pct", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("first_seen", sa.Text(), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=True),
    )
    op.create_index("ix_release_released_at", "release", ["released_at"])

    op.create_table(
        "alert_rule",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Integer(), nullable=True),
        sa.Column("params", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=True),
    )

    op.create_table(
        "alert_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("level", sa.Text(), nullable=True),
        sa.Column("stat_date", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("metric", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("sample", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=True),
    )
    op.create_index("ix_alert_event_code", "alert_event", ["code"])
    op.create_index("ix_alert_event_stat_date", "alert_event", ["stat_date"])


def downgrade() -> None:
    for t in ["alert_event", "alert_rule", "release", "topic_daily", "daily_metric"]:
        op.drop_table(t)
