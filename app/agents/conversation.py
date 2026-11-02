"""Hội thoại lưu DB, append-only (không sửa/xoá lượt cũ). Hội thoại quá dài → mở hội thoại mới."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.roles import UserContext
from app.db.models import Conversation, Message
from app.errors import NotFound
from app.llm.types import Msg

MAX_MESSAGES = 80


class ConversationStore:
    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self.sm = sm

    async def open(
        self, conversation_id: str | None, ctx: UserContext, profile: str, model: str
    ) -> Conversation:
        """Lấy hội thoại của chính session này; khác role / quá dài / không có → tạo mới."""
        async with self.sm() as s, s.begin():
            if conversation_id:
                conv = await s.get(Conversation, conversation_id)
                if conv is not None and conv.session_public_id != ctx.session_id:
                    raise NotFound("conversation không thuộc session")
                if conv is not None and conv.role == ctx.role.value and conv.message_count < MAX_MESSAGES:
                    return conv
            conv = Conversation(
                session_public_id=ctx.session_id,
                account_id=ctx.account_id,
                role=ctx.role.value,
                profile=profile,
                model=model,
            )
            s.add(conv)
            await s.flush()
            return conv

    async def messages(self, conversation_id: str) -> list[Msg]:
        async with self.sm() as s:
            rows = (
                await s.scalars(
                    select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
                )
            ).all()
            return [dict(r.content_json) for r in rows]

    async def append(self, conversation_id: str, msg: Msg) -> None:
        async with self.sm() as s, s.begin():
            conv = await s.get(Conversation, conversation_id, with_for_update=True)
            if conv is None:
                raise NotFound("conversation")
            s.add(
                Message(
                    conversation_id=conversation_id,
                    seq=conv.message_count,
                    role=msg["role"],
                    content_json=msg,
                )
            )
            conv.message_count += 1

    async def set_model(self, conversation_id: str, model: str, degraded: bool) -> None:
        async with self.sm() as s, s.begin():
            await s.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(model=model, degraded=degraded)
            )

    async def history_for_ui(self, conversation_id: str, ctx: UserContext) -> list[dict[str, str]]:
        async with self.sm() as s:
            conv = await s.get(Conversation, conversation_id)
            if conv is None or conv.session_public_id != ctx.session_id:
                raise NotFound("conversation")
        out = []
        for m in await self.messages(conversation_id):
            text = "".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
            if text and m["role"] in ("user", "assistant"):
                if m["role"] == "user" and text.startswith("[") and "]\n" in text:
                    text = text.split("]\n", 1)[1]
                out.append({"role": m["role"], "text": text})
        return out
