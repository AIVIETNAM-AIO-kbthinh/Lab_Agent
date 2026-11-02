"""Sheets client: chỉ các method TK §4.3 cần."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.google.base import GoogleService

ValueRender = Literal["UNFORMATTED_VALUE", "FORMATTED_VALUE", "FORMULA"]


@dataclass(frozen=True)
class SheetProps:
    sheet_id: int
    title: str
    index: int
    row_count: int = 1000
    col_count: int = 26


class SheetsAPI(Protocol):
    async def get_tabs(self, spreadsheet_id: str) -> list[SheetProps]: ...

    async def get_values(
        self, spreadsheet_id: str, a1: str, render: ValueRender = "UNFORMATTED_VALUE"
    ) -> list[list[Any]]: ...

    async def update_values(
        self, spreadsheet_id: str, a1: str, values: list[list[Any]], raw: bool = False
    ) -> None: ...

    async def batch_update_values(
        self, spreadsheet_id: str, data: list[tuple[str, list[list[Any]]]], raw: bool = False
    ) -> None: ...

    async def append_values(self, spreadsheet_id: str, a1: str, values: list[list[Any]]) -> str: ...

    async def batch_update(
        self, spreadsheet_id: str, requests: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...

    async def copy_sheet_to(
        self, spreadsheet_id: str, sheet_id: int, dest_spreadsheet_id: str
    ) -> SheetProps: ...


class GoogleSheets:
    def __init__(self, creds: Any, timeout: float = 60) -> None:
        self._g = GoogleService("sheets", "v4", creds, timeout)

    async def get_tabs(self, spreadsheet_id: str) -> list[SheetProps]:
        res = await self._g.call(
            lambda s: s.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties(sheetId,title,index,gridProperties(rowCount,columnCount))",
            )
        )
        out = []
        for sh in res.get("sheets", []):
            p = sh["properties"]
            gp = p.get("gridProperties", {})
            out.append(
                SheetProps(
                    p["sheetId"],
                    p["title"],
                    p.get("index", 0),
                    gp.get("rowCount", 1000),
                    gp.get("columnCount", 26),
                )
            )
        return sorted(out, key=lambda x: x.index)

    async def get_values(
        self, spreadsheet_id: str, a1: str, render: ValueRender = "UNFORMATTED_VALUE"
    ) -> list[list[Any]]:
        res = await self._g.call(
            lambda s: (
                s.spreadsheets()
                .values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=a1,
                    valueRenderOption=render,
                    dateTimeRenderOption="SERIAL_NUMBER",
                    majorDimension="ROWS",
                )
            )
        )
        return list(res.get("values", []))

    async def update_values(
        self, spreadsheet_id: str, a1: str, values: list[list[Any]], raw: bool = False
    ) -> None:
        await self._g.call(
            lambda s: (
                s.spreadsheets()
                .values()
                .update(
                    spreadsheetId=spreadsheet_id,
                    range=a1,
                    body={"values": values},
                    valueInputOption="RAW" if raw else "USER_ENTERED",
                )
            )
        )

    async def batch_update_values(
        self, spreadsheet_id: str, data: list[tuple[str, list[list[Any]]]], raw: bool = False
    ) -> None:
        if not data:
            return
        body = {
            "valueInputOption": "RAW" if raw else "USER_ENTERED",
            "data": [{"range": r, "values": v} for r, v in data],
        }
        await self._g.call(
            lambda s: s.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheet_id, body=body)
        )

    async def append_values(self, spreadsheet_id: str, a1: str, values: list[list[Any]]) -> str:
        res = await self._g.call(
            lambda s: (
                s.spreadsheets()
                .values()
                .append(
                    spreadsheetId=spreadsheet_id,
                    range=a1,
                    body={"values": values},
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                )
            )
        )
        return str(res.get("updates", {}).get("updatedRange", ""))

    async def batch_update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not requests:
            return []
        res = await self._g.call(
            lambda s: s.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
        )
        return list(res.get("replies", []))

    async def copy_sheet_to(self, spreadsheet_id: str, sheet_id: int, dest_spreadsheet_id: str) -> SheetProps:
        res = await self._g.call(
            lambda s: (
                s.spreadsheets()
                .sheets()
                .copyTo(
                    spreadsheetId=spreadsheet_id,
                    sheetId=sheet_id,
                    body={"destinationSpreadsheetId": dest_spreadsheet_id},
                )
            )
        )
        gp = res.get("gridProperties", {})
        return SheetProps(
            res["sheetId"],
            res["title"],
            res.get("index", 0),
            gp.get("rowCount", 1000),
            gp.get("columnCount", 26),
        )
