#!/usr/bin/env bash
# SpaceX Launches demo — REST → datetime parsing + ranking → Excel sink.
#
# Hits the public SpaceX API (no auth), pulls launch records, parses the
# UTC date strings into proper datetimes, ranks launches by date, writes
# an Excel report.
#
#   rest_api_fetcher → select_columns → datetime_parser → rank → dataframe_to_excel

set -euo pipefail

PROJECT_DIR="${1:-spacex-demo}"

echo ">>> Creating project at $PROJECT_DIR"
mkdir -p "$PROJECT_DIR" && cd "$PROJECT_DIR"

uv init --python 3.11 --no-readme >/dev/null 2>&1 || true
uv add -q "dagster>=1.10.0" "dagster-webserver" "pandas" "requests" "openpyxl"

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components"
$CLI add rest_api_fetcher    --auto-install
$CLI add select_columns      --auto-install
$CLI add datetime_parser     --auto-install
$CLI add rank                --auto-install
$CLI add dataframe_to_excel  --auto-install

echo ">>> Writing definitions.py"
cat > definitions.py <<'PY'
"""SpaceX Launches demo — datetime parsing + ranking + Excel output.

Hits the public SpaceX API, picks human-interesting columns, parses launch
dates, ranks each launch by date (newest first), writes an Excel sheet.

Pipeline:
    rest_api_fetcher → select_columns → datetime_parser → rank → dataframe_to_excel
"""
import importlib.util
from pathlib import Path

import dagster as dg


def _load(category: str, component_id: str, class_name: str):
    here = Path(__file__).parent
    path = here / "components" / "assets" / category / component_id / "component.py"
    spec = importlib.util.spec_from_file_location(f"_dcc_{component_id}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


RestApiFetcher    = _load("ingestion",  "rest_api_fetcher",    "RestApiFetcherComponent")
SelectColumns     = _load("transforms", "select_columns",      "SelectColumnsComponent")
DatetimeParser    = _load("transforms", "datetime_parser",     "DatetimeParser")
Rank              = _load("transforms", "rank",                "RankComponent")
DataframeToExcel  = _load("sinks",      "dataframe_to_excel",  "DataframeToExcelComponent")


# 1. Fetch — array of launches, no auth, no json_path needed (the response IS the array)
ingest = RestApiFetcher.model_validate({
    "asset_name": "launches_raw",
    "api_url": "https://api.spacexdata.com/v4/launches",
    "method": "GET",
    "auth_type": "none",
    "output_format": "dataframe",
    "description": "All SpaceX launches — public API, no auth",
    "group_name": "ingest",
})

# 2. Pick the human-interesting columns
selected = SelectColumns.model_validate({
    "asset_name": "launches_clean",
    "upstream_asset_key": "launches_raw",
    "columns": ["name", "date_utc", "success", "rocket", "flight_number", "details"],
    "reorder": True,
    "group_name": "transform",
})

# 3. Parse the date_utc string into a proper timestamp
dated = DatetimeParser.model_validate({
    "asset_name": "launches_dated",
    "upstream_asset_key": "launches_clean",
    "date_column": "date_utc",
    "output_column": "launch_date",
    "extract_components": False,  # don't add year/month/day cols, keep clean
    "group_name": "transform",
})

# 4. Rank launches by date — newest first
ranked = Rank.model_validate({
    "asset_name": "launches_ranked",
    "upstream_asset_key": "launches_dated",
    "column": "launch_date",
    "method": "dense",
    "ascending": False,           # rank=1 means most recent
    "output_column": "rank_by_date",
    "group_name": "transform",
})

# 5. Write the report to Excel
write_xlsx = DataframeToExcel.model_validate({
    "asset_name": "launches_report",
    "upstream_asset_key": "launches_ranked",
    "file_path": "/tmp/spacex_launches.xlsx",
    "sheet_name": "Launches",
    "include_index": False,
    "group_name": "sink",
})

defs = dg.Definitions.merge(
    ingest.build_defs(None),
    selected.build_defs(None),
    dated.build_defs(None),
    ranked.build_defs(None),
    write_xlsx.build_defs(None),
)
PY

if ! grep -q "\[tool.dagster\]" pyproject.toml 2>/dev/null; then
  cat >> pyproject.toml <<'TOML'

[tool.dagster]
module_name = "definitions"
TOML
fi

cat <<MSG

>>> Setup complete.

Run the pipeline:
    cd $PROJECT_DIR
    uv run dagster asset materialize --select '*' -m definitions

Output: /tmp/spacex_launches.xlsx — every SpaceX launch ever, ranked by
date, with parsed datetime + flight metadata.

Inspect:
    uv run python -c "
    import pandas as pd
    df = pd.read_excel('/tmp/spacex_launches.xlsx')
    print(f'Total launches: {len(df)}')
    print(f'Columns: {list(df.columns)}')
    print(df.head(5).to_string())
    "
MSG
