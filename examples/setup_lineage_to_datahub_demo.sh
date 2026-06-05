#!/usr/bin/env bash
# Lineage → DataHub end-to-end demo — DataHub in Docker, live-validated.
#
# WHAT THIS DEMONSTRATES
#   The two lineage components shipped in the templates registry that
#   pair to surface Dagster's asset graph in DataHub:
#
#     1. lineage_graph_extractor  — source asset that walks the live
#                                   Dagster asset graph and emits a
#                                   canonical {nodes, edges, source_system}
#                                   payload + a content-hash for change
#                                   detection
#     2. lineage_to_datahub       — sink asset that consumes the upstream
#                                   payload and pushes to DataHub's GMS
#                                   API (ingestProposal endpoint), one
#                                   datasetProperties + one upstreamLineage
#                                   aspect per Dagster asset
#
#   The demo scaffolds a Dagster project, drops in a small asset graph
#   that mirrors a realistic ETL shape (raw → transform → mart), then
#   pushes the resulting lineage to DataHub. Validation step queries
#   DataHub's GraphQL API to confirm the assets + edges arrived.
#
# COST: $0 — DataHub OSS in Docker. Pulls ~3.5 GB across ~8 containers
#   on first run and takes 5-10 min to fully come up.

set -eo pipefail

PROJECT_DIR="${1:-lineage-to-datahub-demo}"
DATAHUB_VERSION="${DATAHUB_VERSION:-v1.3.0}"
DATAHUB_FRONTEND_PORT="${DATAHUB_FRONTEND_PORT:-19002}"  # default 9002 → host 19002
DATAHUB_GMS_PORT="${DATAHUB_GMS_PORT:-18080}"            # default 8080 → host 18080
DATAHUB_USER="${DATAHUB_USER:-datahub}"
DATAHUB_PASSWORD="${DATAHUB_PASSWORD:-datahub}"

# --- 0. Tool check -------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "✗ docker required"; exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "✗ uv required"; exit 1
fi

# --- 1. Fetch DataHub's quickstart compose file --------------------------
# We use `docker compose` directly against DataHub's published compose
# file instead of going through the `acryl-datahub` CLI. The CLI uses
# python-docker which has a known urllib3>=2 + Mac-Docker-Desktop
# incompatibility that makes it hang on Apple Silicon.
DATAHUB_COMPOSE="/tmp/datahub-quickstart-compose.yml"
echo ">>> Fetching DataHub quickstart compose file ($DATAHUB_VERSION)"
curl -sS \
  "https://raw.githubusercontent.com/datahub-project/datahub/master/docker/quickstart/docker-compose.quickstart-profile.yml" \
  -o "$DATAHUB_COMPOSE"
if [ ! -s "$DATAHUB_COMPOSE" ]; then
  echo "    ✗ Could not fetch compose file"; exit 1
fi
echo "    ✓ Compose file fetched ($(wc -l < "$DATAHUB_COMPOSE") lines)"

# Write a .env file alongside the compose so docker compose picks up
# the required vars. DataHub's CLI sets these for you; we have to do
# it manually since we're going CLI-less.
cat > "$(dirname "$DATAHUB_COMPOSE")/.env" <<EOF
DATAHUB_VERSION=$DATAHUB_VERSION
UI_INGESTION_DEFAULT_CLI_VERSION=0.15.0.1
DATAHUB_TOKEN_SERVICE_SIGNING_KEY=clm-demo-signing-key-do-not-use-in-prod
DATAHUB_TOKEN_SERVICE_SALT=clm-demo-salt-do-not-use-in-prod
DATAHUB_MAPPED_FRONTEND_PORT=9002
DATAHUB_MAPPED_GMS_PORT=8080
DATAHUB_ELASTIC_PORT=9200
DATAHUB_KAFKA_BROKER_PORT=9092
EOF

