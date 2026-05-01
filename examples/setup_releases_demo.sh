#!/usr/bin/env bash
# GitHub Releases demo — canonical create-dagster + dg.
#
# Hits the public GitHub API (no auth, ~60 req/hr per IP), pulls the last
# 50 dagster-io/dagster releases, keeps stable ones, sorts newest-first,
# writes a Parquet file.
#
# Pipeline (6 components, all autoloaded by `dg`):
#     rest_api_fetcher → select_columns → datetime_parser → filter → sort → dataframe_to_parquet

set -euo pipefail

PROJECT_DIR="${1:-releases-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests pyarrow
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 community components into src/$PKG/defs/"
$CLI add rest_api_fetcher       --auto-install
$CLI add select_columns         --auto-install
$CLI add datetime_parser        --auto-install
$CLI add filter                 --auto-install
$CLI add sort                   --auto-install
$CLI add dataframe_to_parquet   --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.defs.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: releases_raw
  api_url: "https://api.github.com/repos/dagster-io/dagster/releases?per_page=50"
  method: GET
  auth_type: none
  output_format: dataframe
  description: GitHub releases for dagster-io/dagster — public, no auth
  group_name: ingest
EOF

cat > "src/$PKG/defs/select_columns/defs.yaml" <<EOF
type: $PKG.defs.select_columns.component.SelectColumnsComponent
attributes:
  asset_name: releases_clean
  upstream_asset_key: releases_raw
  columns: [tag_name, name, published_at, prerelease, draft, html_url]
  reorder: true
  group_name: transform
EOF

cat > "src/$PKG/defs/datetime_parser/defs.yaml" <<EOF
type: $PKG.defs.datetime_parser.component.DatetimeParser
attributes:
  asset_name: releases_typed
  upstream_asset_key: releases_clean
  date_column: published_at
  output_column: published_dt
  group_name: transform
EOF

cat > "src/$PKG/defs/filter/defs.yaml" <<EOF
type: $PKG.defs.filter.component.FilterComponent
attributes:
  asset_name: releases_stable
  upstream_asset_key: releases_typed
  condition: "prerelease == False and draft == False"
  group_name: transform
EOF

cat > "src/$PKG/defs/sort/defs.yaml" <<EOF
type: $PKG.defs.sort.component.SortComponent
attributes:
  asset_name: releases_ordered
  upstream_asset_key: releases_stable
  by: [published_dt]
  ascending: false
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_parquet/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: releases_report
  upstream_asset_key: releases_ordered
  file_path: /tmp/dagster_releases.parquet
  compression: snappy
  index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Output: /tmp/dagster_releases.parquet — last 50 stable releases, newest
first, with parsed publish dates (parquet preserves tz-aware datetimes).
MSG
