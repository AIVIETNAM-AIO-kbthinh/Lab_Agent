"""P4 tự sinh schema: bảng template_drafts (mẫu đơn mới chờ admin duyệt)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "template_drafts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("owner_session", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=32), nullable=True),
        sa.Column("actor", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PREVIEWED"),
        sa.Column("form_id", sa.String(length=100), nullable=False),
        sa.Column("schema_json", JSONB, nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("blank_docx", sa.LargeBinary(), nullable=False),
        sa.Column("template_docx", sa.LargeBinary(), nullable=False),
        sa.Column("preview_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("preview_mime", sa.String(length=100), nullable=True),
        sa.Column("preview_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shown_hash", sa.String(length=64), nullable=True),
        sa.Column("report_json", JSONB, nullable=False, server_default="{}"),
        sa.Column("op_id", sa.String(length=32), nullable=True),
        sa.Column("result_json", JSONB, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_template_drafts_owner_session", "template_drafts", ["owner_session"])
    op.create_index("ix_template_drafts_status", "template_drafts", ["status"])
    op.create_index("ix_template_drafts_expires_at", "template_drafts", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_template_drafts_expires_at", table_name="template_drafts")
    op.drop_index("ix_template_drafts_status", table_name="template_drafts")
    op.drop_index("ix_template_drafts_owner_session", table_name="template_drafts")
    op.drop_table("template_drafts")
