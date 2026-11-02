"""Xuất file theo yêu cầu: bảng exports (file đã lọc chờ người yêu cầu tải về)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "exports",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("owner_session", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("dataset", sa.String(length=40), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("filters_json", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_exports_owner_session", "exports", ["owner_session"])
    op.create_index("ix_exports_expires_at", "exports", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_exports_expires_at", table_name="exports")
    op.drop_index("ix_exports_owner_session", table_name="exports")
    op.drop_table("exports")
