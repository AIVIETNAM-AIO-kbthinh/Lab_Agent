"""Tra file theo đường dẫn từ drive.root_folder_id: resolve("data/Members") -> DriveFile.

Cache trong bộ nhớ + DB (drive_path_cache). Gặp 404 với ID đã cache thì tra lại.
"""

from __future__ import annotations

import unicodedata
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DrivePathCache
from app.errors import ConfigError, NotFound
from app.google.drive import FOLDER, DriveAPI, DriveFile


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip()


class PathCache(Protocol):
    async def get(self, path: str) -> tuple[str, str] | None: ...
    async def set(self, path: str, file_id: str, mime_type: str) -> None: ...
    async def drop(self, path: str) -> None: ...


class MemoryPathCache:
    def __init__(self) -> None:
        self._d: dict[str, tuple[str, str]] = {}

    async def get(self, path: str) -> tuple[str, str] | None:
        return self._d.get(path)

    async def set(self, path: str, file_id: str, mime_type: str) -> None:
        self._d[path] = (file_id, mime_type)

    async def drop(self, path: str) -> None:
        self._d.pop(path, None)


class DbPathCache:
    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sm

    async def get(self, path: str) -> tuple[str, str] | None:
        async with self._sm() as s:
            row = await s.scalar(select(DrivePathCache).where(DrivePathCache.path == path))
            return (row.file_id, row.mime_type) if row else None

    async def set(self, path: str, file_id: str, mime_type: str) -> None:
        async with self._sm() as s, s.begin():
            stmt = insert(DrivePathCache).values(path=path, file_id=file_id, mime_type=mime_type)
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["path"], set_={"file_id": file_id, "mime_type": mime_type}
                )
            )

    async def drop(self, path: str) -> None:
        async with self._sm() as s, s.begin():
            await s.execute(delete(DrivePathCache).where(DrivePathCache.path == path))


class PathResolver:
    def __init__(self, drive: DriveAPI, root_folder_id: str, cache: PathCache | None = None) -> None:
        self.drive = drive
        self.root_id = root_folder_id
        self._mem: dict[str, DriveFile] = {}
        self._cache = cache or MemoryPathCache()

    async def resolve(self, path: str, *, refresh: bool = False) -> DriveFile:
        path = "/".join(_norm(p) for p in path.strip("/").split("/") if p)
        if not path:
            return await self.drive.get_file(self.root_id)
        if not refresh:
            if path in self._mem:
                return self._mem[path]
            cached = await self._cache.get(path)
            if cached:
                try:
                    f = await self.drive.get_file(cached[0])
                    if not f.trashed:
                        self._mem[path] = f
                        return f
                except NotFound:
                    pass
                await self._cache.drop(path)
        f = await self._walk(path)
        self._mem[path] = f
        await self._cache.set(path, f.id, f.mime_type)
        return f

    async def _walk(self, path: str) -> DriveFile:
        parent_id = self.root_id
        found: DriveFile | None = None
        walked: list[str] = []
        for part in path.split("/"):
            walked.append(part)
            children = [c for c in await self.drive.list_children(parent_id) if _norm(c.name) == part]
            if not children:
                raise NotFound(f"Drive: không tìm thấy '{'/'.join(walked)}' dưới thư mục gốc")
            if len(children) > 1:
                raise ConfigError(
                    f"Drive: có {len(children)} mục cùng tên '{'/'.join(walked)}', cần giữ lại một"
                )
            found = children[0]
            parent_id = found.id
        assert found is not None
        return found

    async def forget_path(self, path: str) -> None:
        """Bỏ cache của một đường dẫn (file vừa bị chuyển chỗ / thay bằng file khác cùng tên)."""
        path = "/".join(_norm(p) for p in path.strip("/").split("/") if p)
        self._mem.pop(path, None)
        await self._cache.drop(path)

    async def file_id(self, path: str) -> str:
        return (await self.resolve(path)).id

    async def ensure_folder(self, path: str) -> DriveFile:
        parts = [p for p in path.strip("/").split("/") if p]
        parent = await self.resolve("")
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}" if cur else part
            try:
                parent = await self.resolve(cur)
            except NotFound:
                parent = await self.drive.create_folder(part, parent.id)
                self._mem[cur] = parent
                await self._cache.set(cur, parent.id, FOLDER)
        return parent

    async def path_of(self, file: DriveFile, max_depth: int = 12) -> str | None:
        """Đường dẫn tương đối từ root, hoặc None nếu file không nằm dưới root."""
        names = [file.name]
        cur = file
        for _ in range(max_depth):
            if not cur.parents:
                return None
            pid = cur.parents[0]
            if pid == self.root_id:
                return "/".join(reversed(names))
            cur = await self.drive.get_file(pid)
            names.append(cur.name)
        return None

    def forget(self) -> None:
        self._mem.clear()