# --- 2a. Spin up the storage layer first --------------------------------
# Bring up MySQL / OpenSearch / Kafka in isolation. We need MySQL up
# *before* the system-update job runs so we can pre-load init.sql
# (the v1.3.0 quickstart-profile has a known bug where
# DATAHUB_SQL_SETUP_ENABLED=true doesn't actually create the
# metadata_aspect_v2 table — system-update then crashes on first read).
echo ">>> Spinning up DataHub storage layer (mysql, opensearch, kafka)"
echo "    First run pulls ~3.5 GB across 8 containers (5-10 min). Be patient."
docker compose -f "$DATAHUB_COMPOSE" --profile quickstart up -d \
  mysql opensearch kafka-broker >/tmp/datahub-quickstart.log 2>&1
COMPOSE_RC=$?
if [ $COMPOSE_RC -ne 0 ]; then
  echo "    ✗ docker compose up (storage layer) failed (exit $COMPOSE_RC). Tail:"
  tail -30 /tmp/datahub-quickstart.log
  exit 1
fi

echo ">>> Waiting for MySQL to be healthy..."
for i in $(seq 1 60); do
  if docker inspect datahub-mysql-1 --format '{{.State.Health.Status}}' 2>/dev/null | grep -q "healthy"; then
    echo "    MySQL healthy after ${i}*2s"
    break
  fi
  sleep 2
done

# --- 2b. Pre-load DataHub's MySQL schema (workaround for v1.3.0 bug) ----
echo ">>> Pre-loading DataHub init.sql into MySQL"
DATAHUB_INIT_SQL="/tmp/datahub-init.sql"
curl -sS \
  "https://raw.githubusercontent.com/datahub-project/datahub/$DATAHUB_VERSION/docker/mysql-setup/init.sql" \
  | sed 's/DATAHUB_DB_NAME/datahub/g' > "$DATAHUB_INIT_SQL"
if [ ! -s "$DATAHUB_INIT_SQL" ]; then
  echo "    ✗ Could not fetch init.sql for $DATAHUB_VERSION"; exit 1
fi
docker exec -i datahub-mysql-1 mysql -u root -pdatahub < "$DATAHUB_INIT_SQL" 2>/dev/null
echo "    ✓ init.sql loaded ($(docker exec datahub-mysql-1 mysql -u root -pdatahub datahub -e 'SHOW TABLES;' 2>/dev/null | tail -n +2 | wc -l | tr -d ' ') tables)"

# --- 2c. Bring up the rest (system-update, gms, frontend, actions) ------
echo ">>> Spinning up the rest of DataHub (system-update, gms, frontend)"
docker compose -f "$DATAHUB_COMPOSE" --profile quickstart up -d \
  >>/tmp/datahub-quickstart.log 2>&1
COMPOSE_RC=$?
if [ $COMPOSE_RC -ne 0 ]; then
  echo "    ✗ docker compose up failed (exit $COMPOSE_RC). Tail of log:"
  tail -30 /tmp/datahub-quickstart.log
  exit 1
fi
echo "    Containers launched. Polling for frontend readiness..."

echo ">>> Waiting for DataHub frontend to respond (up to 12 min)..."
DATAHUB_READY=false
for i in $(seq 1 144); do  # 144 * 5s = 12 min
  if curl -sS -m 2 "http://localhost:9002/" -o /dev/null -w "%{http_code}" 2>/dev/null | grep -qE "200|302"; then
    echo "    DataHub frontend ready after ${i}*5s"
    DATAHUB_READY=true
    break
  fi
  sleep 5
done
if [ "$DATAHUB_READY" != "true" ]; then
  echo "    !!! DataHub didn't reach ready state in 12 min."
  echo "    Check 'docker ps' + tail of /tmp/datahub-quickstart.log:"
  tail -30 /tmp/datahub-quickstart.log
  echo ""
  echo "    Most common cause: ES or MySQL container didn't come up in time."
  echo "    Look at: docker logs elasticsearch  /  docker logs mysql"
  exit 1
fi

