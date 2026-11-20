"""Command-line entry point: ``tsa-throughput``."""

import logging
from dataclasses import asdict
from pathlib import Path

import click
import polars as pl

from tsa_throughput.build import build, default_workers
from tsa_throughput.parse import ParseError, parse_workbook


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Log per-day overlap decisions.")
def main(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


@main.command("build")
@click.option("--raw-dir", type=click.Path(exists=True, file_okay=False, path_type=Path), default="raw")
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path), default="data")
@click.option("--workers", type=int, default=default_workers, show_default="CPUs available")
@click.option("--keep-going", is_flag=True, help="Skip releases that fail to parse instead of aborting.")
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=".cache/parsed",
    show_default=True,
    help="Per-file parse cache; invalidated automatically when the parser changes.",
)
@click.option("--no-cache", is_flag=True, help="Parse every file from scratch.")
def build_cmd(
    raw_dir: Path, out_dir: Path, workers: int, keep_going: bool, cache_dir: Path, no_cache: bool
) -> None:
    """Parse every release (PDF, else Excel) in RAW_DIR and write the datasets to OUT_DIR."""
    try:
        build(raw_dir, out_dir, workers, keep_going=keep_going, cache_dir=None if no_cache else cache_dir)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("inspect")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--rows", type=int, default=10, show_default=True)
def inspect_cmd(path: Path, rows: int) -> None:
    """Parse one PDF or workbook and print its QA report and first rows."""
    try:
        parsed = parse_workbook(path)
    except ParseError as exc:
        raise click.ClickException(str(exc)) from exc
    for field, value in asdict(parsed.report).items():
        click.echo(f"{field:>12}: {value}")
    with pl.Config(tbl_rows=rows, tbl_cols=-1, fmt_str_lengths=40):
        click.echo(parsed.data.head(rows))
