"""Parse one TSA FOIA throughput workbook into a tidy hourly checkpoint table.

The workbooks are PDF-to-Excel conversions of a pivot-style report. Each sheet
(often one per PDF page) has a header row, then data rows in which the Date,
Hour, and Airport labels appear only on the first row of their group. Two
layouts occur:

* "standard": a label sits on the first row of its group, so a forward fill
  recovers it. Pages may repeat the header and the current labels.
* "centered": the Date and Hour labels sit on their own rows in the *middle*
  of their group (a vertically centered merged cell). A forward fill would
  assign the top half of every group to the previous hour. Airport labels are
  merged cells too, and a group split by a page break can carry its label on
  either side of the break. Airports are taken from the merged ranges (see
  ``_resolve_centered_airports``), hour groups are delimited by where the
  alphabetical airport sequence restarts, and Date/Hour labels are matched to
  groups by order (see ``_assign_centered_labels``).
"""

import itertools
import re
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import fastexcel
import pdfplumber
import polars as pl
from pdfplumber.utils.exceptions import PdfminerException

from tsa_throughput.filenames import parse_date_range

KEY = ("date", "hour", "airport", "checkpoint")
SCHEMA = pl.Schema(
    {
        "date": pl.Date,
        "hour": pl.Int8,
        "airport": pl.String,
        "airport_name": pl.String,
        "city": pl.String,
        "state": pl.String,
        "checkpoint": pl.String,
        "pax": pl.Int64,
    }
)

_HEADER_SCAN_ROWS = 40
_PAX_HEADER = re.compile(r"pax|throughput", re.IGNORECASE)
_HOUR = r"^\d{1,2}:\d{2}(:\d{2})?$"
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")
_TEXT_COLUMNS = ("airport", "airport_name", "city", "state", "checkpoint")


class ParseError(Exception):
    """A workbook could not be parsed into a trustworthy table."""


@dataclass(frozen=True)
class Layout:
    columns: dict[str, int]
    pax_label: str
    width: int


@dataclass(frozen=True)
class FileReport:
    """Summary and QA findings for one parsed workbook."""

    file: str
    layout: str
    sheets: int
    rows: int
    pax_total: int
    first_date: date
    last_date: date
    named_start: date | None
    named_end: date | None
    metric: str
    blank_rows: int
    header_rows: int
    label_rows: int
    issues: tuple[str, ...]
    # Filled in by the build when the same week also exists in another format.
    alternates: tuple[str, ...] = ()
    cross_check: str = ""


@dataclass(frozen=True)
class ParsedFile:
    data: pl.DataFrame
    report: FileReport


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text or None


def _find_layout(rows: Sequence[tuple[str | None, ...]]) -> tuple[int, Layout] | None:
    """Locate the header row and map canonical columns to positions."""
    for i, raw in enumerate(rows[:_HEADER_SCAN_ROWS]):
        cells = [_norm(v) for v in raw]
        if "Airport" not in cells or not any(c and c.startswith("Checkpoint") for c in cells):
            continue
        cols: dict[str, int] = {}
        for j, cell in enumerate(cells):
            match cell:
                case "Date":
                    cols["date"] = j
                case "Airport":
                    cols["airport"] = j
                case "City":
                    cols["city"] = j
                case "State":
                    cols["state"] = j
                case str() if cell.startswith("Hour"):
                    cols["hour"] = j
                case str() if cell.startswith("Checkpoint"):
                    cols["checkpoint"] = j
        missing = {"date", "hour", "airport", "city", "state", "checkpoint"} - cols.keys()
        if missing:
            raise ParseError(f"header row {i} lacks {sorted(missing)}: {cells}")

        # The airport name column has no header; it is the first blank one after "Airport".
        name_col = next((j for j in range(cols["airport"] + 1, cols["city"]) if cells[j] is None), None)
        if name_col is None:
            raise ParseError(f"no airport-name column between Airport and City: {cells}")
        cols["airport_name"] = name_col

        # The throughput label is usually in the header row, but one release puts it a row above.
        pax = [(j, c) for j, c in enumerate(cells) if c and _PAX_HEADER.search(c)]
        if not pax and i > 0:
            above = [_norm(v) for v in rows[i - 1]]
            pax = [(j, c) for j, c in enumerate(above) if c and _PAX_HEADER.search(c)]
        if len(pax) != 1:
            raise ParseError(f"expected one throughput column, found {pax}: {cells}")
        cols["pax"] = pax[0][0]
        return i, Layout(columns=cols, pax_label=pax[0][1], width=len(cells))
    return None


