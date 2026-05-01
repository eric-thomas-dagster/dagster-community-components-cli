#!/usr/bin/env bash
# GitHub Releases demo — REST → select → datetime → filter → sort → Parquet.
#
# Hits the public GitHub API (no auth, ~60 req/hr per IP), pulls the last
# 50 dagster-io/dagster releases, keeps stable ones (no pre-releases, no
# drafts), sorts newest-first, writes a Parquet file.
#
#   rest_api_fetcher → select_columns → datetime_parser → filter → sort → dataframe_to_parquet

set -euo pipefail

PROJECT_DIR="${1:-releases-demo}"

echo ">>> Creating project at $PROJECT_DIR"
mkdir -p "$PROJECT_DIR" && cd "$PROJECT_DIR"

uv init --python 3.11 --no-readme >/dev/null 2>&1 || true
uv add -q "dagster>=1.10.0" "dagster-webserver" "pandas" "requests" "pyarrow"

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 community components"
$CLI add rest_api_fetcher       --auto-install
$CLI add select_columns         --auto-install
$CLI add datetime_parser        --auto-install
$CLI add filter                 --auto-install
$CLI add sort                   --auto-install
$CLI add dataframe_to_parquet   --auto-install

echo ">>> Writing definitions.py"
cat > definitions.py <<'PY'
"""GitHub Releases demo — stable dagster releases sorted newest-first.

Pipeline:
    rest_api_fetcher → select_columns → datetime_parser
                      → filter → sort → dataframe_to_parquet
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


RestApiFetcher       = _load("ingestion",  "rest_api_fetcher",      "RestApiFetcherComponent")
SelectColumns        = _load("transforms", "select_columns",        "SelectColumnsComponent")
DatetimeParser       = _load("transforms", "datetime_parser",       "DatetimeParser")
Filter               = _load("transforms", "filter",                "FilterComponent")
Sort                 = _load("transforms", "sort",                  "SortComponent")
DataframeToParquet   = _load("sinks",      "dataframe_to_parquet",  "DataframeToParquetComponent")


# 1. Fetch — last 50 releases of dagster-io/dagster
ingest = RestApiFetcher.model_validate({
    "asset_name": "releases_raw",
    "api_url": "https://api.github.com/repos/dagster-io/dagster/releases?per_page=50",
    "method": "GET",
    "auth_type": "none",
    "output_format": "dataframe",
    "description": "GitHub releases for dagster-io/dagster — public, no auth",
    "group_name": "ingest",
})

# 2. Drop the noise — keep just the human-meaningful columns
selected = SelectColumns.model_validate({
    "asset_name": "releases_clean",
    "upstream_asset_key": "releases_raw",
    "columns": ["tag_name", "name", "published_at", "prerelease", "draft", "html_url"],
    "reorder": True,
    "group_name": "transform",
})

# 3. Parse the published_at ISO 8601 strings into proper datetimes
typed = DatetimeParser.model_validate({
    "asset_name": "releases_typed",
    "upstream_asset_key": "releases_clean",
    "date_column": "published_at",
    "output_column": "published_dt",
    "group_name": "transform",
})

# 4. Stable releases only — no pre-releases, no drafts
stable = Filter.model_validate({
    "asset_name": "releases_stable",
    "upstream_asset_key": "releases_typed",
    "condition": "prerelease == False and draft == False",
    "group_name": "transform",
})

# 5. Newest first
ordered = Sort.model_validate({
    "asset_name": "releases_ordered",
    "upstream_asset_key": "releases_stable",
    "by": ["published_dt"],
    "ascending": False,
    "group_name": "transform",
})

# 6. Write Parquet — preserves dtypes (incl. tz-aware datetimes)
write_parquet = DataframeToParquet.model_validate({
    "asset_name": "releases_report",
    "upstream_asset_key": "releases_ordered",
    "file_path": "/tmp/dagster_releases.parquet",
    "compression": "snappy",
    "index": False,
    "group_name": "sink",
})

defs = dg.Definitions.merge(
    ingest.build_defs(None),
    selected.build_defs(None),
    typed.build_defs(None),
    stable.build_defs(None),
    ordered.build_defs(None),
    write_parquet.build_defs(None),
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

Output: /tmp/dagster_releases.parquet — last 50 stable dagster releases,
newest first, with parsed publish dates.

Inspect:
    uv run python -c "
    import pandas as pd
    df = pd.read_parquet('/tmp/dagster_releases.parquet')
    print(f'Stable releases: {len(df)}')
    print(df[['tag_name','published_dt','html_url']].head(10).to_string())
    "
MSG
