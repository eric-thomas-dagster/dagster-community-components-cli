#!/usr/bin/env bash
# Compute Log Managers end-to-end demo — Splunk HEC + OTel Collector,
# both validated live in Docker.
#
# WHAT THIS DEMONSTRATES
#   The three compute log managers shipped in v0.10.17:
#
#     1. SplunkComputeLogManager — direct Dagster → Splunk HEC
#     2. OtlpComputeLogManager  — Dagster → OTel Collector → Splunk HEC
#     3. TeeComputeLogManager   — composes both so a single Dagster
#                                  asset materialization writes its
#                                  stdout via BOTH paths in parallel
#
#   After materialization, the script queries Splunk's search API and
#   confirms log events arrived via each path (distinguishable by the
#   `dagster.io_type` and the `source` field that the OTel Collector's
#   splunkhec exporter sets).
#
# COST: $0 — Splunk Free + OTel Collector contrib, both containerized.
#   Splunk pulls ~1.5GB the first time and takes 2-3 min to start; budget
#   accordingly.

set -euo pipefail

PROJECT_DIR="${1:-compute-log-managers-demo}"

# Splunk container ports (avoid common host conflicts).
SPLUNK_CONTAINER="${SPLUNK_CONTAINER:-clm-demo-splunk}"
SPLUNK_WEB_PORT="${SPLUNK_WEB_PORT:-18000}"
SPLUNK_HEC_PORT="${SPLUNK_HEC_PORT:-18088}"
SPLUNK_MGMT_PORT="${SPLUNK_MGMT_PORT:-18089}"
SPLUNK_PASSWORD="${SPLUNK_PASSWORD:-clm-demo-password-1234}"
# HEC token — Splunk's image accepts this env var and provisions the
# default global HEC token to its value.
SPLUNK_HEC_TOKEN="${SPLUNK_HEC_TOKEN:-clm-demo-hec-token-do-not-use-in-prod}"
SPLUNK_INDEX="${SPLUNK_INDEX:-dagster}"

# OTel Collector container.
OTEL_CONTAINER="${OTEL_CONTAINER:-clm-demo-otel-collector}"
OTEL_HTTP_PORT="${OTEL_HTTP_PORT:-14318}"

# Custom Docker network so the two containers can resolve each other
# by name. The OTel Collector forwards to http://splunk:8088 internally
# while the host reaches Splunk via $SPLUNK_HEC_PORT.
NETWORK="${NETWORK:-clm-demo-net}"

# --- 0. Reset state --------------------------------------------------------
docker rm -f "$SPLUNK_CONTAINER" "$OTEL_CONTAINER" >/dev/null 2>&1 || true
docker network rm "$NETWORK" >/dev/null 2>&1 || true
docker network create "$NETWORK" >/dev/null

# --- 1. Bring up Splunk ----------------------------------------------------
echo ">>> Starting Splunk Free in Docker (container: $SPLUNK_CONTAINER)"
echo "    Note: first run pulls ~1.5 GB; subsequent runs use the cached image."
echo "    On Apple Silicon, Splunk runs via Rosetta (linux/amd64) — startup"
echo "    is ~30s slower than on native amd64 hosts."
docker run -d --name "$SPLUNK_CONTAINER" \
  --platform linux/amd64 \
  --network "$NETWORK" --network-alias splunk \
  -p "$SPLUNK_WEB_PORT:8000" \
  -p "$SPLUNK_HEC_PORT:8088" \
  -p "$SPLUNK_MGMT_PORT:8089" \
  -e SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com \
  -e SPLUNK_START_ARGS=--accept-license \
  -e SPLUNK_PASSWORD="$SPLUNK_PASSWORD" \
  -e SPLUNK_HEC_TOKEN="$SPLUNK_HEC_TOKEN" \
  splunk/splunk:latest >/dev/null

echo ">>> Waiting for Splunk to come up (typically 2-3 min, allowing up to 5)..."
SPLUNK_READY=false
for i in $(seq 1 60); do
  # HEC health check — returns {"text":"HEC is healthy","code":17} when ready.
  if curl -sSk "https://localhost:$SPLUNK_HEC_PORT/services/collector/health" 2>/dev/null \
       | grep -q '"text"'; then
    echo "    Splunk ready after ${i}*5s"
    SPLUNK_READY=true
    break
  fi
  sleep 5
done
if [ "$SPLUNK_READY" != "true" ]; then
  echo "    !!! Splunk did not become ready in time. Container logs:"
  docker logs --tail 50 "$SPLUNK_CONTAINER"
  exit 1
fi

# --- 1b. Create the demo index ---------------------------------------------
# The default HEC token allows writes to any index, but the index has to
# exist. Create it now.
echo ">>> Creating Splunk index '$SPLUNK_INDEX' (idempotent)"
curl -sSk -u "admin:$SPLUNK_PASSWORD" \
  -X POST "https://localhost:$SPLUNK_MGMT_PORT/services/data/indexes" \
  -d "name=$SPLUNK_INDEX" >/dev/null 2>&1 || true

