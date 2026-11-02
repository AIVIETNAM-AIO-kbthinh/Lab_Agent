"""FakeDrive (cây thư mục trong bộ nhớ) + FakeGmail."""

from __future__ import annotations

import itertools
from email.message import EmailMessage
from typing import Any

from app.errors import NotFound
from app.google.drive import FOLDER, GSHEET, DriveChange, DriveFile

_ids = itertools.count(1)


class FakeDrive:
    def __init__(self, root_id: str = "ROOT") -> None:
        self.root_id = root_id
        self.files: dict[str, DriveFile] = {root_id: DriveFile(root_id, "isilab", FOLDER)}
        self.content: dict[str, bytes] = {}
        self.changes: list[DriveChange] = []
        self.page = 0
        self.uploads: list[DriveFile] = []
        self.fail_next: list[Exception] = []

    def add(
        self, path: str, mime: str = GSHEET, content: bytes = b"", file_id: str | None = None
    ) -> DriveFile:
        parent = self.root_id
        parts = path.strip("/").split("/")
        for p in parts[:-1]:
            existing = next(
                (f for f in self.files.values() if f.name == p and parent in f.parents and f.is_folder), None
            )
            if existing is None:
                existing = DriveFile(f"F{next(_ids)}", p, FOLDER, [parent])
                self.files[existing.id] = existing
            parent = existing.id
        f = DriveFile(
            file_id or f"F{next(_ids)}",
            parts[-1],
            mime,
            [parent],
            version="1",
            modified_time="2026-10-01T00:00:00Z",
        )
        self.files[f.id] = f
        self.content[f.id] = content
        return f

    async def about_email(self) -> str:
        return "lab@gmail.com"

    async def get_file(self, file_id: str) -> DriveFile:
        if file_id not in self.files:
            raise NotFound(file_id)
        return self.files[file_id]

    async def list_children(self, folder_id: str) -> list[DriveFile]:
        return [f for f in self.files.values() if folder_id in f.parents and not f.trashed]

    async def export(self, file_id: str, mime_type: str) -> bytes:
        return self.content[file_id]

    async def download(self, file_id: str) -> bytes:
        return self.content[file_id]

    async def create_folder(self, name: str, parent_id: str) -> DriveFile:
        f = DriveFile(f"F{next(_ids)}", name, FOLDER, [parent_id])
        self.files[f.id] = f
        return f

    async def upload(
        self,
        name: str,
        parent_id: str,
        data: bytes,
        mime_type: str,
        convert_to: str | None = None,
        app_properties: dict[str, str] | None = None,
    ) -> DriveFile:
        if self.fail_next:
            raise self.fail_next.pop(0)
        f = DriveFile(
            f"F{next(_ids)}",
            name,
            convert_to or mime_type,
            [parent_id],
            app_properties=dict(app_properties or {}),
        )
        self.uploads.append(f)
        self.files[f.id] = f
        self.content[f.id] = data
        return f

    async def move(self, file_id: str, add_parent: str, remove_parent: str) -> None:
        f = self.files[file_id]
        f.parents = [add_parent if p == remove_parent else p for p in f.parents]

    async def start_page_token(self) -> str:
        return str(self.page)

    async def list_changes(self, page_token: str) -> tuple[list[DriveChange], str | None, str | None]:
        out, self.changes = self.changes, []
        self.page += 1
        return out, None, str(self.page)


class FakeGmail:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_next: list[Exception] = []

    async def send(self, message: EmailMessage) -> str:
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.sent.append(message)
        return f"msg{len(self.sent)}"

    def subjects(self) -> list[str]:
        return [str(m["Subject"]) for m in self.sent]

    def bodies(self) -> list[str]:
        out: list[str] = []
        for m in self.sent:
            body: Any = m.get_body(("plain",))
            out.append(body.get_content() if body else m.get_content())
        return out
