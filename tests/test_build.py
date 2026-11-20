from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from tests.conftest import WorkbookFactory
from tsa_throughput.build import (
    combine,
    cross_check,
    daily_totals,
    discover,
    manifest,
    parse_cached,
    parse_release,
    select_sources,
    write_outputs,
)
from tsa_throughput.parse import SCHEMA, FileReport, ParsedFile, ParseError

D1, D2, D3 = date(2022, 6, 26), date(2022, 6, 27), date(2022, 6, 28)


def parsed(name: str, rows: list[tuple[date, int, str, str, int]]) -> ParsedFile:
    data = pl.DataFrame(
        [(d, h, a, f"{a} Intl", "City", "ST", c, p) for d, h, a, c, p in rows],
        schema=SCHEMA,
        orient="row",
    )
    report = FileReport(
        file=name, layout="standard", sheets=1, rows=data.height, pax_total=int(data["pax"].sum()),
        first_date=data["date"].min(), last_date=data["date"].max(),  # type: ignore[arg-type]
        named_start=None, named_end=None, metric="Total Pax", blank_rows=0, header_rows=0,
        label_rows=0, issues=(),
    )  # fmt: skip
    return ParsedFile(data=data, report=report)


@pytest.fixture
def overlapping() -> list[ParsedFile]:
    # "short" covers D1-D2; the re-post "long_0" covers D1-D3 and has an extra row on D2.
    short = parsed("week.xlsx", [(D1, 0, "ATL", "Main", 10), (D2, 0, "ATL", "Main", 20)])
    long = parsed(
        "week_0.xlsx",
        [
            (D1, 0, "ATL", "Main", 10),
            (D2, 0, "ATL", "Main", 21),
            (D2, 0, "BOS", "E2", 5),
            (D3, 0, "ATL", "Main", 30),
        ],
    )
    return [short, long]


def test_each_day_comes_from_one_file(overlapping: list[ParsedFile]) -> None:
    sources = select_sources(overlapping)
    assert sources.select("date", "file").rows() == [
        (D1, "week_0.xlsx"),  # tie on rows -> lexicographically last name
        (D2, "week_0.xlsx"),  # more rows wins
        (D3, "week_0.xlsx"),
    ]
    assert sources["candidates"].to_list()[0] == ["week.xlsx", "week_0.xlsx"]

    hourly = combine(overlapping, sources)
    assert hourly.select("date", "airport", "pax").rows() == [
        (D1, "ATL", 10),
        (D2, "ATL", 21),
        (D2, "BOS", 5),
        (D3, "ATL", 30),
    ]


def test_daily_totals(overlapping: list[ParsedFile]) -> None:
    hourly = combine(overlapping, select_sources(overlapping))
    daily = daily_totals(hourly)
    assert daily.rows() == [(D1, 10, 1, 1), (D2, 26, 2, 2), (D3, 30, 1, 1)]
    assert daily.schema["pax"] == pl.Int64


def test_combine_rejects_duplicate_keys() -> None:
    dup = parsed("x.xlsx", [(D1, 0, "ATL", "Main", 1), (D1, 0, "ATL", "Main", 2)])
    with pytest.raises(ValueError, match="duplicate keys"):
        combine([dup], select_sources([dup]))


def test_manifest_counts_days_used(overlapping: list[ParsedFile]) -> None:
    files = manifest(overlapping, select_sources(overlapping))
    assert dict(files.select("file", "days_used").rows()) == {"week.xlsx": 0, "week_0.xlsx": 3}


def test_write_outputs_splits_years_and_removes_stale(tmp_path: Path, overlapping: list[ParsedFile]) -> None:
    stale = tmp_path / "hourly" / "tsa_throughput_hourly_1999.parquet"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    sources = select_sources(overlapping)
    hourly = combine(overlapping, sources)

    write_outputs(hourly, daily_totals(hourly), manifest(overlapping, sources), tmp_path)

    assert sorted(p.name for p in (tmp_path / "hourly").iterdir()) == ["tsa_throughput_hourly_2022.parquet"]
    assert pl.read_parquet(tmp_path / "hourly" / "tsa_throughput_hourly_2022.parquet").equals(hourly)
    assert pl.read_csv(tmp_path / "tsa_throughput_daily.csv", try_parse_dates=True)["pax"].to_list() == [
        10,
        26,
        30,
    ]
    assert (tmp_path / "source_files.csv").exists()


def test_discover_groups_formats_by_stem_pdf_first(tmp_path: Path) -> None:
    for name in ("a.xlsx", "a.pdf", "b.xlsx", "~$b.xlsx", "notes.csv"):
        (tmp_path / name).write_bytes(b"")
    releases = discover(tmp_path)
    assert [(r.stem, [p.name for p in r.files]) for r in releases] == [
        ("a", ["a.pdf", "a.xlsx"]),
        ("b", ["b.xlsx"]),
    ]


def test_cross_check_ignores_checkpoint_naming_but_not_counts() -> None:
    pdf = parsed("w.pdf", [(D1, 0, "BGM", "0210", 5), (D1, 0, "BGM", "0211", 1)])
    same = parsed("w.xlsx", [(D1, 0, "BGM", "210", 5), (D1, 0, "BGM", "211", 1)])
    other = parsed("w.xlsx", [(D1, 0, "BGM", "210", 5), (D1, 0, "BGM", "211", 2)])
    assert cross_check(pdf, same) == "matches w.xlsx"
    assert cross_check(pdf, other).startswith("differs from w.xlsx: 1 date-hour-airport")


def _week_sheet() -> list[list[object]]:
    header: list[object] = [
        "Date",
        "Hour of Day",
        "Airport",
        None,
        "City",
        "State",
        "Checkpoint",
        "Total Pax",
    ]
    row: list[object] = [
        datetime(2022, 5, 1),
        "00:00",
        "ANC",
        "Anchorage Intl",
        "Anchorage",
        "AK",
        "South",
        10,
    ]
    return [header, row]


def test_release_falls_back_to_excel_when_pdf_fails(make_workbook: WorkbookFactory, tmp_path: Path) -> None:
    xlsx = make_workbook("week.xlsx", [_week_sheet()])
    (tmp_path / "week.pdf").write_bytes(b"not a pdf")
    (release,) = discover(tmp_path)

    result = parse_release(release, cache_dir=None, fingerprint="x")

    assert isinstance(result, ParsedFile)
    assert result.report.file == xlsx.name
    assert result.report.alternates == ("week.pdf",)
    assert any(issue.startswith("used fallback; week.pdf") for issue in result.report.issues)


def test_release_with_no_parseable_file_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "week.pdf").write_bytes(b"not a pdf")
    (release,) = discover(tmp_path)
    assert isinstance(parse_release(release, cache_dir=None, fingerprint="x"), ParseError)


def test_parse_cache_round_trips(make_workbook: WorkbookFactory, tmp_path: Path) -> None:
    path = make_workbook("week.xlsx", [_week_sheet()])
    cache = tmp_path / "cache"
    first = parse_cached(path, cache, "fp")
    assert len(list(cache.glob("*.parquet"))) == 1
    second = parse_cached(path, cache, "fp")
    assert second.data.equals(first.data)
    assert second.report == first.report
    parse_cached(path, cache, "new-parser")
    assert len(list(cache.glob("*.parquet"))) == 2  # a parser change gets its own entry
