"""Bảng DB backend (mục 7.5). Mọi thay đổi schema qua Alembic."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}  # noqa: RUF012


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ----------------------------------------------------------------- auth


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    lock_level: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    guest: Mapped[bool] = mapped_column(Boolean, default=False)
    dev_login_name: Mapped[str | None] = mapped_column(String(200))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    public_id: Mapped[str] = mapped_column(String(32), default=_uuid, unique=True)


class RateLimit(Base):
    __tablename__ = "rate_limits"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


# ----------------------------------------------------------------- chat


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_public_id: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str | None] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(20))
    profile: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(100))
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)


class Message(Base):
    """Append-only. content_json giữ nguyên block (kể cả block riêng của provider)."""

    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    content_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    __table_args__ = (UniqueConstraint("conversation_id", "seq"),)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_public_id: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100))
    ext: Mapped[str] = mapped_column(String(10))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# ----------------------------------------------------------------- form engine (P3 dùng; tạo bảng từ đầu)


class Draft(TimestampMixin, Base):
    __tablename__ = "drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    form_id: Mapped[str] = mapped_column(String(100))
    form_version: Mapped[str] = mapped_column(String(40))
    owner_session: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(20), default="COLLECTING")
    fields_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attachments_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    rendered_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    shown_hash: Mapped[str | None] = mapped_column(String(64))
    preview_version: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # agent = agent điền hộ (bắt buộc xem trước) | upload = user tự điền, lưu nguyên file đã gửi
    mode: Mapped[str] = mapped_column(String(20), default="agent", server_default="agent")
    role: Mapped[str] = mapped_column(String(20), default="guest", server_default="guest")
    account_id: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(200), default="", server_default="")
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    rendered_docx: Mapped[bytes | None] = mapped_column(LargeBinary)
    preview_mime: Mapped[str | None] = mapped_column(String(100))
    original_upload_id: Mapped[str | None] = mapped_column(String(32))
    op_id: Mapped[str | None] = mapped_column(String(32))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class PendingAction(TimestampMixin, Base):
    __tablename__ = "pending_actions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(50))
    owner_session: Mapped[str] = mapped_column(String(32), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    role: Mapped[str] = mapped_column(String(20), default="", server_default="")
    actor: Mapped[str] = mapped_column(String(200), default="", server_default="")
    op_id: Mapped[str | None] = mapped_column(String(32))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class FormVersion(TimestampMixin, Base):
    __tablename__ = "form_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    form_id: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), index=True)  # pending_review | active | inactive
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    roles: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    template_file_id: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("form_id", "version"),)


class TemplateDraft(TimestampMixin, Base):
    """Mẫu đơn mới admin gửi lên, chờ duyệt (P4): schema tự sinh + mẫu trắng + bản có placeholder + bản điền thử.

    PREVIEWED → (admin bấm Kích hoạt) CONFIRMING → ACTIVATED | FAILED; hoặc CANCELLED.
    """

    __tablename__ = "template_drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_session: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="PREVIEWED", index=True)
    form_id: Mapped[str] = mapped_column(String(100))
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_filename: Mapped[str] = mapped_column(String(255))
    source_sha256: Mapped[str] = mapped_column(String(64))
    blank_docx: Mapped[bytes] = mapped_column(LargeBinary)
    template_docx: Mapped[bytes] = mapped_column(LargeBinary)
    preview_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    preview_mime: Mapped[str | None] = mapped_column(String(100))
    preview_version: Mapped[int] = mapped_column(Integer, default=0)
    shown_hash: Mapped[str | None] = mapped_column(String(64))
    report_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    op_id: Mapped[str | None] = mapped_column(String(32))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Export(Base):
    """File xuất theo yêu cầu (lọc từ sheet lớn). Chỉ đúng session đã yêu cầu tải được, hết hạn thì bị xoá."""

    __tablename__ = "exports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_session: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(200), default="")
    dataset: Mapped[str] = mapped_column(String(40))
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    row_count: Mapped[int] = mapped_column(Integer)
    filters_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# ----------------------------------------------------------------- outbox


class OutboxOp(TimestampMixin, Base):
    __tablename__ = "outbox_ops"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(200), default="system")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_outbox_ready", "status", "next_attempt_at"),)


class OutboxStep(Base):
    __tablename__ = "outbox_steps"
    op_id: Mapped[str] = mapped_column(ForeignKey("outbox_ops.id", ondelete="CASCADE"), primary_key=True)
    step: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="DONE")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ----------------------------------------------------------------- job / đồng bộ


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    scope: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), default="DONE")  # CLAIMED | DONE
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Sequence(Base):
    __tablename__ = "sequences"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class DriveState(Base):
    __tablename__ = "drive_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DrivePathCache(Base):
    __tablename__ = "drive_path_cache"
    path: Mapped[str] = mapped_column(String(500), primary_key=True)
    file_id: Mapped[str] = mapped_column(String(100))
    mime_type: Mapped[str] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IndexDoc(Base):
    __tablename__ = "index_docs"
    doc_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    source: Mapped[str] = mapped_column(String(20), index=True)  # drive | web
    location: Mapped[str] = mapped_column(String(1000))  # path Drive hoặc URL
    title: Mapped[str] = mapped_column(String(500))
    access: Mapped[str] = mapped_column(String(20))
    doc_type: Mapped[str] = mapped_column(String(20))
    lang: Mapped[str | None] = mapped_column(String(10))
    doc_date: Mapped[date | None] = mapped_column(Date)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[str | None] = mapped_column(String(100))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    collection: Mapped[str] = mapped_column(String(100))
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WebPage(Base):
    __tablename__ = "web_pages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(80), index=True)
    url_canonical: Mapped[str] = mapped_column(String(1000))
    lang: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(500), default="")
    content_hash: Mapped[str | None] = mapped_column(String(64))
    doc_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | missing | deleted
    depth: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_fetched: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("source_id", "url_canonical", "lang"),)


class WebCrawlRun(Base):
    __tablename__ = "web_crawl_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(80), index=True)
    mode: Mapped[str] = mapped_column(String(10))  # quick | full
    status: Mapped[str] = mapped_column(String(20))  # RUNNING | APPLIED | BLOCKED | ABORTED | FAILED
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


# ----------------------------------------------------------------- LLM


class LLMUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    session_public_id: Mapped[str | None] = mapped_column(String(32), index=True)
    account_id: Mapped[str | None] = mapped_column(String(32), index=True)
    profile: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    first_token_ms: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(100))
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