# --- 2. Bring up the OTel Collector ----------------------------------------
echo ">>> Starting OTel Collector (container: $OTEL_CONTAINER) → splunk:8088"
OTEL_CONFIG="$(mktemp -d)/otel-config.yaml"
cat > "$OTEL_CONFIG" <<EOF
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

exporters:
  splunk_hec:
    token: "$SPLUNK_HEC_TOKEN"
    endpoint: "https://splunk:8088/services/collector"
    source: "otel-collector"          # distinguishes from direct-HEC events
    sourcetype: "dagster:compute_log"
    index: "$SPLUNK_INDEX"
    tls:
      insecure_skip_verify: true     # self-signed Splunk cert

service:
  pipelines:
    logs:
      receivers: [otlp]
      exporters: [splunk_hec]
EOF

docker run -d --name "$OTEL_CONTAINER" \
  --network "$NETWORK" \
  -p "$OTEL_HTTP_PORT:4318" \
  -v "$OTEL_CONFIG:/etc/otelcol-contrib/config.yaml:ro" \
  otel/opentelemetry-collector-contrib:latest >/dev/null

echo ">>> Waiting for OTel Collector to come up..."
for i in $(seq 1 30); do
  if docker exec "$OTEL_CONTAINER" wget -qO- "http://localhost:13133/" >/dev/null 2>&1 \
     || curl -sS "http://localhost:$OTEL_HTTP_PORT/v1/logs" -X POST -d '{}' \
          -H "Content-Type: application/json" 2>/dev/null \
          | grep -qE "200|400"; then
    echo "    OTel Collector ready after ${i}s"
    break
  fi
  sleep 1
done

# --- 3. Scaffold the Dagster project --------------------------------------
echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

# requests for the Splunk HEC + OTLP POST paths in our CLMs;
# urllib3 is already a transitive but we pin to avoid resolver thrash.
uv add -q 'yarl<1.24' 'requests>=2.28' \
  "dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/main.zip"
uv add --dev -q dagster-dg-cli dagster-webserver

# --- 4. Write dagster.yaml with TeeComputeLogManager ----------------------
# Tee wraps Splunk (direct HEC) + OTLP (via Collector → Splunk).
# Same op materialization writes its stdout via BOTH paths in parallel.
echo ">>> Writing dagster.yaml with Tee(Splunk + OTLP) compute log manager"
mkdir -p .dagster_home
cat > .dagster_home/dagster.yaml <<EOF
compute_logs:
  module: dagster_community_components.compute_log_managers.tee
  class: TeeComputeLogManager
  config:
    local_dir: out/clm-demo-local
    display_manager_index: 0
    fail_on_partial_upload: false
    managers:
      # Path 1: Dagster → Splunk HEC (direct, source=dagster)
      - module: dagster_community_components.compute_log_managers.splunk
        class: SplunkComputeLogManager
        config:
          hec_url: https://localhost:$SPLUNK_HEC_PORT/services/collector
          hec_token: "$SPLUNK_HEC_TOKEN"
          splunk_web_url: http://localhost:$SPLUNK_WEB_PORT
          index: $SPLUNK_INDEX
          sourcetype: dagster:compute_log
          source: dagster                  # source=dagster distinguishes from OTel
          verify_ssl: false                # self-signed cert
          batch_size: 100
          local_dir: out/clm-demo-local
      # Path 2: Dagster → OTel Collector → Splunk HEC (OTel Collector
      # tags events with source=otel-collector via its splunk_hec exporter)
      - module: dagster_community_components.compute_log_managers.otlp
        class: OtlpComputeLogManager
        config:
          otlp_endpoint: http://localhost:$OTEL_HTTP_PORT
          service_name: dagster
          location_label: clm-demo
          batch_size: 100
          verify_ssl: false
          local_dir: out/clm-demo-local
EOF

# --- 5. Write a tiny asset that prints to stdout --------------------------
# Single asset, single print line — keeps validation deterministic. We
# include a unique run marker so we can search for THIS run's events
# without picking up stale events from prior demo runs.
RUN_MARKER="clm-demo-$(date +%s)"
echo ">>> Asset run marker: $RUN_MARKER"

# The autoloader walks src/$PKG/defs/ for any .py file with a `defs`
# attribute or top-level @asset/@sensor/etc. — drop a chatty_asset there.
cat > "src/$PKG/defs/chatty.py" <<EOF
import dagster as dg

@dg.asset
def chatty_asset(context: dg.AssetExecutionContext):
    """Emits stdout that should land in Splunk via two paths."""
    for i in range(10):
        print(f"$RUN_MARKER hello line {i}")
    context.log.info("$RUN_MARKER context log entry")
    return None

