"""baseline：重构后首个版本的表结构（与当时 create_all 建出的结构一致）

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DT = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("device_id", sa.String(100), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("device_type", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("mcp_url", sa.String(500), nullable=False),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("supported_data_types", sa.JSON(), nullable=False),
        sa.Column("hardware_info", sa.JSON(), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=False),
        sa.Column("is_online", sa.Boolean(), nullable=False),
        sa.Column("last_seen", DT, nullable=True),
        sa.Column("tools_refreshed_at", DT, nullable=True),
        sa.Column("created_at", DT, nullable=False),
        sa.Column("updated_at", DT, nullable=False),
    )
    op.create_table(
        "remote_agents",
        sa.Column("agent_id", sa.String(100), primary_key=True),
        sa.Column("card_url", sa.String(500), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("card", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("refreshed_at", DT, nullable=True),
        sa.Column("created_at", DT, nullable=False),
    )
    op.create_table(
        "intent_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(100), nullable=False),
        sa.Column("event_ids", sa.JSON(), nullable=False),
        sa.Column("data_summary", sa.Text(), nullable=False),
        sa.Column("intent_detected", sa.Boolean(), nullable=False),
        sa.Column("intent_type", sa.String(100), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("action_required", sa.Boolean(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(20), nullable=False),
        sa.Column("task_id", sa.String(100), nullable=True),
        sa.Column("task_state", sa.String(50), nullable=True),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", DT, nullable=False),
    )
    op.create_index("ix_intent_logs_device_id", "intent_logs", ["device_id"])
    op.create_index("ix_intent_logs_created_at", "intent_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("intent_logs")
    op.drop_table("remote_agents")
    op.drop_table("devices")
