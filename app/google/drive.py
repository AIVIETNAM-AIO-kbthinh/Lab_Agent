"""Drive client: chỉ các method TK §4.3 cần. Không có thao tác xoá."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Protocol

from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from app.google.base import GoogleService

FOLDER = "application/vnd.google-apps.folder"
GDOC = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"

FILE_FIELDS = (
    "id,name,mimeType,parents,modifiedTime,version,md5Checksum,trashed,webViewLink,size,appProperties"
)


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    parents: list[str] = field(default_factory=list)
    modified_time: str | None = None
    version: str | None = None
    md5: str | None = None
    trashed: bool = False
    web_view_link: str | None = None
    size: int | None = None
    app_properties: dict[str, str] = field(default_factory=dict)

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> DriveFile:
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            mime_type=d.get("mimeType", ""),
            parents=list(d.get("parents", [])),
            modified_time=d.get("modifiedTime"),
            version=str(d["version"]) if d.get("version") is not None else None,
            md5=d.get("md5Checksum"),
            trashed=bool(d.get("trashed", False)),
            web_view_link=d.get("webViewLink"),
            size=int(d["size"]) if d.get("size") else None,
            app_properties=dict(d.get("appProperties") or {}),
        )

    @property
    def link(self) -> str:
        if self.web_view_link:
            return self.web_view_link
        if self.mime_type == FOLDER:
            return f"https://drive.google.com/drive/folders/{self.id}"
        if self.mime_type == GDOC:
            return f"https://docs.google.com/document/d/{self.id}/edit"
        return f"https://drive.google.com/file/d/{self.id}/view"


@dataclass
class DriveChange:
    file_id: str
    removed: bool
    file: DriveFile | None


class DriveAPI(Protocol):
    async def about_email(self) -> str: ...
    async def get_file(self, file_id: str) -> DriveFile: ...
    async def list_children(self, folder_id: str) -> list[DriveFile]: ...
    async def export(self, file_id: str, mime_type: str) -> bytes: ...
    async def download(self, file_id: str) -> bytes: ...
    async def create_folder(self, name: str, parent_id: str) -> DriveFile: ...
    async def upload(
        self,
        name: str,
        parent_id: str,
        data: bytes,
        mime_type: str,
        convert_to: str | None = None,
        app_properties: dict[str, str] | None = None,
    ) -> DriveFile: ...
    async def move(self, file_id: str, add_parent: str, remove_parent: str) -> None: ...
    async def start_page_token(self) -> str: ...
    async def list_changes(self, page_token: str) -> tuple[list[DriveChange], str | None, str | None]: ...


class GoogleDrive:
    def __init__(self, creds: Any, timeout: float = 60) -> None:
        self._g = GoogleService("drive", "v3", creds, timeout)

    async def about_email(self) -> str:
        res = await self._g.call(lambda s: s.about().get(fields="user(emailAddress)"))
        return str(res["user"]["emailAddress"])

    async def get_file(self, file_id: str) -> DriveFile:
        res = await self._g.call(
            lambda s: s.files().get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True)
        )
        return DriveFile.from_api(res)

    async def list_children(self, folder_id: str) -> list[DriveFile]:
        out: list[DriveFile] = []
        token: str | None = None
        while True:

            def req(s: Any, t: str | None = token) -> Any:
                return s.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields=f"nextPageToken, files({FILE_FIELDS})",
                    pageSize=1000,
                    pageToken=t,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )

            res = await self._g.call(req)
            out.extend(DriveFile.from_api(f) for f in res.get("files", []))
            token = res.get("nextPageToken")
            if not token:
                return out

    async def export(self, file_id: str, mime_type: str) -> bytes:
        res = await self._g.call(lambda s: s.files().export(fileId=file_id, mimeType=mime_type))
        return bytes(res)

    async def download(self, file_id: str) -> bytes:
        def _dl(s: Any) -> bytes:
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, s.files().get_media(fileId=file_id, supportsAllDrives=True))
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=0)
            return buf.getvalue()

        return await self._g.call_raw(_dl)

    async def create_folder(self, name: str, parent_id: str) -> DriveFile:
        body = {"name": name, "mimeType": FOLDER, "parents": [parent_id]}
        res = await self._g.call(
            lambda s: s.files().create(body=body, fields=FILE_FIELDS, supportsAllDrives=True)
        )
        return DriveFile.from_api(res)

    async def upload(
        self,
        name: str,
        parent_id: str,
        data: bytes,
        mime_type: str,
        convert_to: str | None = None,
        app_properties: dict[str, str] | None = None,
    ) -> DriveFile:
        body: dict[str, Any] = {"name": name, "parents": [parent_id]}
        if convert_to:
            body["mimeType"] = convert_to
        if app_properties:
            body["appProperties"] = app_properties
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
        res = await self._g.call(
            lambda s: s.files().create(
                body=body, media_body=media, fields=FILE_FIELDS, supportsAllDrives=True
            )
        )
        return DriveFile.from_api(res)

    async def move(self, file_id: str, add_parent: str, remove_parent: str) -> None:
        await self._g.call(
            lambda s: s.files().update(
                fileId=file_id,
                addParents=add_parent,
                removeParents=remove_parent,
                fields="id",
                supportsAllDrives=True,
            )
        )

    async def start_page_token(self) -> str:
        res = await self._g.call(lambda s: s.changes().getStartPageToken(supportsAllDrives=True))
        return str(res["startPageToken"])

    async def list_changes(self, page_token: str) -> tuple[list[DriveChange], str | None, str | None]:
        """Trả (changes, next_page_token, new_start_page_token)."""
        res = await self._g.call(
            lambda s: s.changes().list(
                pageToken=page_token,
                pageSize=500,
                includeRemoved=True,
                spaces="drive",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                fields=f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))",
            )
        )
        changes = [
            DriveChange(
                c["fileId"], bool(c.get("removed")), DriveFile.from_api(c["file"]) if c.get("file") else None
            )
            for c in res.get("changes", [])
        ]
        return changes, res.get("nextPageToken"), res.get("newStartPageToken")