def _excel_sheets(path: Path) -> Iterator[pl.DataFrame]:
    reader = fastexcel.read_excel(path)
    for idx in range(len(reader.sheet_names)):
        # skip_rows=0 keeps leading blank rows so row positions match the sheet's own numbering.
        yield reader.load_sheet(idx, header_row=None, dtypes="string", skip_rows=0).to_polars()


def _is_page_number(row: Sequence[str | None]) -> bool:
    """A footer row: blank, or a lone page number in any one column."""
    filled = [c.strip() for c in row if c and c.strip()]
    return not filled or (len(filled) == 1 and filled[0].isdigit())


def _strip_footer(rows: list[list[str | None]]) -> list[list[str | None]]:
    """Some exports draw the page-number footer inside the table grid, below the last data row."""
    end = len(rows)
    while end and _is_page_number(rows[end - 1]):
        end -= 1
    return rows[:end]


def _pdf_pages(path: Path) -> Iterator[pl.DataFrame]:
    """One string table per PDF page.

    pdfplumber builds the grid from the ruling lines, so a label in a vertically
    merged cell lands in the first row of its span: the same layout as the
    standard Excel conversions.
    """
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            rows = _strip_footer([row for table in page.extract_tables() for row in table])
            page.close()
            width = max((len(r) for r in rows), default=0)
            padded = [list(r) + [None] * (width - len(r)) for r in rows]
            yield pl.DataFrame(padded, schema=[f"c{j}" for j in range(width)], orient="row")


def read_cells(path: Path) -> tuple[pl.DataFrame, Layout, int]:
    """Read every sheet (or PDF page) as strings and stack the rows below each header.

    Returns the stacked rows (canonical column names, all ``String``, in sheet
    order, plus 0-based ``sheet`` and ``sheet_row`` positions), the layout of
    the first sheet, and the number of sheets.
    """
    tables = _pdf_pages(path) if path.suffix.lower() == ".pdf" else _excel_sheets(path)
    frames: list[pl.DataFrame] = []
    first: Layout | None = None
    layout: Layout | None = None
    n_sheets = 0
    for idx, sheet in enumerate(tables):
        n_sheets += 1
        if sheet.height == 0:
            continue
        found = _find_layout(sheet.head(_HEADER_SCAN_ROWS).rows())
        if found is not None:
            start, layout = found[0] + 1, found[1]
        elif layout is not None and sheet.width == layout.width:
            start = 0  # continuation sheet without its own header
        else:
            raise ParseError(f"sheet {idx + 1} has no header and no compatible earlier layout")
        if first is None:
            first = layout
        elif layout.pax_label != first.pax_label:
            raise ParseError(f"metric changes within file: {first.pax_label!r} -> {layout.pax_label!r}")
        names = sheet.columns
        frames.append(
            sheet.slice(start).select(
                *(
                    pl.col(names[j]).cast(pl.String).alias(canon)
                    for canon, j in sorted(layout.columns.items())
                ),
                sheet=pl.lit(idx, pl.Int32),
                sheet_row=pl.int_range(start, sheet.height, dtype=pl.Int64),
            )
        )
    if first is None or not frames:
        raise ParseError("file has no data sheets")
    return pl.concat(frames, how="vertical"), first, n_sheets


