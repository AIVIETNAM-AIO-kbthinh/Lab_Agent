"""data/AuditLog: mọi thao tác ghi của hệ thống thêm đúng một dòng (TK §3.5)."""

from __future__ import annotations

import json
from typing import Any

from app import clock
from app.config import PathOnlyCfg
from app.google.paths import PathResolver
from app.google.sheets import SheetsAPI
from app.logging import get_logger
from app.sheets.a1 import quote_tab

HEADER = ["thoi_gian", "actor", "role", "action", "target", "ket_qua", "chi_tiet"]
log = get_logger(__name__)


class AuditLog:
    def __init__(self, sheets: SheetsAPI, paths: PathResolver, cfg: PathOnlyCfg) -> None:
        self.sheets = sheets
        self.paths = paths
        self.cfg = cfg
        self._tab: str | None = cfg.tab

    async def _target(self) -> tuple[str, str]:
        sid = await self.paths.file_id(self.cfg.path)
        if self._tab is None:
            tabs = await self.sheets.get_tabs(sid)
            self._tab = tabs[0].title
        return sid, self._tab

    async def log(
        self,
        actor: str,
        role: str,
        action: str,
        target: str,
        ok: bool = True,
        detail: Any = None,
        *,
        raise_on_error: bool = False,
    ) -> None:
        detail_s = (
            detail if isinstance(detail, str) or detail is None else json.dumps(detail, ensure_ascii=False)
        )
        row = [
            clock.now().strftime("%d/%m/%Y %H:%M:%S"),
            actor,
            role,
            action,
            target,
            "OK" if ok else "FAILED",
            (detail_s or "")[:5000],
        ]
        try:
            sid, tab = await self._target()
            await self.sheets.append_values(sid, f"{quote_tab(tab)}!A1:G1", [row])
        except Exception as e:
            # AuditLog lỗi không được làm hỏng thao tác chính; ghi log để không mất dấu
            log.error("audit_write_failed", action=action, target=target, error=str(e)[:300])
            if raise_on_error:
                raise

    async def recent(
        self, *, limit: int = 200, query: str | None = None, action: str | None = None
    ) -> list[dict[str, str]]:
        """Dòng mới nhất trước (chỉ đọc; trang admin). Lọc theo chữ trong actor/target/chi tiết, hoặc action."""
        sid, tab = await self._target()
        values = await self.sheets.get_values(sid, f"{quote_tab(tab)}!A1:G", render="FORMATTED_VALUE")
        rows: list[dict[str, str]] = []
        q = (query or "").strip().casefold()
        for raw in values:
            cells = [str(c) for c in raw] + [""] * (len(HEADER) - len(raw))
            if not any(cells) or cells[0] == HEADER[0]:
                continue
            row = dict(zip(HEADER, cells[: len(HEADER)], strict=True))
            if action and row["action"] != action:
                continue
            if q and q not in " ".join((row["actor"], row["target"], row["chi_tiet"])).casefold():
                continue
            rows.append(row)
        return list(reversed(rows))[:limit]