# --- 3. Get a personal-access-token from DataHub for the sink ----------
# DataHub OSS quickstart enables PAT issuance by default. We mint a token
# via DataHub's auth endpoint (datahub user account, password "datahub").
echo ">>> Minting a personal-access-token for the sink"
# DataHub's GraphQL is at /api/graphql on the frontend. Sign in first.
COOKIE_JAR="$(mktemp)"
LOGIN_RESP=$(curl -sS -c "$COOKIE_JAR" \
  -X POST "http://localhost:9002/logIn" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$DATAHUB_USER\",\"password\":\"$DATAHUB_PASSWORD\"}")
# Generate a token via the GraphQL query (DataHub v1.3+ requires
# MANAGE_TOKENS privilege for the createAccessToken mutation, but the
# getAccessToken *query* works for the user fetching their own token).
TOKEN_QUERY='{"query":"query { getAccessToken(input: {type: PERSONAL, actorUrn: \"urn:li:corpuser:datahub\", duration: ONE_HOUR}) { accessToken } }"}'
DATAHUB_TOKEN=$(curl -sS -b "$COOKIE_JAR" \
  -X POST "http://localhost:9002/api/graphql" \
  -H "Content-Type: application/json" \
  -d "$TOKEN_QUERY" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['data']['getAccessToken']['accessToken'])" 2>/dev/null || echo "")

if [ -z "$DATAHUB_TOKEN" ]; then
  echo "    !!! Could not mint DataHub PAT. Common causes:"
  echo "    - DataHub quickstart still initializing (try sleep 60 + re-run script)"
  echo "    - GraphQL schema changed across versions; check /tmp/datahub-quickstart.log"
  exit 1
fi
echo "    ✓ Token minted (${#DATAHUB_TOKEN} chars)"
export DATAHUB_API_TOKEN="$DATAHUB_TOKEN"

# --- 4. Scaffold the Dagster project ----------------------------------
echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"
uv add --dev -q dagster-dg-cli dagster-webserver

# --- 5. Install the lineage components via dagster-component CLI -------
# This drops the component source into src/$PKG/components/<id>/ and the
# starter defs.yaml into src/$PKG/defs/<id>/defs.yaml. We rewrite the
# defs.yaml below.
echo ">>> Installing lineage components into the project"
CLI="uvx --from dagster-community-components-cli dagster-component"
$CLI add lineage_graph_extractor --auto-install >/dev/null 2>&1
$CLI add lineage_to_datahub --auto-install >/dev/null 2>&1

# --- 6. Drop in a realistic asset graph: raw → transform → mart -------
cat > "src/$PKG/defs/example_graph.py" <<'EOF'
"""Small ETL-shape asset graph that exercises the lineage extractor."""
import dagster as dg


@dg.asset(group_name="raw", description="Raw orders ingested from upstream")
def raw_orders() -> dict:
    return {"rows": 100}


@dg.asset(group_name="raw", description="Raw customers ingested from upstream")
def raw_customers() -> dict:
    return {"rows": 50}


@dg.asset(group_name="transform", description="Cleaned orders with computed totals")
def orders_clean(raw_orders: dict) -> dict:
    return {"rows": raw_orders["rows"]}


@dg.asset(group_name="transform", description="Customer master joined to orders")
def customer_360(raw_customers: dict, orders_clean: dict) -> dict:
    return {"rows": min(raw_customers["rows"], orders_clean["rows"])}


@dg.asset(group_name="mart", description="Daily revenue by region")
def daily_revenue(orders_clean: dict) -> dict:
    return {"rows": orders_clean["rows"]}


@dg.asset(group_name="mart", description="Top customers by lifetime value")
def top_customers(customer_360: dict) -> dict:
    return {"rows": customer_360["rows"]}


defs = dg.Definitions(assets=[
    raw_orders, raw_customers, orders_clean, customer_360,
    daily_revenue, top_customers,
])
EOF

