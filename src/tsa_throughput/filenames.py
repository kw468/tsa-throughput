"""Recover the reporting week from a TSA release's file name.

TSA file names are inconsistent (URL-encoded spaces, typos such as "janaury",
stray dots, missing separators), so this is a tolerant best-effort parser. The
result is only used to sanity-check the dates found inside the workbook.
"""

import re
from datetime import date
from urllib.parse import unquote

_MONTHS = {
    "january": 1, "janaury": 1, "jan": 1,
    "february": 2, "feburary": 2, "febuary": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "otober": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}  # fmt: skip

_SEP = r"[\s_.,-]*"
_DATE = re.compile(rf"([a-z]+){_SEP}(\d{{1,2}}){_SEP}(\d{{4}})")


def parse_date_range(name: str) -> tuple[date, date] | None:
    """Return the (first, last) day named in a release file name, or None.

    Expects two "<month> <day> <year>" groups, e.g.
    ``tsa-throughput-data-may-15-2022-to-may-21-2022.xlsx``.
    """
    text = unquote(name).lower()
    found: list[date] = []
    for month, day, year in _DATE.findall(text):
        if month not in _MONTHS:
            continue
        try:
            found.append(date(int(year), _MONTHS[month], int(day)))
        except ValueError:
            continue
    if len(found) != 2 or found[0] > found[1]:
        return None
    return found[0], found[1]
