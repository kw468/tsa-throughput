"""Combine parsed releases into the published datasets.

Each weekly release is a PDF (TSA's original) and, for most weeks, an Excel
conversion with the same file stem. The PDF is parsed first; the Excel file is
the fallback when the PDF cannot be parsed, and otherwise serves as an
independent cross-check of the PDF parse.

TSA sometimes re-posts a week (``..._0``) or posts overlapping ranges, so the
same day can appear in several releases. Each day is taken from exactly one
file, never mixed: the file with the most rows for that day, ties broken by
the lexicographically last file name (re-posts carry a ``_0`` suffix).
"""

import hashlib
import importlib.metadata
import json
import logging
import os
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import date
from functools import partial
from pathlib import Path

import polars as pl

from tsa_throughput import filenames, parse
from tsa_throughput.parse import KEY, FileReport, ParsedFile, ParseError, parse_workbook

log = logging.getLogger(__name__)

# Preferred first: the PDF is what TSA publishes; Excel files are conversions of it.
SOURCE_SUFFIXES = (".pdf", ".xlsx", ".xls")
HOURLY_DIR = "hourly"
HOURLY_STEM = "tsa_throughput_hourly"
DAILY_STEM = "tsa_throughput_daily"
MANIFEST = "source_files.csv"


def default_workers() -> int:
    """CPUs this process may use (respects scheduler/cgroup affinity)."""
    return max(1, len(os.sched_getaffinity(0)))


@dataclass(frozen=True)
class Release:
    """All files for one release (same stem), in ``SOURCE_SUFFIXES`` order."""

    stem: str
    files: tuple[Path, ...]


def discover(raw_dir: Path) -> list[Release]:
    """Group the PDFs and Excel files in ``raw_dir`` by stem, skipping Office lock files."""
    by_stem: dict[str, list[Path]] = {}
    for p in raw_dir.iterdir():
        if p.suffix.lower() in SOURCE_SUFFIXES and not p.name.startswith("~$"):
            by_stem.setdefault(p.stem, []).append(p)
    rank = {suffix: i for i, suffix in enumerate(SOURCE_SUFFIXES)}
    return [
        Release(stem, tuple(sorted(files, key=lambda p: rank[p.suffix.lower()])))
        for stem, files in sorted(by_stem.items())
    ]


def _parser_fingerprint() -> str:
    """Changes whenever the parsing code or the reader libraries change."""
    digest = hashlib.sha256()
    for module in (parse, filenames):
        digest.update(Path(module.__file__ or "").read_bytes())
    for dist in ("fastexcel", "pdfplumber", "pdfminer.six", "polars"):
        digest.update(f"{dist}={importlib.metadata.version(dist)}".encode())
    return digest.hexdigest()


def _cache_key(path: Path, fingerprint: str) -> str:
    stat = path.stat()
    return hashlib.sha256(
        f"{path.name}|{stat.st_size}|{stat.st_mtime_ns}|{fingerprint}".encode()
    ).hexdigest()[:24]


def _report_to_json(report: FileReport) -> str:
    return json.dumps(asdict(report), default=lambda v: v.isoformat() if isinstance(v, date) else v)


def _report_from_json(text: str) -> FileReport:
    raw = json.loads(text)
    for field in ("first_date", "last_date", "named_start", "named_end"):
        raw[field] = date.fromisoformat(raw[field]) if raw[field] else None
    raw["issues"], raw["alternates"] = tuple(raw["issues"]), tuple(raw["alternates"])
    return FileReport(**raw)


