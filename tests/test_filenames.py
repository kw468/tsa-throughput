from datetime import date

import pytest

from tsa_throughput.filenames import parse_date_range


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tsa-throughput-data-may-15-2022-to-may-21-2022.xlsx", (date(2022, 5, 15), date(2022, 5, 21))),
        ("January%2031%2C%202016%20to%20Feburary%206%2C%202016.xlsx", (date(2016, 1, 31), date(2016, 2, 6))),
        ("tsa_throughput_april_21._2019_to_april_27_2019.xlsx", (date(2019, 4, 21), date(2019, 4, 27))),
        ("tsa-throughput-janaury-17.-2021-to-january-23-2021.xlsx", (date(2021, 1, 17), date(2021, 1, 23))),
        (
            "tsa-total-throughput-october-9-2022to-october-15-2022.xlsx",
            (date(2022, 10, 9), date(2022, 10, 15)),
        ),
        (
            "tsa-throughput-data-to-november-1-2026-to-november-7-2026.xlsx",
            (date(2026, 11, 1), date(2026, 11, 7)),
        ),
        ("TSA Total Throughput March 6, 2022 to March 12, 2022.xlsx", (date(2022, 3, 6), date(2022, 3, 12))),
        (
            "tsa-total-thrpt-february-11-2024-to-february-17-2024_0.xlsx",
            (date(2024, 2, 11), date(2024, 2, 17)),
        ),
        ("august-16-2015-to-august-22-2015-throughput.xlsx", (date(2015, 8, 16), date(2015, 8, 22))),
        ("december_30_2018_to_january_5_2019.xlsx", (date(2018, 12, 30), date(2019, 1, 5))),
    ],
)
def test_parses_known_name_styles(name: str, expected: tuple[date, date]) -> None:
    assert parse_date_range(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "tsa-monthly-throughput-march-2013_0.xls",
        "tsa_totals.csv",
        "may-21-2022-to-may-15-2022.xlsx",  # reversed range
    ],
)
def test_returns_none_without_a_clear_range(name: str) -> None:
    assert parse_date_range(name) is None
