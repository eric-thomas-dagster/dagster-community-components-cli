#!/usr/bin/env bash
# Partitioned USGS Earthquakes demo — backfillable across any date range.
#
# Same pipeline as setup_earthquakes_demo.sh but with daily partitions
# spanning April 2026 (configurable). Each partition queries the USGS
# historical API for that one day. Backfill any range, materialize
# individual days, view per-partition output in the Dagster UI.
#
#   rest_api_fetcher → json_flatten → select_columns → sort → dataframe_to_json
#   (all 5 components partitioned daily)

set -euo pipefail

PROJECT_DIR="${1:-partitioned-earthquakes-demo}"
PARTITION_START="${2:-2026-04-01}"

echo ">>> Creating project at $PROJECT_DIR (partitions start $PARTITION_START)"
mkdir -p "$PROJECT_DIR" && cd "$PROJECT_DIR"

uv init --python 3.11 --no-readme >/dev/null 2>&1 || true
uv add -q "dagster>=1.10.0" "dagster-webserver" "pandas" "requests"

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components"
$CLI add rest_api_fetcher    --auto-install
$CLI add json_flatten        --auto-install
$CLI add select_columns      --auto-install
$CLI add sort                --auto-install
$CLI add dataframe_to_json   --auto-install

echo ">>> Writing definitions.py"
cat > definitions.py <<PY
"""Partitioned USGS Earthquakes demo.

Daily partitioned pipeline that backfills via the USGS historical query API.
Each daily partition fetches that one day's earthquake catalog and writes a
partition-suffixed JSONL file.

Pipeline (all assets daily-partitioned, start_date=$PARTITION_START):
    rest_api_fetcher → json_flatten → select_columns → sort → dataframe_to_json

The first asset uses {partition_date} / {partition_date_next} templating in the
USGS query URL params (starttime / endtime), so each partition pulls only that
day's quakes.
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


RestApiFetcher  = _load("ingestion",  "rest_api_fetcher",  "RestApiFetcherComponent")
JsonFlatten     = _load("transforms", "json_flatten",      "JsonFlattenComponent")
SelectColumns   = _load("transforms", "select_columns",    "SelectColumnsComponent")
Sort            = _load("transforms", "sort",              "SortComponent")
DataframeToJson = _load("sinks",      "dataframe_to_json", "DataframeToJsonComponent")


PSTART = "$PARTITION_START"


# 1. Fetch — daily-partitioned hit on the USGS historical query API
ingest = RestApiFetcher.model_validate({
    "asset_name": "earthquakes_raw",
    "api_url": "https://earthquake.usgs.gov/fdsnws/event/1/query",
    "method": "GET",
    "params": (
        '{"format": "geojson",'
        ' "starttime": "{partition_date}",'
        ' "endtime": "{partition_date_next}",'
        ' "minmagnitude": 4}'
    ),
    "auth_type": "none",
    "output_format": "dataframe",
    "json_path": "features",
    "partition_type": "daily",
    "partition_start": PSTART,
    "description": "USGS earthquake feed — daily-partitioned, M>=4, public, no auth",
    "group_name": "ingest",
})

# 2. Flatten — partitioned to inherit upstream's partitioning
flattened = JsonFlatten.model_validate({
    "asset_name": "earthquakes_flat",
    "upstream_asset_key": "earthquakes_raw",
    "column": "properties",
    "separator": "_",
    "max_depth": 2,
    "drop_original": True,
    "partition_type": "daily",
    "partition_start": PSTART,
    "group_name": "transform",
})

# 3. Select + rename
selected = SelectColumns.model_validate({
    "asset_name": "earthquakes_clean",
    "upstream_asset_key": "earthquakes_flat",
    "columns": ["id", "properties_mag", "properties_place", "properties_time", "properties_url"],
    "rename": {
        "properties_mag":   "magnitude",
        "properties_place": "place",
        "properties_time":  "timestamp_ms",
        "properties_url":   "usgs_url",
    },
    "reorder": True,
    "partition_type": "daily",
    "partition_start": PSTART,
    "group_name": "transform",
})

# 4. Sort biggest-first
sorted_quakes = Sort.model_validate({
    "asset_name": "earthquakes_sorted",
    "upstream_asset_key": "earthquakes_clean",
    "by": ["magnitude"],
    "ascending": False,
    "na_position": "last",
    "reset_index": True,
    "partition_type": "daily",
    "partition_start": PSTART,
    "group_name": "transform",
})

# 5. Sink — partition-suffixed file path so each day gets its own file
write_jsonl = DataframeToJson.model_validate({
    "asset_name": "earthquakes_report",
    "upstream_asset_key": "earthquakes_sorted",
    "file_path": "/tmp/earthquakes_{partition_key}.jsonl",
    "orient": "records",
    "lines": True,
    "date_format": "iso",
    "partition_type": "daily",
    "partition_start": PSTART,
    "group_name": "sink",
})

defs = dg.Definitions.merge(
    ingest.build_defs(None),
    flattened.build_defs(None),
    selected.build_defs(None),
    sorted_quakes.build_defs(None),
    write_jsonl.build_defs(None),
)
PY

if ! grep -q "\[tool.dagster\]" pyproject.toml 2>/dev/null; then
  cat >> pyproject.toml <<'TOML'

[tool.dagster]
module_name = "definitions"
TOML
fi

cat <<MSG

>>> Setup complete. Partitioned daily, start=$PARTITION_START.

Materialize a single day:
    cd $PROJECT_DIR
    uv run dagster asset materialize --partition 2026-04-15 --select '*' -m definitions

Materialize a range (backfill):
    uv run dagster asset materialize \\
        --partition-range 2026-04-10..2026-04-15 --select '*' -m definitions

Open the UI:
    uv run dagster dev
    # then in browser: pick any partition and click 'Materialize'

Output lands at /tmp/earthquakes_<partition>.jsonl per day, sorted biggest
magnitude first. Each day pulls its own historical window from USGS, no
auth required.
MSG