defs = dg.Definitions(assets=[chatty_asset])
EOF
# Remove the scaffold's empty defs.py (no longer needed).
rm -f "src/$PKG/defs.py"

# --- 6. Materialize -------------------------------------------------------
echo ">>> Materializing chatty_asset (logs should flow to both Splunk paths)"
export DAGSTER_HOME="$(pwd)/.dagster_home"
uv run dg launch --assets chatty_asset 2>&1 | tail -10

# --- 7. Validate: query Splunk for the run marker ------------------------
echo ">>> Waiting ~15s for Splunk to ingest + index events"
sleep 15

echo ""
echo ">>> Validating: querying Splunk for both compute-log paths"

# Splunk search API oneshot: returns search results synchronously.
SEARCH_DIRECT="search index=$SPLUNK_INDEX source=dagster \"$RUN_MARKER\" | stats count"
SEARCH_OTEL="search index=$SPLUNK_INDEX source=otel-collector \"$RUN_MARKER\" | stats count"

DIRECT_COUNT=$(curl -sSk -u "admin:$SPLUNK_PASSWORD" \
  "https://localhost:$SPLUNK_MGMT_PORT/services/search/jobs/oneshot" \
  -d "search=$SEARCH_DIRECT" \
  -d "output_mode=json" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['results'][0].get('count','0') if d.get('results') else '0')" \
  2>/dev/null || echo "0")

OTEL_COUNT=$(curl -sSk -u "admin:$SPLUNK_PASSWORD" \
  "https://localhost:$SPLUNK_MGMT_PORT/services/search/jobs/oneshot" \
  -d "search=$SEARCH_OTEL" \
  -d "output_mode=json" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['results'][0].get('count','0') if d.get('results') else '0')" \
  2>/dev/null || echo "0")

echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "  Validation results"
echo "──────────────────────────────────────────────────────────────────────"
echo "  Direct HEC (source=dagster)       : $DIRECT_COUNT events"
echo "  OTel Collector (source=otel-collector): $OTEL_COUNT events"
echo ""

if [ "$DIRECT_COUNT" -gt 0 ] && [ "$OTEL_COUNT" -gt 0 ]; then
  echo "  ✓ BOTH paths working — Tee fan-out validated end-to-end."
elif [ "$DIRECT_COUNT" -gt 0 ]; then
  echo "  ⚠ Direct HEC working, OTLP path returned 0 events."
  echo "    Inspect: docker logs $OTEL_CONTAINER"
elif [ "$OTEL_COUNT" -gt 0 ]; then
  echo "  ⚠ OTLP path working, direct HEC returned 0 events."
  echo "    Inspect: curl -k -u admin:$SPLUNK_PASSWORD https://localhost:$SPLUNK_HEC_PORT/services/collector/health"
else
  echo "  ✗ Neither path landed events. Common causes:"
  echo "    - Splunk still indexing — re-run validation: sleep 30 && bash -x <this block>"
  echo "    - HEC token not provisioned. Check: curl -k -u admin:$SPLUNK_PASSWORD"
  echo "      https://localhost:$SPLUNK_MGMT_PORT/servicesNS/nobody/splunk_httpinput/data/inputs/http"
  echo "    - Index missing: curl -k -u admin:$SPLUNK_PASSWORD"
  echo "      https://localhost:$SPLUNK_MGMT_PORT/services/data/indexes | grep $SPLUNK_INDEX"
fi

echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "  Where to look next"
echo "──────────────────────────────────────────────────────────────────────"
echo ""
echo "  Splunk Web (admin / $SPLUNK_PASSWORD):"
echo "    http://localhost:$SPLUNK_WEB_PORT"
echo ""
echo "  Search bar:"
echo "    index=$SPLUNK_INDEX \"$RUN_MARKER\""
echo ""
echo "  Compare paths side-by-side:"
echo "    index=$SPLUNK_INDEX \"$RUN_MARKER\" | stats count by source"
echo ""
echo "  Inspect structured fields the CLMs ship:"
echo "    index=$SPLUNK_INDEX \"$RUN_MARKER\" | table dagster_run_id, dagster_step_key, dagster_io_type, source, _raw"
echo ""

# --- 8. Tear-down instructions -------------------------------------------
cat <<DONE

──────────────────────────────────────────────────────────────────────
Teardown when you're done:
──────────────────────────────────────────────────────────────────────
  docker rm -f $SPLUNK_CONTAINER $OTEL_CONTAINER
  docker network rm $NETWORK
  rm -rf $PROJECT_ABS/out/clm-demo-local

To re-run the demo only (Splunk + OTel stay up):
  cd $PROJECT_DIR
  export DAGSTER_HOME="\$(pwd)/.dagster_home"
  uv run dg launch --assets chatty_asset

DONE
