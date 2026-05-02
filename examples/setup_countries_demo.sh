#!/usr/bin/env bash
# REST Countries demo — canonical create-dagster + dg.
#
# Hits the public REST Countries API (no auth), computes population density
# per country, rolls up by region, writes a JSON report.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     rest_api_fetcher → formula → summarize → dataframe_to_json

set -euo pipefail

PROJECT_DIR="${1:-countries-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/defs/"
$CLI add rest_api_fetcher    --auto-install
$CLI add formula             --auto-install
$CLI add summarize           --auto-install
$CLI add dataframe_to_json   --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: countries_raw
  api_url: https://restcountries.com/v3.1/all?fields=region,subregion,population,area,cca3
  method: GET
  auth_type: none
  output_format: dataframe
  description: All countries — region, population, area
  group_name: ingest
EOF

cat > "src/$PKG/defs/formula/defs.yaml" <<EOF
type: $PKG.components.formula.component.FormulaComponent
attributes:
  asset_name: countries_with_density
  upstream_asset_key: countries_raw
  expressions:
    density_per_km2: "population / area"
  group_name: transform
EOF

cat > "src/$PKG/defs/summarize/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: region_summary
  upstream_asset_key: countries_with_density
  group_by: [region]
  aggregations:
    population: sum
    density_per_km2: mean
    cca3: count
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_json/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_json.component.DataframeToJsonComponent
attributes:
  asset_name: region_summary_report
  upstream_asset_key: region_summary
  file_path: /tmp/region_summary.json
  orient: records
  indent: 2
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Output: /tmp/region_summary.json — total population and mean density
per region, with country counts. 250 countries → 6 regions.
MSG
