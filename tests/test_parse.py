from datetime import date, datetime

import polars as pl
import pytest

from tests.conftest import WorkbookFactory
from tsa_throughput.parse import SCHEMA, ParseError, _is_page_number, _strip_footer, parse_workbook

STANDARD_HEADER: list[object] = [
    "Date",
    "Hour of Day",
    "Airport",
    None,
    "City",
    "State",
    "Checkpoint",
    "Total Pax\n+ KCM PAX",
]
CENTERED_HEADER: list[object] = [
    "Date", None, "Hour of Day", None, "Airport", None, "City", "State", "Checkpoint", "Metrics",
    "PMIS - Total Customer Throughput (Unadjusted)",
]  # fmt: skip


def rows_of(df: pl.DataFrame) -> list[tuple[object, ...]]:
    return df.select("date", "hour", "airport", "checkpoint", "pax").rows()


def test_standard_layout_fills_labels_and_drops_page_furniture(make_workbook: WorkbookFactory) -> None:
    page1 = [
        STANDARD_HEADER,
        [datetime(2022, 5, 1), "00:00", "ANC", "Anchorage Intl", "Anchorage", "AK", "South", 10],
        [None, None, "ATL", "Atlanta Intl", "Atlanta,", "GA", "Main", 5],
        [None, None, None, None, None, None, "North", 0],
        [None, "01:00", "ANC", "Anchorage Intl", "Anchorage", "AK", "South", 7],
        [None] * 8,
        STANDARD_HEADER,  # header repeated mid-sheet at a page break
        ["5/1/202\n2", "01:00", "MD W", "Chicago Midway", "Chicago", "IL", "Main", "1,234"],
    ]
    page2 = [
        STANDARD_HEADER,
        [datetime(2022, 5, 2), "00:00", "ANC", "Anchorage Intl", "Anchorage", "AK", "South", 3],
    ]
    path = make_workbook("tsa-throughput-may-1-2022-to-may-2-2022.xlsx", [page1, page2])

    parsed = parse_workbook(path)

    assert parsed.data.schema == SCHEMA
    assert rows_of(parsed.data) == [
        (date(2022, 5, 1), 0, "ANC", "South", 10),
        (date(2022, 5, 1), 0, "ATL", "Main", 5),
        (date(2022, 5, 1), 0, "ATL", "North", 0),
        (date(2022, 5, 1), 1, "ANC", "South", 7),
        (date(2022, 5, 1), 1, "MDW", "Main", 1234),
        (date(2022, 5, 2), 0, "ANC", "South", 3),
    ]
    atl = parsed.data.filter(pl.col("airport") == "ATL")
    assert atl["airport_name"].to_list() == ["Atlanta Intl", "Atlanta Intl"]
    assert atl["city"].to_list() == ["Atlanta", "Atlanta"]
    report = parsed.report
    assert (report.layout, report.metric, report.issues) == ("standard", "Total Pax + KCM PAX", ())
    assert (report.header_rows, report.pax_total) == (1, 1259)


def test_airport_details_do_not_leak_between_airports(make_workbook: WorkbookFactory) -> None:
    sheet = [
        STANDARD_HEADER,
        [datetime(2022, 5, 1), "00:00", "ANC", "Anchorage Intl", "Anchorage", "AK", "South", 1],
        [None, None, "BOS", None, None, None, "E2", 2],
    ]
    parsed = parse_workbook(make_workbook("x.xlsx", [sheet]))

    bos = parsed.data.filter(pl.col("airport") == "BOS")
    assert bos["airport_name"].to_list() == [None]
    assert any("missing airport_name" in issue for issue in parsed.report.issues)


def test_throughput_label_above_header_row(make_workbook: WorkbookFactory) -> None:
    sheet = [
        ["Report Description"],
        [
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "Metrics",
            "PMIS - Total Customer Throughput (Unadjusted)",
        ],
        ["Date", "Hour", "Airport", None, "City", "State", "Checkpoint", None, None],
        [datetime(2014, 3, 1), "0:00", "ABQ", "Albuquerque", "Albuquerque", "NM", "A/B", None, 4],
        [None, "1:00", "ABQ", "Albuquerque", "Albuquerque", "NM", "A/B", None, 2],
    ]
    parsed = parse_workbook(make_workbook("monthly.xls.xlsx", [sheet]))

    assert rows_of(parsed.data) == [
        (date(2014, 3, 1), 0, "ABQ", "A/B", 4),
        (date(2014, 3, 1), 1, "ABQ", "A/B", 2),
    ]
    assert parsed.report.named_start is None


def centered_rows() -> list[list[object]]:
    """Two days x two hours; Date/Hour labels sit mid-group on their own rows."""

    def data(code: str, pax: int, hour: str | None = None) -> list[object]:
        return [None, None, hour, None, code, f"{code} Intl", "City", "ST", "Main", None, pax]

    def label(value: object) -> list[object]:
        return [value] + [None] * 10

    return [
        CENTERED_HEADER,
        data("ABQ", 1),
        label("00:00"),
        data("ANC", 2),
        data("ATL", 3),
        label(datetime(2018, 6, 24)),
        data("ANC", 4, hour="01:00"),  # small groups carry the hour on their first row
        data("BOS", 5),
        data("ABQ", 6),
        label("00:00"),
        label(datetime(2018, 6, 25)),
        data("ANC", 7),
        label("01:00"),
        data("ABQ", 8),
        data("BOS", 9),
    ]


