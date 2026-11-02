"""Form registry: bảng form_versions (cache ngắn trong bộ nhớ).

- Mỗi (form_id, version) một dòng. Đơn đang dùng = bản `active` mới nhất.
- Phiên bản cũ vẫn tra được theo version: bản nháp đang dở render bằng phiên bản đã tạo ra nó (TK §7.7).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import FormVersion
from app.forms.schema import FormSchema


@dataclass(frozen=True)
class FormDef:
    schema: FormSchema
    version: str
    status: str
    template_file_id: str | None

    @property
    def form_id(self) -> str:
        return self.schema.form_id


def _to_def(r: FormVersion) -> FormDef:
    return FormDef(FormSchema.model_validate(r.schema_json), r.version, r.status, r.template_file_id)


class FormRegistry:
    def __init__(self, sm: async_sessionmaker[AsyncSession], ttl_s: float = 30) -> None:
        self.sm = sm
        self.ttl_s = ttl_s
        self._active: dict[str, FormDef] | None = None
        self._at = 0.0

    def invalidate(self) -> None:
        self._active = None

    async def _load_active(self) -> dict[str, FormDef]:
        if self._active is not None and time.monotonic() - self._at < self.ttl_s:
            return self._active
        async with self.sm() as s:
            rows = (await s.scalars(select(FormVersion).where(FormVersion.status == "active"))).all()
        latest: dict[str, FormVersion] = {}
        for r in rows:
            if r.form_id not in latest or r.version > latest[r.form_id].version:
                latest[r.form_id] = r
        self._active = {k: _to_def(v) for k, v in latest.items()}
        self._at = time.monotonic()
        return self._active

    async def active(self, form_id: str) -> FormDef | None:
        return (await self._load_active()).get(form_id)

    async def list_active(self) -> list[FormDef]:
        return sorted((await self._load_active()).values(), key=lambda d: d.schema.title)

    async def for_role(self, role: str) -> list[FormDef]:
        return [d for d in await self.list_active() if role in d.schema.roles]

    async def get(self, form_id: str, version: str) -> FormDef | None:
        async with self.sm() as s:
            r = await s.scalar(
                select(FormVersion).where(FormVersion.form_id == form_id, FormVersion.version == version)
            )
        return _to_def(r) if r else None

    async def upsert(
        self, schema: FormSchema, *, status: str = "active", template_file_id: str | None = None
    ) -> None:
        """Ghi một phiên bản. `active` ⇒ các phiên bản active cũ của cùng form chuyển `inactive`."""
        values = {
            "form_id": schema.form_id,
            "version": schema.version,
            "status": status,
            "title": schema.title,
            "description": schema.description,
            "roles": list(schema.roles),
            "schema_json": schema.dump(),
            "template_file_id": template_file_id,
        }
        async with self.sm() as s, s.begin():
            if status == "active":
                await s.execute(
                    update(FormVersion)
                    .where(
                        FormVersion.form_id == schema.form_id,
                        FormVersion.status == "active",
                        FormVersion.version != schema.version,
                    )
                    .values(status="inactive")
                )
            stmt = insert(FormVersion).values(**values)
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["form_id", "version"],
                    set_={k: v for k, v in values.items() if k not in ("form_id", "version")},
                )
            )
        self.invalidate()

    async def deactivate(self, form_id: str) -> int:
        async with self.sm() as s, s.begin():
            res = await s.execute(
                update(FormVersion)
                .where(FormVersion.form_id == form_id, FormVersion.status == "active")
                .values(status="inactive")
                .returning(FormVersion.id)
            )
            n = len(res.all())
        self.invalidate()
        return n