def parse_cached(path: Path, cache_dir: Path | None, fingerprint: str) -> ParsedFile:
    """``parse_workbook`` with an on-disk cache (PDFs take minutes to parse)."""
    if cache_dir is None:
        return parse_workbook(path)
    key = _cache_key(path, fingerprint)
    data_path, report_path = cache_dir / f"{key}.parquet", cache_dir / f"{key}.json"
    if data_path.exists() and report_path.exists():
        return ParsedFile(data=pl.read_parquet(data_path), report=_report_from_json(report_path.read_text()))
    parsed = parse_workbook(path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_path.with_name(f".{data_path.name}.{os.getpid()}.tmp")
    parsed.data.write_parquet(tmp)
    tmp.replace(data_path)
    report_path.write_text(_report_to_json(parsed.report))
    return parsed


def cross_check(primary: ParsedFile, other: ParsedFile) -> str:
    """Compare two parses of the same release by date, hour, and airport totals.

    Checkpoint names are left out because the Excel conversions drop leading
    zeros from numeric-looking names ("0210" -> "210").
    """
    by = ["date", "hour", "airport"]
    a = primary.data.group_by(by).agg(pl.col("pax").sum())
    b = other.data.group_by(by).agg(pl.col("pax").sum())
    joined = a.join(b, on=by, how="full", coalesce=True, suffix="_other")
    cells = joined.filter(pl.col("pax").ne_missing(pl.col("pax_other"))).height
    if cells == 0 and primary.data.height == other.data.height:
        return f"matches {other.report.file}"
    return (
        f"differs from {other.report.file}: {cells} date-hour-airport totals, "
        f"rows {primary.data.height} vs {other.data.height}"
    )


def parse_release(release: Release, cache_dir: Path | None, fingerprint: str) -> ParsedFile | ParseError:
    """Parse the preferred file of a release, falling back to the others; cross-check if possible."""
    failures: list[str] = []
    for i, path in enumerate(release.files):
        try:
            parsed = parse_cached(path, cache_dir, fingerprint)
        except ParseError as exc:
            failures.append(str(exc))
            continue
        report = parsed.report
        issues = [f"used fallback; {f}" for f in failures]
        check = ""
        for other_path in release.files[i + 1 :]:
            try:
                check = cross_check(parsed, parse_cached(other_path, cache_dir, fingerprint))
            except ParseError as exc:
                check = f"{other_path.name} failed to parse: {exc}"
            if not check.startswith("matches"):
                issues.append(f"cross-check {check}")
            break
        alternates = tuple(p.name for p in release.files if p != path)
        report = replace(
            report, issues=report.issues + tuple(issues), alternates=alternates, cross_check=check
        )
        return ParsedFile(data=parsed.data, report=report)
    return ParseError("; ".join(failures))


def parse_all(
    releases: Iterable[Release], workers: int, cache_dir: Path | None = None
) -> tuple[list[ParsedFile], list[ParseError]]:
    """Parse releases in parallel; returns successes and failures separately."""
    parsed: list[ParsedFile] = []
    errors: list[ParseError] = []
    work = partial(parse_release, cache_dir=cache_dir, fingerprint=_parser_fingerprint())
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(work, releases):
            if isinstance(result, ParseError):
                errors.append(result)
            else:
                parsed.append(result)
    return parsed, errors


def select_sources(parsed: list[ParsedFile]) -> pl.DataFrame:
    """Pick one source file per date; returns ``date, file, rows, candidates``."""
    counts = pl.concat(
        p.data.group_by("date").len("rows").with_columns(file=pl.lit(p.report.file)) for p in parsed
    )
    return (
        counts.sort(["date", "rows", "file"], descending=[False, True, True])
        .group_by("date", maintain_order=True)
        .agg(pl.col("file").first(), pl.col("rows").first(), candidates=pl.col("file").sort())
    )


def combine(parsed: list[ParsedFile], sources: pl.DataFrame) -> pl.DataFrame:
    """Stack the chosen (date, file) slices into one hourly table sorted by key."""
    chosen = sources.select("date", "file")
    hourly = (
        pl.concat(p.data.with_columns(file=pl.lit(p.report.file)) for p in parsed)
        .join(chosen, on=["date", "file"], how="semi")
        .drop("file")
        .sort(KEY)
    )
    dupes = hourly.filter(pl.struct(KEY).is_duplicated())
    if dupes.height:
        raise ValueError(f"{dupes.height} duplicate keys after source selection:\n{dupes.head()}")
    return hourly


def daily_totals(hourly: pl.DataFrame) -> pl.DataFrame:
    """National totals per day."""
    return (
        hourly.group_by("date")
        .agg(
            pax=pl.col("pax").sum(),
            airports=pl.col("airport").n_unique().cast(pl.Int32),
            checkpoints=pl.struct("airport", "checkpoint").n_unique().cast(pl.Int32),
        )
        .sort("date")
    )


def manifest(parsed: list[ParsedFile], sources: pl.DataFrame) -> pl.DataFrame:
    """One row per source workbook with its QA summary and the days it supplies."""
    used = sources.group_by("file").agg(days_used=pl.len().cast(pl.Int32))
    reports = [_report_row(p.report) for p in parsed]
    return (
        pl.DataFrame(reports)
        .join(used, on="file", how="left")
        .with_columns(pl.col("days_used").fill_null(0))
        .sort("first_date", "file")
    )


def _report_row(report: FileReport) -> dict[str, object]:
    row: dict[str, object] = dict(asdict(report))
    row["issues"] = "; ".join(report.issues)
    row["alternates"] = "; ".join(report.alternates)
    return row


def _write_atomic(df: pl.DataFrame, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    if path.suffix == ".csv":
        df.write_csv(tmp)
    else:
        df.write_parquet(tmp, compression="zstd", compression_level=19, statistics=True)
    tmp.replace(path)


def write_outputs(hourly: pl.DataFrame, daily: pl.DataFrame, files: pl.DataFrame, out_dir: Path) -> None:
    """Write yearly hourly parquet files, daily CSV/parquet, and the manifest."""
    hourly_dir = out_dir / HOURLY_DIR
    hourly_dir.mkdir(parents=True, exist_ok=True)
    written = set()
    for (year,), part in hourly.group_by(pl.col("date").dt.year(), maintain_order=True):
        path = hourly_dir / f"{HOURLY_STEM}_{year}.parquet"
        _write_atomic(part, path)
        written.add(path)
    for stale in set(hourly_dir.glob(f"{HOURLY_STEM}_*.parquet")) - written:
        log.warning("removing stale %s", stale.name)
        stale.unlink()
    _write_atomic(daily, out_dir / f"{DAILY_STEM}.csv")
    _write_atomic(daily, out_dir / f"{DAILY_STEM}.parquet")
    _write_atomic(files, out_dir / MANIFEST)


def build(
    raw_dir: Path, out_dir: Path, workers: int, keep_going: bool = False, cache_dir: Path | None = None
) -> pl.DataFrame:
    """Run the full pipeline; returns the manifest.

    Raises ``RuntimeError`` if any release fails to parse in every available
    format, unless ``keep_going`` is set, in which case failures are logged
    and skipped. ``cache_dir`` stores per-file parses between runs.
    """
    releases = discover(raw_dir)
    log.info("parsing %d releases with %d workers", len(releases), workers)
    parsed, errors = parse_all(releases, workers, cache_dir)
    for err in errors:
        log.error("%s", err)
    if errors and not keep_going:
        raise RuntimeError(f"{len(errors)} release(s) failed to parse; rerun with --keep-going to skip")

    sources = select_sources(parsed)
    overlaps = sources.filter(pl.col("candidates").list.len() > 1)
    log.info("%d days appear in more than one workbook", overlaps.height)
    for row in overlaps.iter_rows(named=True):
        log.debug("%s in %s; using %s", row["date"], row["candidates"], row["file"])

    hourly = combine(parsed, sources)
    daily = daily_totals(hourly)
    files = manifest(parsed, sources)
    write_outputs(hourly, daily, files, out_dir)
    for row in files.filter(pl.col("issues") != "").iter_rows(named=True):
        log.warning("%s: %s", row["file"], row["issues"])
    log.info(
        "wrote %d hourly rows, %d days (%s to %s)",
        hourly.height, daily.height, daily["date"].min(), daily["date"].max(),
    )  # fmt: skip
    return files