def _parse_dates(col: pl.Expr) -> pl.Expr:
    return pl.coalesce(col.str.to_date(fmt, strict=False) for fmt in _DATE_FORMATS)


_XLSX_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_CELL_RANGE = re.compile(r"([A-Z]+)(\d+):([A-Z]+)(\d+)")


def _column_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + ord(ch) - ord("A") + 1
    return index - 1


def merged_row_spans(path: Path, sheet_index: int, column: int) -> list[tuple[int, int]]:
    """0-based (first_row, last_row) of the merged ranges whose left edge is ``column``.

    Reads the ``<mergeCells>`` list straight from the .xlsx package, since the
    Excel readers used here do not expose merges.
    """
    if path.suffix.lower() != ".xlsx":
        raise ParseError("merged-cell layout is only supported for .xlsx files")
    with zipfile.ZipFile(path) as zf:
        sheets = ET.fromstring(zf.read("xl/workbook.xml")).find("main:sheets", _XLSX_NS)
        if sheets is None:
            raise ParseError("workbook.xml has no sheets")
        rel_id = sheets[sheet_index].get(_REL_ID)
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        target = next(
            r.get("Target", "") for r in rels.iterfind("rel:Relationship", _XLSX_NS) if r.get("Id") == rel_id
        )
        member = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        spans = []
        with zf.open(member) as fh:
            for _, el in ET.iterparse(fh):
                if el.tag == f"{{{_XLSX_NS['main']}}}mergeCell":
                    m = _CELL_RANGE.fullmatch(el.get("ref", ""))
                    if m and _column_index(m[1]) == column:
                        spans.append((int(m[2]) - 1, int(m[4]) - 1))
                el.clear()
    return sorted(spans)


def _resolve_centered_airports(
    rows: pl.DataFrame, data: pl.DataFrame, spans: list[tuple[int, int]]
) -> tuple[list[str], int]:
    """Airport code for every data row of a centered-layout sheet.

    Multi-checkpoint airports are merged cells holding the code in their top
    row. A group split by a page break becomes two pieces (a range, or a single
    unmerged row), only one of which holds the code. An empty piece joins the
    neighbouring airport that keeps checkpoints in alphabetical order; if both
    do, the one that already owns its checkpoint names elsewhere in the file
    (each airport recurs in every hour group). Returns the codes and the
    number of pieces attributed.
    """
    code_at = dict(zip(rows["sheet_row"].to_list(), rows["airport"].to_list(), strict=True))
    sheet_rows = data["sheet_row"].to_list()
    checkpoints = data["checkpoint"].to_list()
    position = {r: i for i, r in enumerate(sheet_rows)}
    codes: list[str | None] = data["airport"].to_list()
    covered: set[int] = set()
    pending: list[list[int]] = []
    for first, last in spans:
        members = [position[r] for r in range(first, last + 1) if r in position]
        if not members:
            continue
        if any(codes[i] is not None for i in members if sheet_rows[i] != first):
            raise ParseError(f"airport label inside merged range starting at sheet row {first + 1}")
        covered.update(members)
        top = code_at.get(first)
        if top is None:
            pending.append(members)
        else:
            for i in members:
                codes[i] = top
    # A one-row remnant of a split group needs no merge, so it is just an unlabelled loose row.
    pending.extend([i] for i, c in enumerate(codes) if c is None and i not in covered)
    pending.sort()

    owned = {(c, cp) for c, cp in zip(codes, checkpoints, strict=True) if c is not None}
    for members in pending:
        first, last = members[0], members[-1]
        before = codes[first - 1] if first > 0 else None
        after = codes[last + 1] if last + 1 < len(codes) else None
        # Checkpoints are listed alphabetically within an airport, so joining a side must keep that order.
        fits_before = before is not None and checkpoints[first].casefold() > checkpoints[first - 1].casefold()
        fits_after = after is not None and checkpoints[last].casefold() < checkpoints[last + 1].casefold()
        feasible = [side for side, fits in ((before, fits_before), (after, fits_after)) if fits]
        votes = {side: sum((side, checkpoints[i]) in owned for i in members) for side in feasible}
        best = max(votes.values(), default=0)
        winners = feasible if len(feasible) == 1 else [s for s, v in votes.items() if v == best and v > 0]
        if len(winners) != 1:
            raise ParseError(
                f"cannot attribute page-split rows at sheet row {sheet_rows[first] + 1} "
                f"to {before} or {after} (votes {votes})"
            )
        for i in members:
            codes[i] = winners[0]
    return [c for c in codes if c is not None], len(pending)


