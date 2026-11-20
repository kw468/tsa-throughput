# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A pipeline that parses TSA's weekly FOIA "TSA Throughput" releases (PDFs, plus Excel conversions for most
weeks; `raw/`, untracked) into
published datasets in `data/` (tracked): yearly hourly parquet files, a daily national total
CSV/parquet, and `source_files.csv` (one QA row per workbook). The repository is **public**:
see "Publishing rules" below.

## Commands

```sh
uv sync                                   # environment (Python 3.12, uv-managed)
uv run tsa-throughput build               # raw/ -> data/; per-file parses cached in .cache/parsed
uv run tsa-throughput build --no-cache    # reparse everything (a cold build is ~30 CPU-hours of PDF parsing)
uv run tsa-throughput build --keep-going  # skip releases that fail instead of aborting
uv run tsa-throughput inspect raw/<file>  # parse one PDF/workbook, print FileReport + head
uv run just check                         # ruff lint + format check, pyrefly, pytest
uv run pytest tests/test_parse.py -k centered   # single test
```

`just` is a dev dependency, so invoke it via `uv run just`. The `Justfile` does
`import? 'local.just'` for untracked machine-specific recipes.

## Architecture

- `src/tsa_throughput/parse.py`: one PDF or workbook → tidy `pl.DataFrame` matching `SCHEMA` plus a
  `FileReport`. PDFs are read page by page with pdfplumber's ruling-line table finder, which puts a
  merged-cell label in the first row of its span. Excel files are read with fastexcel (calamine). All
  cells are strings; both feed the same code path. Columns are located by header *content*
  (`_find_layout`), never position. Two layouts:
  - **standard**: labels on the first row of each group → forward fill. Name/city/state are
    filled only within an airport group (`group` = cumsum of non-null airport).
  - **centered** (detected by label-only rows or hour strings in the Date column; only the
    2017-10-08 and 2018-06-24 weeks): labels sit mid-group in vertically merged cells. Airports come
    from `<mergeCell>` ranges read straight from the xlsx XML (`merged_row_spans`). Page-split
    groups are attributed by alphabetical checkpoint order, then by checkpoint names owned elsewhere
    in the file. Hour groups are where the airport sequence restarts, and Date/Hour labels are
    matched to groups by order. Every step asserts consistency and raises `ParseError` instead of
    guessing.
  - Hard failures raise `ParseError`; soft anomalies go to `FileReport.issues`, which ends up in
    `source_files.csv`.
- `src/tsa_throughput/build.py`: `discover` groups files by stem into a `Release`, PDF first. The
  first file that parses wins, and the next one is parsed as a cross-check (`cross_check` compares
  date × hour × airport totals; checkpoint names are excluded because Excel conversions drop leading
  zeros, e.g. "0210" → "210"). Parses are cached in `.cache/parsed`, keyed on the file's
  name/size/mtime plus a fingerprint of `parse.py`, `filenames.py`, and the reader library versions,
  so code changes invalidate the cache automatically. Parsing runs in a `ProcessPoolExecutor` (workers
  default to the CPU affinity). Then `select_sources` picks **one release per date** (most rows, tie →
  lexicographically last name, so `_0` re-posts win). Never mix files within a day; this is what
  prevents the duplicate-week double counting. `combine` fails on any duplicate
  `(date, hour, airport, checkpoint)`. Outputs are written atomically, and stale year files are
  removed.
- `src/tsa_throughput/filenames.py`: tolerant parser for the week range in TSA's inconsistent file
  names. It is used only for QA (dates outside the named range are reported, not dropped).

## Data invariants worth preserving

- `pax` is Int64 on purpose: polars does not widen Int32 sums, and full-dataset sums exceed 2^31.
- Within an hour group airports are alphabetical in the source, and the parser uses this as a QA
  check. Checkpoints are alphabetical within an airport in 2014–2021 (the centered-layout logic
  relies on this). From 2022, SEA lists "SCP IAF" before "SCP 1".
- Airport codes have all whitespace stripped (PDF conversion splits them around W/M). Other text is
  whitespace-collapsed only; names are kept as reported (no harmonization).
- Known non-issues in the build log: some TSA file names have wrong dates (e.g. year "2919"); the
  file contents are correct. The two centered-layout weeks' PDFs (Excel-printed, no ruling lines) fail
  to parse by design and fall back to their xlsx.

## Publishing rules

- `.gitignore` is an **allowlist**. Add a path there deliberately before it can be committed.
- Nothing tracked may reference a specific machine, user, home directory, or absolute path. Code
  takes paths from CLI options with relative defaults (`raw`, `data`). Machine-specific notes,
  job scripts, and recipes go in untracked files (`CLAUDE.local.md`, `local.just`).
- `raw/` (source workbooks and PDFs), `clean/` (legacy per-file outputs from an older script), and
  `scratch/` are never published.
