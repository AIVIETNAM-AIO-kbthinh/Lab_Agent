"""form engine: cột bổ sung cho drafts, pending_actions

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # drafts: cách nộp (agent điền / file user tự điền), người dùng, siêu dữ liệu, bản docx cùng lần render, kết quả
    op.add_column("drafts", sa.Column("mode", sa.String(length=20), server_default="agent", nullable=False))
    op.add_column("drafts", sa.Column("role", sa.String(length=20), server_default="guest", nullable=False))
    op.add_column("drafts", sa.Column("account_id", sa.String(length=32), nullable=True))
    op.add_column("drafts", sa.Column("actor", sa.String(length=200), server_default="", nullable=False))
    op.add_column("drafts", sa.Column("meta_json", JSONB, server_default="{}", nullable=False))
    op.add_column("drafts", sa.Column("rendered_docx", sa.LargeBinary(), nullable=True))
    op.add_column("drafts", sa.Column("preview_mime", sa.String(length=100), nullable=True))
    op.add_column("drafts", sa.Column("original_upload_id", sa.String(length=32), nullable=True))
    op.add_column("drafts", sa.Column("op_id", sa.String(length=32), nullable=True))
    op.add_column("drafts", sa.Column("result_json", JSONB, nullable=True))

    op.add_column("pending_actions", sa.Column("summary", sa.Text(), server_default="", nullable=False))
    op.add_column(
        "pending_actions", sa.Column("role", sa.String(length=20), server_default="", nullable=False)
    )
    op.add_column(
        "pending_actions", sa.Column("actor", sa.String(length=200), server_default="", nullable=False)
    )
    op.add_column("pending_actions", sa.Column("op_id", sa.String(length=32), nullable=True))
    op.add_column("pending_actions", sa.Column("result_json", JSONB, nullable=True))
    op.create_index("ix_form_versions_status", "form_versions", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_form_versions_status", table_name="form_versions")
    for c in ("result_json", "op_id", "actor", "role", "summary"):
        op.drop_column("pending_actions", c)
    for c in (
        "result_json",
        "op_id",
        "original_upload_id",
        "preview_mime",
        "rendered_docx",
        "meta_json",
        "actor",
        "account_id",
        "role",
        "mode",
    ):
        op.drop_column("drafts", c)