def _assign_centered_labels(df: pl.DataFrame, spans: list[tuple[int, int]]) -> tuple[pl.DataFrame, int]:
    """Recover airport, date, and hour for the centered layout (see module docstring).

    ``df`` holds every non-blank row of the sheet with ``row`` and ``sheet_row``
    indexes. Within an hour group airports are listed alphabetically, so a
    group starts wherever the airport code sorts before the previous one. The
    i-th hour label (in row order) belongs to the i-th group, and a new day
    starts whenever the hour does not increase. Each label must lie between its
    neighbouring groups. Returns the data rows and the number of page-split
    airport ranges that needed attribution.
    """
    is_data = pl.col("checkpoint").is_not_null() & pl.col("pax").is_not_null()
    data = df.filter(is_data)
    codes, n_split = _resolve_centered_airports(df, data, spans)
    data = data.with_columns(airport=pl.Series(codes, dtype=pl.String))

    # Hours appear in either the hour or the date column; dates only in the date column.
    hours = pl.concat(
        [df.filter(pl.col(c).str.contains(_HOUR)).select("row", label=pl.col(c)) for c in ("hour", "date")]
    ).sort("row")
    dates = df.filter(pl.col("date").is_not_null() & ~pl.col("date").str.contains(_HOUR)).select(
        "row", label=_parse_dates(pl.col("date"))
    )
    if dates["label"].null_count():
        raise ParseError("centered layout: unparseable date label")

    row_ids = data["row"].to_list()
    starts = [row_ids[0]] + [row_ids[k] for k in range(1, len(codes)) if codes[k] < codes[k - 1]]
    if len(starts) != hours.height:
        raise ParseError(f"centered layout: {len(starts)} hour groups but {hours.height} hour labels")

    hour_values = hours["label"].str.extract(r"^(\d{1,2})", 1).cast(pl.Int8).to_list()
    day_of_group = [0]
    for prev, cur in itertools.pairwise(hour_values):
        day_of_group.append(day_of_group[-1] + (cur <= prev))
    if day_of_group[-1] + 1 != dates.height:
        raise ParseError(f"centered layout: {day_of_group[-1] + 1} days but {dates.height} date labels")

    groups = pl.DataFrame(
        {
            "start": starts,
            "hour": hours["label"],
            "date": [dates["label"][d] for d in day_of_group],
            "day": day_of_group,
        },
        schema_overrides={"start": data.schema["row"]},
    )
    data = data.drop("date", "hour").join_asof(groups, left_on="row", right_on="start")

    group_spans = data.group_by("start").agg(lo=pl.col("row").min(), hi=pl.col("row").max()).sort("start")
    lo, hi = group_spans["lo"].to_list(), group_spans["hi"].to_list()
    for g, label_row in enumerate(hours["row"].to_list()):
        after_prev = g == 0 or label_row > hi[g - 1]
        before_next = g == len(lo) - 1 or label_row < lo[g + 1]
        if not (after_prev and before_next):
            raise ParseError(f"centered layout: hour label at row {label_row} outside group {g}")
    day_spans = data.group_by("day").agg(lo=pl.col("row").min(), hi=pl.col("row").max()).sort("day")
    for d, label_row in enumerate(dates["row"].to_list()):
        if not day_spans["lo"][d] <= label_row <= day_spans["hi"][d]:
            raise ParseError(f"centered layout: date label at row {label_row} outside day {d}")
    return data.drop("start", "day").with_columns(pl.col("date").cast(pl.String)), n_split


