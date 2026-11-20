from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import openpyxl
import pytest

Row = Sequence[object]


class WorkbookFactory(Protocol):
    def __call__(self, name: str, sheets: Sequence[Sequence[Row]], merges: Sequence[str] = ()) -> Path: ...


@pytest.fixture
def make_workbook(tmp_path: Path) -> WorkbookFactory:
    """Write sheets (lists of rows) to ``tmp_path/name``; ``merges`` apply to the first sheet."""

    def _make(name: str, sheets: Sequence[Sequence[Row]], merges: Sequence[str] = ()) -> Path:
        wb = openpyxl.Workbook()
        for default in list(wb.worksheets):
            wb.remove(default)
        for i, rows in enumerate(sheets, start=1):
            ws = wb.create_sheet(f"Table {i}")
            for row in rows:
                ws.append(list(row))
        for ref in merges:
            wb.worksheets[0].merge_cells(ref)
        path = tmp_path / name
        wb.save(path)
        return path

    return _make
