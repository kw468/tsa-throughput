# Run recipes with `uv run just <recipe>` (just is a dev dependency).

# Machine-specific recipes (e.g. job submission) live in an untracked local.just.
import? 'local.just'

default:
    @just --list

# Create or refresh the virtual environment
sync:
    uv sync

# Rebuild data/ from the workbooks in raw/ (extra args go to `tsa-throughput build`)
build *args:
    uv run tsa-throughput build {{ args }}

# Parse one workbook and print its QA report and first rows
inspect file:
    uv run tsa-throughput inspect "{{ file }}"

# Run tests, e.g. `just test -k centered`
test *args:
    uv run pytest {{ args }}

lint:
    uv run ruff check src tests
    uv run ruff format --check src tests

fmt:
    uv run ruff format src tests
    uv run ruff check --fix src tests

typecheck:
    uv run pyrefly check

# Lint, typecheck, and test
check: lint typecheck test