def parse_workbook(path: Path) -> ParsedFile:
    """Parse one workbook into rows matching ``SCHEMA``, plus a QA report.

    Raises ``ParseError`` when the structure is not recognised or labels cannot
    be assigned unambiguously; softer anomalies are listed in ``report.issues``.
    """
    try:
        return _parse_workbook(path)
    except (
        ParseError,
        fastexcel.FastExcelError,
        PdfminerException,
        zipfile.BadZipFile,
        ET.ParseError,
    ) as exc:
        # Re-raise as ParseError: it pickles across processes, unlike the reader's own exceptions.
        raise ParseError(f"{path.name}: {type(exc).__name__}: {exc}") from None


def _parse_workbook(path: Path) -> ParsedFile:
    cells, layout, n_sheets = read_cells(path)
    issues: list[str] = []

    # The PDF conversion wraps text, and sometimes splits codes around wide glyphs ("MD W" for MDW).
    values = pl.col(pl.String)
    cells = (
        cells.with_row_index("row")
        .with_columns(
            pl.col("date").str.replace_all(r"[\r\n]", "").str.strip_chars(),
            pl.col("airport").str.replace_all(r"\s+", ""),
            *(
                pl.col(c).str.replace_all(r"\s+", " ").str.strip_chars()
                for c in (*_TEXT_COLUMNS[1:], "pax", "hour")
            ),
        )
        .with_columns(values.replace("", None))
    )

    content = pl.any_horizontal(values.is_not_null())
    blank_rows = cells.height - cells.filter(content).height
    is_header = (pl.col("airport") == "Airport") | (pl.col("pax") == layout.pax_label)
    header_rows = cells.filter(is_header).height
    cells = cells.filter(content & ~is_header.fill_null(False))

    is_data = pl.col("checkpoint").is_not_null() & pl.col("pax").is_not_null()
    is_label = (
        pl.col("checkpoint").is_null()
        & pl.col("pax").is_null()
        & pl.col("airport").is_null()
        & (pl.col("date").is_not_null() | pl.col("hour").is_not_null())
    )
    label_rows = cells.filter(is_label).height
    orphans = cells.filter(~is_data & ~is_label)
    if orphans.height:
        issues.append(f"{orphans.height} rows with neither data nor labels dropped")

    centered = label_rows > 0 or cells.filter(pl.col("date").str.contains(_HOUR)).height > 0
    if centered:
        if n_sheets != 1:
            raise ParseError("centered layout is only supported for single-sheet workbooks")
        spans = merged_row_spans(path, 0, layout.columns["airport"])
        data, n_split = _assign_centered_labels(cells, spans)
        if n_split:
            issues.append(f"{n_split} page-split airport groups attributed by checkpoint name")
    else:
        data = cells.filter(is_data)
        if data["date"][0] is None or data["hour"][0] is None or data["airport"][0] is None:
            raise ParseError("first data row lacks date/hour/airport labels")
        starts_group = pl.col("date").is_not_null() | pl.col("hour").is_not_null()
        if data.filter(starts_group & pl.col("airport").is_null()).height:
            raise ParseError("a date/hour group starts without an airport label")
        data = data.with_columns(pl.col("date", "hour").forward_fill())

    # Name/city/state are given once per airport group; fill them within that group only.
    data = data.with_columns(group=pl.col("airport").is_not_null().cum_sum())
    data = data.with_columns(
        pl.col("airport").forward_fill(),
        *(pl.col(c).forward_fill().over("group") for c in ("airport_name", "city", "state")),
    ).with_columns(
        # A page break can repeat the code without the name; borrow it from the same file.
        pl.col(c).fill_null(pl.col(c).drop_nulls().first().over("airport"))
        for c in ("airport_name", "city", "state")
    )

    hour = pl.col("hour").str.extract(r"^(\d{1,2}):\d{2}", 1).cast(pl.Int8, strict=False)
    pax = pl.col("pax").str.replace_all(",", "").cast(pl.Float64, strict=False)
    typed = data.with_columns(
        date_parsed=_parse_dates(pl.col("date")),
        hour_parsed=hour,
        pax_parsed=pax,
        city=pl.col("city").str.strip_chars_end(", "),
    )
    _require_parsed(typed, "date", "date_parsed")
    _require_parsed(typed, "hour", "hour_parsed")
    _require_parsed(typed, "pax", "pax_parsed")
    if typed.filter(~pl.col("hour_parsed").is_between(0, 23)).height:
        raise ParseError("hour outside 0-23")
    bad_pax = typed.filter((pl.col("pax_parsed") < 0) | (pl.col("pax_parsed") % 1 != 0))
    if bad_pax.height:
        raise ParseError(f"non-integer or negative throughput: {bad_pax['pax'].head(5).to_list()}")

    # Within an hour group, airports must be in alphabetical order; a break means mislabelled rows.
    order = typed.with_columns(prev=pl.col("airport").shift().over("date_parsed", "hour_parsed"))
    unordered = order.filter(pl.col("airport") < pl.col("prev")).height
    if unordered:
        issues.append(f"{unordered} rows break alphabetical airport order within an hour")

    out = (
        typed.select(
            date=pl.col("date_parsed"),
            hour=pl.col("hour_parsed"),
            airport=pl.col("airport"),
            airport_name=pl.col("airport_name"),
            city=pl.col("city"),
            state=pl.col("state"),
            checkpoint=pl.col("checkpoint"),
            pax=pl.col("pax_parsed").cast(pl.Int64),
        )
        .match_to_schema(SCHEMA)
        .sort(KEY)
    )
    issues.extend(_check(out, path.name))
    named = parse_date_range(path.name)
    report = FileReport(
        file=path.name,
        layout="centered" if centered else "standard",
        sheets=n_sheets,
        rows=out.height,
        pax_total=int(out["pax"].sum()),
        first_date=out["date"].min(),  # type: ignore[arg-type]
        last_date=out["date"].max(),  # type: ignore[arg-type]
        named_start=named[0] if named else None,
        named_end=named[1] if named else None,
        metric=layout.pax_label,
        blank_rows=blank_rows,
        header_rows=header_rows,
        label_rows=label_rows,
        issues=tuple(issues),
    )
    return ParsedFile(data=out, report=report)


