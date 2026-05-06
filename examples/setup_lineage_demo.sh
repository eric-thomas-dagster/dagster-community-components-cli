#!/usr/bin/env bash
# Catalog Lineage Sync demo — locally validatable, no external infra.
#
# WHAT THIS DEMONSTRATES
#   The new lineage_graph_extractor → lineage_to_<catalog> pipeline.
#   This demo uses the file sink (no auth required) so you can validate
#   end-to-end locally. Swap `lineage_to_file` for `lineage_to_purview`
#   (or alation/collibra/datahub/openlineage/webhook) by changing one
#   YAML file.
#
# Pipeline:
#   synthetic_data_generator → orders_raw  ──┐
#   dataframe_to_csv          → orders_csv ──┤  (the asset graph being tracked)
#                                            │
#   lineage_graph_extractor → lineage_graph (canonical asset payload)
#                              │
#                              ▼
#                       lineage_to_file → /tmp/dagster_lineage.json
#
# CHANGE DETECTION
#   The lineage sink stores the upstream payload_hash in its asset
#   metadata. On re-materialization, if the upstream hash matches the
#   last-pushed hash, the catalog POST (file write here) is SKIPPED.
#
#   Requires a persistent DAGSTER_HOME — by default `dg launch` uses an
#   ephemeral home, so this demo sets one explicitly.
#
# COST: $0 (no external services).

set -euo pipefail
PROJECT_DIR="${1:-lineage-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pyarrow
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add synthetic_data_generator --auto-install
$CLI add dataframe_to_csv         --auto-install
$CLI add lineage_graph_extractor  --auto-install
$CLI add lineage_to_file          --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 50
  random_state: 42
  description: "Raw synthetic orders for the lineage demo"
  group_name: bronze
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_csv
  upstream_asset_key: orders_raw
  file_path: /tmp/orders_export.csv
  description: "Orders exported to CSV (silver layer)"
  group_name: silver
EOF

cat > "src/$PKG/defs/lineage_graph_extractor/defs.yaml" <<EOF
type: $PKG.components.lineage_graph_extractor.component.LineageGraphExtractorComponent
attributes:
  asset_name: lineage_graph
  scope: code_location
  group_name: lineage
  organization: "DemoCorp"
  platform_name: dagster
  platform_display_name: "Dagster"
EOF

cat > "src/$PKG/defs/lineage_to_file/defs.yaml" <<EOF
type: $PKG.components.lineage_to_file.component.LineageToFileComponent
attributes:
  asset_name: lineage_in_local_json
  upstream_asset_key: lineage_graph
  catalog_url: /tmp/dagster_lineage.json
  api_token_env: ""
  only_push_on_change: true
EOF

cat <<MSG

>>> Setup complete.

Materialize (with persistent DAGSTER_HOME so change detection persists):
    cd $PROJECT_DIR
    export DAGSTER_HOME=/tmp/lineage_dagster_home
    mkdir -p \$DAGSTER_HOME
    uv run dg launch --assets '*'

Inspect the JSON output:
    python3 -c "import json; d=json.load(open('/tmp/dagster_lineage.json')); print(f\"nodes={d['sync_metadata']['total_nodes']}, edges={d['sync_metadata']['total_edges']}, hash={d['sync_metadata']['payload_hash']}\")"

Verify change detection — re-materialize lineage_in_local_json:
    uv run dg launch --assets '+lineage_in_local_json' 2>&1 | grep "skipping push to file"
    # Should see: 'Lineage unchanged (hash=...), skipping push to file. Graph: 4 nodes, 2 edges.'

Swap to a real catalog:
    # Change defs.yaml type from LineageToFileComponent → LineageToPurviewComponent etc.
    # See examples/lineage_catalogs.md for catalog-specific config.
MSG
