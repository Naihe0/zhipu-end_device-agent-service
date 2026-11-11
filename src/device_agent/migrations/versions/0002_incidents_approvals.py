"""事件（incident）、工具审批、设备工具策略、意图类别

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DT = sa.DateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(
            sa.Column("tool_policies", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
    with op.batch_alter_table("intent_logs") as batch:
        batch.add_column(sa.Column("category", sa.String(30), nullable=False, server_default="other"))
        batch.add_column(sa.Column("incident_id", sa.String(100), nullable=True))
        batch.create_index("ix_intent_logs_incident_id", ["incident_id"])

    op.create_table(
        "incidents",
        sa.Column("incident_id", sa.String(100), primary_key=True),
        sa.Column("device_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("trigger_count", sa.Integer(), nullable=False),
        sa.Column("opened_at", DT, nullable=False),
        sa.Column("last_triggered_at", DT, nullable=False),
        sa.Column("closed_at", DT, nullable=True),
    )
    op.create_index("ix_incidents_device_id", "incidents", ["device_id"])
    op.create_index("ix_incidents_status", "incidents", ["status"])

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(100), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("device_id", sa.String(100), nullable=False),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("task_id", sa.String(100), nullable=True),
        sa.Column("context_id", sa.String(100), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", DT, nullable=False),
        sa.Column("expires_at", DT, nullable=False),
        sa.Column("decided_at", DT, nullable=True),
    )
    op.create_index("ix_approvals_status", "approvals", ["status"])
    op.create_index("ix_approvals_context_id", "approvals", ["context_id"])


def downgrade() -> None:
    op.drop_table("approvals")
    op.drop_table("incidents")
    with op.batch_alter_table("intent_logs") as batch:
        batch.drop_index("ix_intent_logs_incident_id")
        batch.drop_column("incident_id")
        batch.drop_column("category")
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("tool_policies")
