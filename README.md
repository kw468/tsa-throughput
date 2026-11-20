# TSA checkpoint throughput, by airport, checkpoint, and hour

Cleaned, combined versions of the weekly **TSA Throughput** releases that the
Transportation Security Administration publishes in its
[FOIA electronic reading room](https://www.tsa.gov/foia/readingroom): the number
of people screened at each security checkpoint in each hour, from March 2014
to November 2026.

TSA posts one PDF report per week: about 1,000 pages of a pivot-style table
with merged cells, repeated page headers, and the occasional re-posted week.
This repository turns those releases into two tidy tables:

| Dataset | Grain | Rows | Files |
| --- | --- | --- | --- |
| **Hourly** | date × hour × airport × checkpoint | 34.7 M | `data/hourly/tsa_throughput_hourly_<year>.parquet` |
| **Daily** | date (national total) | 4,546 | `data/tsa_throughput_daily.csv` (and `.parquet`) |

`data/source_files.csv` lists every source file with its date range, row
count, total, column label, cross-check result, and any QA notes, so each row
can be traced back.

This project is not affiliated with or endorsed by TSA.

## Quick start

```python
import polars as pl

hourly = pl.scan_parquet("data/hourly/*.parquet")
daily = pl.read_csv("data/tsa_throughput_daily.csv", try_parse_dates=True)

# Busiest checkpoints in 2025
(hourly.filter(pl.col("date").dt.year() == 2025)
       .group_by("airport", "checkpoint").agg(pl.col("pax").sum())
       .sort("pax", descending=True).head(10).collect())
```

```sql
-- DuckDB
SELECT airport, date_trunc('month', date) AS month, sum(pax) AS pax
FROM 'data/hourly/*.parquet'
GROUP BY ALL ORDER BY ALL;
```

```r
# R
library(arrow)
hourly <- open_dataset("data/hourly")
daily  <- read.csv("data/tsa_throughput_daily.csv")
```

## Columns

**Hourly** (`data/hourly/*.parquet`, one file per calendar year; the key is
`date, hour, airport, checkpoint`, which is unique across all files)

| Column | Type | Description |
| --- | --- | --- |
| `date` | date | Local calendar date |
| `hour` | int8 | Hour of day, 0–23 (`5` = 05:00–05:59) |
| `airport` | string | Airport code as reported (almost always the IATA code) |
| `airport_name` | string | Airport name as reported that week |
| `city` | string | City as reported that week |
| `state` | string | State or territory |
| `checkpoint` | string | Checkpoint name as reported |
| `pax` | int64 | People screened in that hour |

**Daily** (`data/tsa_throughput_daily.csv`)

| Column | Type | Description |
| --- | --- | --- |
| `date` | date | Local calendar date |
| `pax` | int64 | Sum of `pax` over all hours, airports, and checkpoints |
| `airports` | int32 | Airports reporting that day |
| `checkpoints` | int32 | Airport–checkpoint pairs reporting that day |

## Source files

The raw releases are not redistributed here. They are public:

- **May 2017 onward:** TSA FOIA Electronic Reading Room,
  <https://www.tsa.gov/foia/readingroom>, category *Throughput Data*. Files are
  served from `https://www.tsa.gov/sites/default/files/foia-readingroom/<name>.pdf`.
- **2013 – early 2017:** DHS, *TSA Customer Throughput/Wait Times Reports*,
  <https://www.dhs.gov/tsa-throughput-reports> (one page per year).
- **Archived copy:** ICPSR holds every PDF that was in the reading room on
  2025-05-21, <https://doi.org/10.3886/E230861>.

## Coverage

2014-03-01 through 2026-11-07. Every week from 2015-06-28 onward is complete.
The only gaps are weeks TSA never released: the DHS archive lists for 2014 and
2015 skip exactly these dates, and nothing was published for April–May 2014.

| Missing dates | Days |
| --- | --- |
| 2014-04-01 – 2014-05-17 | 47 |
| 2014-06-01 – 2014-06-07 | 7 |
| 2014-06-22 – 2014-06-28 | 7 |
| 2014-09-14 – 2014-09-27 | 14 |
| 2014-11-16 – 2014-11-22 | 7 |
| 2015-06-21 – 2015-06-27 | 7 |

## What to know before using it

- **What is counted.** The source column label changes over time:
  "PMIS – Total Customer Throughput (Unadjusted)" until early 2022,
  "Total Pax + KCM PAX" (KCM = Known Crewmember) after that (releases switch
  between late February and early April 2022), and
  "Total Pax + Crew PAX" from 2026-10-11. The label for each week is in
  `source_files.csv` (`metric`). Check for level shifts around these dates
  before relying on long-run comparisons.
- **Relation to TSA's published daily numbers.** Daily totals here run about
  2–3% below the national figures on
  [tsa.gov/travel/passenger-volumes](https://www.tsa.gov/travel/passenger-volumes),
  which TSA notes were later revised to include Known Crewmembers and late
  flights. A spot check of 2023 found most days 2.0–2.9% lower and a few
  up to about 5.6% lower, with complete hour and airport coverage on those days.
- **Names are as reported.** Airport names, cities, and checkpoint names change
  over time and are not harmonized. One airport code changes as well: Sawyer
  International (Marquette, MI) appears as `KSAW` (2014), `SAW`, and `MQT`.
  Zero-throughput rows are kept as reported.
- **Overlapping releases.** TSA occasionally re-posted a week or published
  overlapping ranges (13 days are covered by two releases). Each day is taken
  from exactly one release, the one with the most rows for that day; the
  `days_used` column in `source_files.csv` shows which.
- **Checkpoint names with leading zeros.** Some checkpoints are numbered
  (BGM `0210`, ITH `0240`, SLK `0282`, JHW `0250`). They are kept as TSA prints
  them; Excel conversions of the reports often show them as `210` etc.

## How the data were cleaned

The weekly PDF is the primary source. Each page's table is read from the PDF's
ruling lines (with [pdfplumber](https://github.com/jsvine/pdfplumber)), which
places every merged-cell label in the first row of its group. Where an Excel
conversion of the same week is also available, it is parsed independently and
the two are compared on every date × hour × airport total. All 641 weeks that
have both versions agree exactly (`cross_check` in `source_files.csv`). Excel is
used directly only for the March 2014 release, which TSA published as `.xls`, and
for two weeks (2017-10-08, 2018-06-24) whose PDFs were printed without table
lines.

All cells are read as text, and columns are found by header *content* rather
than position, so relabelled columns in new releases need no special cases.
For each file the pipeline:

1. Finds the header row on every page or sheet and maps Date, Hour, Airport, name, City,
   State, Checkpoint, and the throughput column. It drops blank rows, page-number
   footers, and headers repeated at page breaks.
2. Fills the Date, Hour, and Airport labels, which the report prints only on the
   first row of each group. Name, city, and state are filled only within the same
   airport, so they never carry over to the next airport.
3. Repairs conversion artifacts: dates broken across lines (`12/27/202⏎0`),
   airport codes split around wide letters (`MD W` → `MDW`), and stray
   whitespace and trailing commas.
4. Handles the two Excel-only weeks (2017-10-08 and 2018-06-24), whose labels
   sit in the *middle* of vertically merged cells. For these, airports are read from
   the merged ranges, groups split by a page break are attributed using
   checkpoint order and names, and hour groups are found where the
   alphabetical airport sequence restarts. Each step is checked for
   consistency, and the parser stops with an error rather than guessing.
5. Validates the result. Throughput must be a non-negative integer, hours 0–23,
   keys unique, airports in alphabetical order within each hour, and dates
   consistent with the week in the file name. Anything unusual is recorded in
   `source_files.csv` (`issues`).

The combined tables are then built by choosing one release per day (see above)
and checking that the hourly key is unique.

## Reproducing

Requires [uv](https://docs.astral.sh/uv/). Download the weekly PDFs (see
*Source files*) into `raw/`. Excel versions with the same file name are optional
and are used as a cross-check. Then:

```sh
uv sync
uv run tsa-throughput build              # writes data/
uv run tsa-throughput inspect raw/<file>  # QA report for one PDF or workbook
```

Reading a PDF takes about three minutes, so a first build is slow (about 30
CPU-hours for all releases, spread over all available cores). Parses are cached
in `.cache/`, so adding a week later only parses the new file. Development tasks
are in the `Justfile` (`uv run just --list`): `test`, `lint`, `typecheck`, `check`.

## License

Code: MIT (`LICENSE`). Data: CC0 1.0 (`data/LICENSE`). The underlying TSA
records are works of the U.S. federal government.
