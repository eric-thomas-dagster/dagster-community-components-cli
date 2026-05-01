#!/usr/bin/env bash
# USGS Earthquakes JSON pipeline demo.
#
# Hits the USGS public earthquake feed (no auth), flattens nested JSON,
# selects + renames columns, sorts by magnitude, writes JSONL output.
#
# Demonstrates a different category mix than the Titanic and Penguins demos:
# REST-API ingest + JSON manipulation + JSON sink.
#
#   rest_api_fetcher → json_flatten → select_columns → sort → dataframe_to_json

set -euo pipefail

PROJECT_DIR="${1:-earthquakes-demo}"

echo ">>> Creating project at $PROJECT_DIR"
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
cat > definitions.py <<'PY'
"""USGS Earthquakes JSON pipeline demo.

Hits the USGS public earthquake feed (no auth), flattens nested JSON,
selects + renames columns, sorts by magnitude descending, writes the
result to /tmp/earthquakes.jsonl.

Pipeline:
    rest_api_fetcher → json_flatten → select_columns → sort → dataframe_to_json

Loads each Component class via importlib (since the source repo's
`dagster_component_templates` namespace is the legacy one). For projects
that prefer pip install, replace the importlib block with:

    from dagster_community_components import (
        RestApiFetcherComponent, JsonFlattenComponent,
        SelectColumnsComponent, SortComponent,
        DataframeToJsonComponent,
    )

then `pip install dagster-community-components` and remove the loader.
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
JsonFlatten       = _load("transforms", "json_flatten",        "JsonFlattenComponent")
SelectColumns     = _load("transforms", "select_columns",      "SelectColumnsComponent")
Sort              = _load("transforms", "sort",                "SortComponent")
DataframeToJson   = _load("sinks",      "dataframe_to_json",   "DataframeToJsonComponent")


# 1. Fetch — public USGS earthquake feed (last 24 hours, all magnitudes)
#    Pull the `features` array out of the GeoJSON, return as a DataFrame.
ingest = RestApiFetcher.model_validate({
    "asset_name": "earthquakes_raw",
    "api_url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
    "method": "GET",
    "auth_type": "none",
    "output_format": "dataframe",
    "json_path": "features",
    "description": "USGS earthquake feed — last 24 hours, public, no auth",
    "group_name": "ingest",
})

# 2. Flatten — each row's `properties` dict expands into top-level columns
#    (mag, place, time, etc. become properties_mag, properties_place, ...).
flattened = JsonFlatten.model_validate({
    "asset_name": "earthquakes_flat",
    "upstream_asset_key": "earthquakes_raw",
    "column": "properties",
    "separator": "_",
    "max_depth": 2,
    "drop_original": True,
    "group_name": "transform",
})

# 3. Select + rename — keep just the human-interesting columns
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
    "group_name": "transform",
})

# 4. Sort — biggest quakes first
sorted_quakes = Sort.model_validate({
    "asset_name": "earthquakes_sorted",
    "upstream_asset_key": "earthquakes_clean",
    "by": ["magnitude"],
    "ascending": False,
    "na_position": "last",
    "reset_index": True,
    "group_name": "transform",
})

# 5. Sink — newline-delimited JSON, one record per line
write_jsonl = DataframeToJson.model_validate({
    "asset_name": "earthquakes_report",
    "upstream_asset_key": "earthquakes_sorted",
    "file_path": "/tmp/earthquakes.jsonl",
    "orient": "records",
    "lines": True,
    "date_format": "iso",
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

>>> Setup complete.

Run the pipeline:
    cd $PROJECT_DIR
    uv run dagster asset materialize --select '*' -m definitions

Output lands at /tmp/earthquakes.jsonl — one JSON line per earthquake,
sorted from largest magnitude to smallest, last 24 hours.

Inspect the output:
    head -3 /tmp/earthquakes.jsonl
    wc -l /tmp/earthquakes.jsonl

You'll see ~270 records (USGS publishes that many per day on average),
with biggest-magnitude quakes at the top. Note: results vary day-to-day
since this hits a live feed.
MSG
