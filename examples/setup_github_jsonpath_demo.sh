#!/usr/bin/env bash
# JSONPath extraction demo — canonical create-dagster + dg.
#
# Searches the GitHub repo API for orchestration projects, extracts nested
# fields (owner login, license, topics) using both nested_field_extractor
# (dot paths) and json_path_extractor (JSONPath), writes a flat CSV.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     rest_api_fetcher → nested_field_extractor → json_path_extractor → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-github-jsonpath-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests jsonpath-ng
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/defs/"
$CLI add rest_api_fetcher          --auto-install
$CLI add nested_field_extractor    --auto-install
$CLI add json_path_extractor       --auto-install
$CLI add dataframe_to_csv          --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Search GitHub repos for "orchestrator" — items[] contains nested objects
cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: repos_raw
  api_url: "https://api.github.com/search/repositories?q=orchestrator&per_page=10&sort=stars"
  method: GET
  auth_type: none
  output_format: dataframe
  json_path: items
  description: Top 10 GitHub repos matching "orchestrator" by stars
  group_name: ingest
EOF

# 2. nested_field_extractor — dot paths into the `owner` and `license` dicts
cat > "src/$PKG/defs/nested_field_extractor/defs.yaml" <<EOF
type: $PKG.components.nested_field_extractor.component.NestedFieldExtractorComponent
attributes:
  asset_name: repos_with_owner
  upstream_asset_key: repos_raw
  source_column: owner
  extractions:
    owner_login: login
    owner_url: html_url
  drop_source: true
  group_name: parse
EOF

# 3. json_path_extractor — JSONPath into `license` (which can be null) plus
# `topics` which is an array
cat > "src/$PKG/defs/json_path_extractor/defs.yaml" <<EOF
type: $PKG.components.json_path_extractor.component.JsonPathExtractorComponent
attributes:
  asset_name: repos_flat
  upstream_asset_key: repos_with_owner
  source_column: license
  extractions:
    license_key: "\$.key"
    license_name: "\$.name"
  drop_source: true
  group_name: parse
EOF

# 4. Sink — pick the columns worth seeing
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: repos_report
  upstream_asset_key: repos_flat
  file_path: /tmp/github_repos.csv
  include_index: false
  columns: [name, full_name, owner_login, owner_url, license_key, license_name, stargazers_count, language]
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/github_repos.csv — top 10 "orchestrator" GitHub repos
with nested owner/license fields flattened into top-level columns.

Inspect:
    column -t -s, /tmp/github_repos.csv | head -11
MSG