def test_centered_layout_assigns_labels_by_group(make_workbook: WorkbookFactory) -> None:
    parsed = parse_workbook(make_workbook("june_24_2018_to_june_25_2018.xlsx", [centered_rows()]))

    d1, d2 = date(2018, 6, 24), date(2018, 6, 25)
    assert rows_of(parsed.data) == [
        (d1, 0, "ABQ", "Main", 1),
        (d1, 0, "ANC", "Main", 2),
        (d1, 0, "ATL", "Main", 3),
        (d1, 1, "ANC", "Main", 4),
        (d1, 1, "BOS", "Main", 5),
        (d2, 0, "ABQ", "Main", 6),
        (d2, 0, "ANC", "Main", 7),
        (d2, 1, "ABQ", "Main", 8),
        (d2, 1, "BOS", "Main", 9),
    ]
    assert parsed.report.layout == "centered"
    assert parsed.report.label_rows == 5
    assert parsed.report.issues == ()


def test_centered_layout_rejects_label_count_mismatch(make_workbook: WorkbookFactory) -> None:
    rows = centered_rows()
    del rows[12]  # drop the last "01:00" label
    with pytest.raises(ParseError, match="hour groups but"):
        parse_workbook(make_workbook("x.xlsx", [rows]))


def _centered(code: str | None, checkpoint: str, pax: int, hour: str | None = None) -> list[object]:
    name = f"{code} Intl" if code else None
    return [None, None, hour, None, code, name, "City" if code else None, "ST" if code else None,
            checkpoint, None, pax]  # fmt: skip


def test_centered_layout_attributes_page_split_groups(make_workbook: WorkbookFactory) -> None:
    rows = [
        CENTERED_HEADER,                       # sheet row 1
        _centered("ABQ", "A", 1),              # 2
        [None] * 2 + ["00:00"] + [None] * 8,   # 3  hour label row
        _centered("SAF", "Checkpoint 1", 2),   # 4
        _centered(None, "Checkpoint 1", 3),    # 5  empty merge E5:E6: SAN's group before a page break
        _centered(None, "Checkpoint 2", 4),    # 6
        _centered("SAN", "Checkpoint 3", 5),   # 7  merge E7:E8
        _centered(None, "Checkpoint 5", 6),    # 8
        [datetime(2018, 6, 24)] + [None] * 10, # 9  date label row
        _centered("ABQ", "A", 7, hour="01:00"),  # 10
        _centered("ROC", "Checkpoint A", 8),   # 11
        _centered(None, "Main Checkpoint", 9), # 12 unmerged remnant: ROC or RST? order says ROC
        _centered("RST", "Main Checkpoint", 10),  # 13
        _centered("SAF", "Checkpoint 1", 11),  # 14
        _centered("SAN", "Checkpoint 1", 12),  # 15 merge E15:E18
        _centered(None, "Checkpoint 2", 13),   # 16
        _centered(None, "Checkpoint 3", 14),   # 17
        _centered(None, "Checkpoint 5", 15),   # 18
    ]  # fmt: skip
    path = make_workbook("june_24_2018_to_june_24_2018.xlsx", [rows], merges=["E5:E6", "E7:E8", "E15:E18"])

    parsed = parse_workbook(path)

    got = parsed.data.select("hour", "airport", "checkpoint", "pax").rows()
    assert got == [
        (0, "ABQ", "A", 1),
        (0, "SAF", "Checkpoint 1", 2),
        (0, "SAN", "Checkpoint 1", 3),
        (0, "SAN", "Checkpoint 2", 4),
        (0, "SAN", "Checkpoint 3", 5),
        (0, "SAN", "Checkpoint 5", 6),
        (1, "ABQ", "A", 7),
        (1, "ROC", "Checkpoint A", 8),
        (1, "ROC", "Main Checkpoint", 9),
        (1, "RST", "Main Checkpoint", 10),
        (1, "SAF", "Checkpoint 1", 11),
        (1, "SAN", "Checkpoint 1", 12),
        (1, "SAN", "Checkpoint 2", 13),
        (1, "SAN", "Checkpoint 3", 14),
        (1, "SAN", "Checkpoint 5", 15),
    ]
    assert parsed.data["airport_name"].null_count() == 0
    assert parsed.report.issues == ("2 page-split airport groups attributed by checkpoint name",)


def test_unrecognised_sheet_raises(make_workbook: WorkbookFactory) -> None:
    with pytest.raises(ParseError, match="no header"):
        parse_workbook(make_workbook("x.xlsx", [[["something", "else"], [1, 2]]]))


def test_non_integer_throughput_raises(make_workbook: WorkbookFactory) -> None:
    sheet = [STANDARD_HEADER, [datetime(2022, 5, 1), "00:00", "ANC", "A", "B", "AK", "South", 1.5]]
    with pytest.raises(ParseError, match="non-integer"):
        parse_workbook(make_workbook("x.xlsx", [sheet]))


def test_pdf_footer_rows_are_stripped_from_page_end() -> None:
    data: list[str | None] = ["3/3/2024", "00:00", "ANC", "Anchorage", "AK", "South", "12"]
    rows: list[list[str | None]] = [
        data,
        [None, None, None, None, None, None, "7"],  # orphan pax mid-page: kept, so it gets reported
        data,
        ["12", None, None, None, None, None, None],  # page number under Date
        [None, None, None, None, "13", None, None],  # page number under State
        ["", None, None, None, None, None, None],
    ]
    assert _strip_footer(rows) == rows[:3]
    assert not _is_page_number(["12", None, "ANC"])
