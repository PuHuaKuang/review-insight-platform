"""alert_event lifecycle fields for ack/mute/resolve + dedup."""
import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alert_event") as batch_op:
        batch_op.add_column(sa.Column("status", sa.Text(), nullable=True, server_default="open"))
        batch_op.add_column(sa.Column("occurrences", sa.Integer(), nullable=True, server_default="1"))
        batch_op.add_column(sa.Column("last_seen_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("acked_by", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("acked_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("assignee", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("mute_until", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("notified_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resolved_at", sa.Text(), nullable=True))
    op.create_index("ix_alert_event_status", "alert_event", ["status"])


def downgrade() -> None:
    op.drop_index("ix_alert_event_status", table_name="alert_event")
    with op.batch_alter_table("alert_event") as batch_op:
        for col in [
            "status", "occurrences", "last_seen_at", "acked_by", "acked_at",
            "assignee", "mute_until", "notified_at", "resolved_at",
        ]:
            batch_op.drop_column(col)
