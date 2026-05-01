#!/usr/bin/env bash
# REST Countries demo — REST → formula → summarize → JSON sink.
#
# Hits the public REST Countries API (no auth), computes population
# density per country, rolls up by region, writes a JSON report.
#
#   rest_api_fetcher → formula → summarize → dataframe_to_json

set -euo pipefail

PROJECT_DIR="${1:-countries-demo}"

echo ">>> Creating project at $PROJECT_DIR"
mkdir -p "$PROJECT_DIR" && cd "$PROJECT_DIR"

uv init --python 3.11 --no-readme >/dev/null 2>&1 || true
uv add -q "dagster>=1.10.0" "dagster-webserver" "pandas" "requests"

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add rest_api_fetcher    --auto-install
$CLI add formula             --auto-install
$CLI add summarize           --auto-install
$CLI add dataframe_to_json   --auto-install

echo ">>> Writing definitions.py"
cat > definitions.py <<'PY'
"""REST Countries demo — population density rollup by region.

Pipeline:
    rest_api_fetcher → formula → summarize → dataframe_to_json
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


RestApiFetcher    = _load("ingestion",  "rest_api_fetcher",   "RestApiFetcherComponent")
Formula           = _load("transforms", "formula",            "FormulaComponent")
Summarize         = _load("transforms", "summarize",          "SummarizeComponent")
DataframeToJson   = _load("sinks",      "dataframe_to_json",  "DataframeToJsonComponent")


# 1. Fetch — restcountries.com returns a JSON array, ?fields=... keeps response flat.
ingest = RestApiFetcher.model_validate({
    "asset_name": "countries_raw",
    "api_url": "https://restcountries.com/v3.1/all?fields=region,subregion,population,area,cca3",
    "method": "GET",
    "auth_type": "none",
    "output_format": "dataframe",
    "description": "All countries — region, population, area",
    "group_name": "ingest",
})

# 2. Compute density (people per km^2). df.eval handles inf/NaN cleanly.
densified = Formula.model_validate({
    "asset_name": "countries_with_density",
    "upstream_asset_key": "countries_raw",
    "expressions": {
        "density_per_km2": "population / area",
    },
    "group_name": "transform",
})

# 3. Roll up by region: total population, mean density, country count.
rolled = Summarize.model_validate({
    "asset_name": "region_summary",
    "upstream_asset_key": "countries_with_density",
    "group_by": ["region"],
    "aggregations": {
        "population": "sum",
        "density_per_km2": "mean",
        "cca3": "count",
    },
    "group_name": "transform",
})

# 4. Write the rollup as JSON records.
write_json = DataframeToJson.model_validate({
    "asset_name": "region_summary_report",
    "upstream_asset_key": "region_summary",
    "file_path": "/tmp/region_summary.json",
    "orient": "records",
    "indent": 2,
    "group_name": "sink",
})

defs = dg.Definitions.merge(
    ingest.build_defs(None),
    densified.build_defs(None),
    rolled.build_defs(None),
    write_json.build_defs(None),
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

Output: /tmp/region_summary.json — total population and mean density
per region, with country counts.

Inspect:
    cat /tmp/region_summary.json
MSG
