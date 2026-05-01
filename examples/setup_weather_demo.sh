#!/usr/bin/env bash
# Open-Meteo weather demo — REST → datetime → running_total → transpose → CSV.
#
# Hits the public Open-Meteo API (no auth, no key), pulls the past 14 days
# of NYC weather, parses dates, computes running precipitation, transposes
# into a "metric per day" matrix, writes a CSV.
#
#   rest_api_fetcher → datetime_parser → running_total → transpose → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-weather-demo}"

echo ">>> Creating project at $PROJECT_DIR"
mkdir -p "$PROJECT_DIR" && cd "$PROJECT_DIR"

uv init --python 3.11 --no-readme >/dev/null 2>&1 || true
uv add -q "dagster>=1.10.0" "dagster-webserver" "pandas" "requests"

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components"
$CLI add rest_api_fetcher    --auto-install
$CLI add datetime_parser     --auto-install
$CLI add running_total       --auto-install
$CLI add transpose           --auto-install
$CLI add dataframe_to_csv    --auto-install

echo ">>> Writing definitions.py"
cat > definitions.py <<'PY'
"""Open-Meteo weather demo — daily NYC weather pivoted into a metric-per-day matrix.

Pipeline:
    rest_api_fetcher → datetime_parser → running_total → transpose → dataframe_to_csv
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


RestApiFetcher    = _load("ingestion",  "rest_api_fetcher",  "RestApiFetcherComponent")
DatetimeParser    = _load("transforms", "datetime_parser",   "DatetimeParser")
RunningTotal      = _load("transforms", "running_total",     "RunningTotalComponent")
Transpose         = _load("transforms", "transpose",         "TransposeComponent")
DataframeToCsv    = _load("sinks",      "dataframe_to_csv",  "DataframeToCsvComponent")


# 1. Fetch — Open-Meteo's `daily` block is a dict of parallel lists; pd.DataFrame
#    handles that shape natively, so json_path="daily" gives us a clean df.
ingest = RestApiFetcher.model_validate({
    "asset_name": "weather_raw",
    "api_url": (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=40.71&longitude=-74.01"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        "&timezone=UTC&past_days=14&forecast_days=1"
    ),
    "method": "GET",
    "auth_type": "none",
    "output_format": "dataframe",
    "json_path": "daily",
    "description": "NYC daily weather, past 14 days + today",
    "group_name": "ingest",
})

# 2. Parse the time strings (YYYY-MM-DD) into proper dates
typed = DatetimeParser.model_validate({
    "asset_name": "weather_typed",
    "upstream_asset_key": "weather_raw",
    "date_column": "time",
    "input_format": "%Y-%m-%d",
    "output_format": "%Y-%m-%d",
    "group_name": "transform",
})

# 3. Cumulative precipitation across the window
cumul = RunningTotal.model_validate({
    "asset_name": "weather_with_cumulative_precip",
    "upstream_asset_key": "weather_typed",
    "value_column": "precipitation_sum",
    "output_column": "cumulative_precip_mm",
    "sort_by": "time",
    "sort_ascending": True,
    "agg_function": "sum",
    "group_name": "transform",
})

# 4. Transpose so dates are columns and metrics are rows — exec-summary shape
pivoted = Transpose.model_validate({
    "asset_name": "weather_by_date",
    "upstream_asset_key": "weather_with_cumulative_precip",
    "index_column": "time",
    "reset_column_name": "metric",
    "group_name": "transform",
})

# 5. Write the wide matrix as CSV
write_csv = DataframeToCsv.model_validate({
    "asset_name": "weather_report",
    "upstream_asset_key": "weather_by_date",
    "file_path": "/tmp/weather_report.csv",
    "include_index": False,
    "group_name": "sink",
})

defs = dg.Definitions.merge(
    ingest.build_defs(None),
    typed.build_defs(None),
    cumul.build_defs(None),
    pivoted.build_defs(None),
    write_csv.build_defs(None),
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

Output: /tmp/weather_report.csv — NYC weather pivoted, one row per metric
(temp_max / temp_min / precip / cumulative_precip), one column per day.

Inspect:
    column -t -s, /tmp/weather_report.csv
MSG