def _require_parsed(df: pl.DataFrame, raw: str, parsed: str) -> None:
    bad = df.filter(pl.col(parsed).is_null())
    if bad.height:
        raise ParseError(f"{bad.height} unparseable {raw} values, e.g. {bad[raw].unique().head(5).to_list()}")


def _check(df: pl.DataFrame, name: str) -> list[str]:
    issues = []
    odd_codes = df.filter(~pl.col("airport").str.contains(r"^[A-Z0-9]{3,4}$"))["airport"].unique().sort()
    if odd_codes.len():
        issues.append(f"unusual airport codes {odd_codes.to_list()}")
    for col in ("airport_name", "city", "state"):
        if n := df[col].null_count():
            issues.append(f"{n} rows missing {col}")
    dupes = df.filter(pl.struct(KEY).is_duplicated()).height
    if dupes:
        issues.append(f"{dupes} rows share a date-hour-airport-checkpoint key")
    named = parse_date_range(name)
    if named:
        start, end = named
        outside = df.filter(~pl.col("date").is_between(start, end)).height
        if outside:
            issues.append(f"{outside} rows dated outside {start}..{end} named in the file")
        days = df["date"].n_unique()
        if days != (end - start).days + 1:
            issues.append(f"{days} distinct days, file name implies {(end - start).days + 1}")
    return issues
