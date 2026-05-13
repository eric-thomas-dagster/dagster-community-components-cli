#!/usr/bin/env bash
# Dremio OSS (Docker) → dremio_ingestion → summarize → parquet demo.
#
# Why interactive bits: Dremio's first-user + PAT minting are gated behind the
# UI in modern versions. The script handles Docker + Dagster scaffolding; you do
# two minutes of clicking in the Dremio UI for the auth setup.

set -euo pipefail

PROJECT_DIR="${1:-dremio-pipeline-demo}"
DREMIO_NAME=dg-dremio-demo
DREMIO_PORT=9047
DREMIO_FLIGHT_PORT=32010

echo ">>> 1/5  Starting Dremio OSS in Docker ($DREMIO_NAME on :$DREMIO_PORT)"
docker rm -f "$DREMIO_NAME" >/dev/null 2>&1 || true
docker run -d --name "$DREMIO_NAME" \
  -p $DREMIO_PORT:9047 \
  -p $DREMIO_FLIGHT_PORT:32010 \
  -p 31010:31010 \
  -v dremio_demo_data:/opt/dremio/data \
  dremio/dremio-oss:latest >/dev/null

echo "    Waiting for Dremio to become healthy..."
for i in {1..60}; do
  if curl -sf http://localhost:$DREMIO_PORT >/dev/null 2>&1; then
    echo "    Dremio is up at http://localhost:$DREMIO_PORT"
    break
  fi
  sleep 2
done

echo ""
echo "==========================================================="
echo ">>> 2/5  MANUAL STEP — Dremio first-user + PAT (≈ 2 minutes)"
echo "==========================================================="
echo ""
echo "  1. Open http://localhost:$DREMIO_PORT in your browser"
echo "  2. Create the admin user (any email + password)"
echo "  3. Once logged in: top-right user menu → Account Settings"
echo "     → Personal Access Tokens → Create"
echo "  4. Copy the token, then export it in your shell:"
echo ""
echo "       export DREMIO_PAT=<paste-token-here>"
echo ""
echo "  5. Add the Samples source: Datasets sidebar → '+' next to Sources"
echo "     → Select 'Sample Source' → Save (gives us the 'Samples' space)"
echo ""
read -p "Press ENTER once you've created the PAT, exported DREMIO_PAT, and added the Samples source..."

if [ -z "${DREMIO_PAT:-}" ]; then
  echo "ERROR: DREMIO_PAT is not set. Re-export it and re-run from step 3 onward."
  exit 1
fi

echo ""
echo ">>> 3/5  Scaffolding Dagster project ($PROJECT_DIR)"
uvx create-dagster project "$PROJECT_DIR" --uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG=$(python -c 'import os; print(os.path.basename(os.getcwd()).replace("-","_"))')

echo ">>> 4/5  Installing community components: dremio_ingestion + summarize + dataframe_to_parquet"
uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git \
  dagster-component add dremio_ingestion >/dev/null
uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git \
  dagster-component add summarize >/dev/null
uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git \
  dagster-component add dataframe_to_parquet >/dev/null

echo ">>> 5/5  Writing defs.yaml — 3 components"

mkdir -p "src/$PKG/defs/incidents_raw" \
         "src/$PKG/defs/incidents_by_category" \
         "src/$PKG/defs/incidents_parquet"

cat > "src/$PKG/defs/incidents_raw/defs.yaml" <<EOF
type: $PKG.components.dremio_ingestion.component.DremioIngestionComponent
attributes:
  asset_name: incidents_raw
  host: http://localhost:$DREMIO_PORT
  auth_type: pat
  auth_token_env_var: DREMIO_PAT
  query: |
    SELECT * FROM "Samples"."samples.dremio.com"."SF_incidents2016.json"
    LIMIT 5000
  group_name: dremio
  kinds: [dremio, sql]
EOF

cat > "src/$PKG/defs/incidents_by_category/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: incidents_by_category
  upstream_asset_key: incidents_raw
  group_by: [category]
  aggregations:
    n_incidents: {col: incident_id, agg: count}
  group_name: dremio
EOF

cat > "src/$PKG/defs/incidents_parquet/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: incidents_parquet
  upstream_asset_key: incidents_by_category
  file_path: "./output/incidents_by_category.parquet"
  compression: snappy
  group_name: dremio
EOF

echo ""
echo "==============================================="
echo "Demo project ready in ./$PROJECT_DIR"
echo ""
echo "Validate:"
echo "  cd $PROJECT_DIR"
echo "  uv run dg check defs"
echo ""
echo "Run end-to-end:"
echo "  DREMIO_PAT=\"\$DREMIO_PAT\" uv run dg launch --assets incidents_parquet"
echo ""
echo "Or launch the UI:"
echo "  DREMIO_PAT=\"\$DREMIO_PAT\" uv run dg dev   # then http://localhost:3000"
echo ""
echo "Tear down Dremio:"
echo "  docker rm -f $DREMIO_NAME"
echo "==============================================="