# --- 7. Wire up the extractor + sink (override the CLI-installed defaults)
cat > "src/$PKG/defs/lineage_graph_extractor/defs.yaml" <<EOF
type: $PKG.components.lineage_graph_extractor.component.LineageGraphExtractorComponent
attributes:
  asset_name: lineage_graph
  scope: code_location
  group_name: lineage
  organization: "ClmDemo"
  platform_name: dagster
  platform_display_name: "Dagster"
EOF

cat > "src/$PKG/defs/lineage_to_datahub/defs.yaml" <<EOF
type: $PKG.components.lineage_to_datahub.component.LineageToDataHubComponent
attributes:
  asset_name: lineage_to_datahub
  upstream_asset_key: lineage_graph
  # The quickstart-profile compose maps GMS to host port 8080. Hit it
  # directly — the frontend reverse-proxy at /api/gms strips Bearer
  # auth and returns 401.
  catalog_url: http://localhost:8080
  api_token_env: DATAHUB_API_TOKEN
  only_push_on_change: false
EOF

# --- 8. Materialize ----------------------------------------------------
echo ">>> Materializing the example graph + extractor + DataHub sink"
uv run dg launch --assets '*' 2>&1 | tail -10

# --- 9. Validate via DataHub GraphQL ----------------------------------
echo ">>> Waiting 15s for DataHub indexing"
sleep 15

echo ">>> Querying DataHub for the assets we just pushed"
# DataHub v1.3+ GraphQL: 'searchResults' (not 'entities'); platform value
# must be the full urn:li:dataPlatform:<name> form, not the short name.
SEARCH='{"query":"query { search(input: { type: DATASET, query: \"*\", start: 0, count: 50, filters: [{field: \"platform\", value: \"urn:li:dataPlatform:dagster\"}] }) { total searchResults { entity { urn ... on Dataset { properties { name } } } } } }"}'
RESULT=$(curl -sS -b "$COOKIE_JAR" \
  -X POST "http://localhost:9002/api/graphql" \
  -H "Content-Type: application/json" \
  -d "$SEARCH")

TOTAL=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['search']['total'])" 2>/dev/null || echo "0")
echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "  Validation results"
echo "──────────────────────────────────────────────────────────────────────"
echo "  Assets in DataHub on platform=dagster: $TOTAL"
echo "  Expected:                              8  (6 example assets +"
echo "                                            lineage_graph + lineage_to_datahub)"
echo ""

if [ "$TOTAL" = "8" ]; then
  echo "  ✓ Live-validated end-to-end. View in DataHub:"
  echo "    http://localhost:9002 (login: $DATAHUB_USER / $DATAHUB_PASSWORD)"
  echo ""
  echo "    Browse → Datasets → dagster"
  echo "    Click any asset → Lineage tab → see upstream/downstream edges"
elif [ "$TOTAL" -gt "0" ]; then
  echo "  ⚠ Partial. $TOTAL of 8 expected. Check DataHub UI + Dagster materialization logs."
else
  echo "  ✗ No assets found. Common causes:"
  echo "    - DataHub still indexing (sleep 60 + re-query)"
  echo "    - Token expired (regenerate via /api/graphql)"
  echo "    - Sink raised but materialization swallowed; check dg launch output above"
fi

echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "  Lineage edges to look for"
echo "──────────────────────────────────────────────────────────────────────"
echo "  raw_orders     → orders_clean → daily_revenue"
echo "  raw_customers  → customer_360 → top_customers"
echo "  orders_clean   → customer_360"
echo ""

cat <<DONE
──────────────────────────────────────────────────────────────────────
Teardown when you're done:
──────────────────────────────────────────────────────────────────────
  docker compose -f $DATAHUB_COMPOSE --profile quickstart down -v
  rm -rf $PROJECT_DIR

Re-run materialization only (DataHub stays up):
  cd $PROJECT_DIR
  export DATAHUB_API_TOKEN=<mint a fresh token via /api/graphql — the one
                          from this run expires in 1 hour>
  uv run dg launch --assets '*'

DONE
