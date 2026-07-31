#!/usr/bin/env bash
# agentic_batch_triage_router — same batch fan-out shape as the plain and
# generic-component versions, but using llm_multi_path_router with
# fanout_mode: true. The SAME component the SE saw in agentic_router.md,
# just flipped into batch mode. Same YAML surface, different runtime shape.

set -eo pipefail

PROJECT_DIR="${1:-agentic-batch-triage-router-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — router will error at materialize time."
fi

rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi

uv add -q "$DCC_SRC" pandas openai duckdb dagster-duckdb

export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME" data audit

# --- Seed baggage DuckDB + today's reports --------------------------------
uv run python - <<'PY'
import duckdb
con = duckdb.connect("data/baggage.duckdb")
con.execute("""
    CREATE TABLE IF NOT EXISTS baggage_tracking (
        baggage_id VARCHAR PRIMARY KEY, last_scanned_at VARCHAR,
        hours_since_scan INT, status VARCHAR, eta_hours VARCHAR,
        delivery_address VARCHAR);
""")
con.execute("DELETE FROM baggage_tracking;")
con.executemany("INSERT INTO baggage_tracking VALUES (?,?,?,?,?,?)", [
    ("BAG-001", "ORD",  6, "in_transit_to_LHR",  "4",  "12 Baker St, London W1U 3BE"),
    ("BAG-002", "CDG", 72, "not_found",          "NA", "NA"),
    ("BAG-003", "JFK",  2, "awaiting_delivery",  "NA", "123 Main St, Brooklyn NY 11201"),
    ("BAG-004", "SFO",  1, "in_transit_to_ORD",  "3",  "555 Michigan Ave, Chicago IL 60611"),
    ("BAG-005", "LHR", 96, "not_found",          "NA", "NA"),
])
con.close()
print("seeded 5 baggage records")
PY

cat > data/daily_baggage_reports.csv <<'CSV'
case_id,passenger,flight,baggage_id,description
d1,Alice,UA100 ORD→LHR,BAG-001,Checked bag never arrived at LHR.
d2,Bob,AF200 JFK→CDG,BAG-002,Bag missing 3 days — no updates.
d3,Carol,DL300 SFO→JFK,BAG-003,Airline confirms bag at JFK; need delivery.
d4,Dan,AA150 SFO→ORD,BAG-004,Bag not on carousel at ORD.
d5,Eve,BA200 JFK→LHR,BAG-005,Bag missing 4 days — filed police report.
CSV

# --- defs.yaml files ------------------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# DuckDB resource (baggage_db)
mkdir -p "$DEFS/baggage_db"
cat > "$DEFS/baggage_db/defs.yaml" <<YAML
type: dagster_community_components.DuckDBResourceComponent
attributes:
  resource_key: baggage_db
  database: ${PROJECT_ABS}/data/baggage.duckdb
YAML

# Source
mkdir -p "$DEFS/daily_baggage_reports"
cat > "$DEFS/daily_baggage_reports/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: daily_baggage_reports
  file_path: ${PROJECT_ABS}/data/daily_baggage_reports.csv
  group_name: source
YAML

# THE ROUTER — same component as agentic_router.md, but with fanout_mode: true
mkdir -p "$DEFS/daily_triage_batch"
cat > "$DEFS/daily_triage_batch/defs.yaml" <<'YAML'
type: dagster_community_components.LlmMultiPathRouterComponent
attributes:
  asset_name: daily_triage_batch
  upstream_asset_key: daily_baggage_reports
  # THE FLIP: batch fan-out instead of per-case partitions.
  fanout_mode: true
  # Still uses this column to identify each case's row within the batch.
  partition_static_column: case_id

  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  max_iterations: 5
  temperature: 0.0

  group_name: router

  task_template: |
    You are a baggage-loss triage agent. Resolve case {case_id} for {passenger}
    on flight {flight}, baggage_id {baggage_id}. Description: {description}

    SOP: query the DB; if located + address → organize_delivery + inform_passenger → done.
    If not_found → inquire_with_airport; then send_care_voucher + request_info → done.
    Branches: delivery_request, voucher_issued, escalation.

  tools:
    - name: query_baggage_db
      description: "Query the baggage tracking database. Args: baggage_id."
      tool_type: sql
      resource: baggage_db
      sql_template: |
        SELECT baggage_id, status, last_scanned_at, hours_since_scan, eta_hours, delivery_address
        FROM baggage_tracking
        WHERE baggage_id = '{args}'

    - name: inquire_with_airport
      description: "Send a lookup to an airport. Args: airport_code."
      system_message: |
        You are the airport lookup response. Respond: "airport=<code>; response=<one-line>"

    - name: send_care_voucher
      description: "Issue a voucher. Args: passenger_id, amount_usd."
      system_message: "Respond: 'voucher_id=V<6-digits>; passenger_id=<id>; amount_usd=<n>; status=issued'."

    - name: organize_delivery
      description: "Book a courier. Args: baggage_id, address."
      system_message: "Respond: 'delivery_id=D<6-digits>; baggage_id=<id>; address=<addr>; eta_hours=8'."

    - name: inform_passenger
      description: "Notify the passenger. Args: passenger_id, message."
      system_message: "Respond: 'notification_id=N<6-digits>; passenger_id=<id>; status=sent'."

    - name: request_info_from_passenger
      description: "Ask the passenger a question. Args: question."
      system_message: "Roleplay a terse reply. Respond: 'passenger_reply=<one sentence>'."

  outputs:
    - name: delivery_request
      description: "Bag located and delivery organized."
      output_schema:
        baggage_id: "BAG-* id from query_baggage_db"
        passenger: "Passenger name"
        address: "Delivery address from DB"
        delivery_id: "D<digits> from organize_delivery"
        eta_hours: "scheduled_eta_hours (integer)"

    - name: voucher_issued
      description: "A care voucher was sent (bag not immediately deliverable)."
      output_schema:
        passenger: "Passenger name"
        amount_usd: "Voucher amount (integer)"
        voucher_id: "V<digits> from send_care_voucher"
        reason: "One-line reason"

    - name: escalation
      description: "The case needs human review."
      output_schema:
        case_summary: "One-sentence summary"
        needs_action: "What the reviewer should do next"
        urgency: "low | medium | high"
YAML

# --- dg check + materialize -----------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

if [ -z "$OPENAI_API_KEY" ]; then
  echo ""
  echo "! OPENAI_API_KEY not set — skipping materialization."
  echo "  Cleanup: rm -rf $PROJECT_ABS"
  exit 0
fi

echo ""
echo ">>> daily_baggage_reports"
uv run dg launch --assets daily_baggage_reports 2>&1 | tail -2

echo ""
echo ">>> daily_triage_batch — ONE run, N cases fan out via llm_multi_path_router fanout_mode"
uv run dg launch --assets daily_triage_batch 2>&1 | tail -3

echo ""
echo ">>> Branch assets — unpartitioned DataFrames per branch"
for BRANCH in delivery_request voucher_issued escalation; do
  echo "    ─── $BRANCH ───"
  uv run dg launch --assets "$BRANCH" 2>&1 | tail -2
done

cat <<DONE

✓ agentic_batch_triage_router demo done.

Same LlmMultiPathRouterComponent as agentic_router.md — just with
fanout_mode: true. Same YAML surface, different runtime shape:

  daily_baggage_reports (source)
       └─► daily_triage_batch  ← ONE unpartitioned @graph_asset
              │                    fan_out_cases → 5x triage_one_case → collect_batch
              │
              ├──► delivery_request  ← unpartitioned DataFrame, N rows for delivery cases
              ├──► voucher_issued    ← unpartitioned DataFrame, N rows for voucher cases
              └──► escalation        ← unpartitioned DataFrame, N rows for escalation cases

Compare to the per-case partition mode (agentic_router.md):
  - Per-case:  N runs, per-case retry/lineage, dynamic partitions per branch
  - Fan-out:   1 run, all cases parallelized via DynamicOut inside the graph_asset,
               branches are unpartitioned DataFrames with N rows

Browse:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # Click daily_triage_batch's run → see fan_out_cases → 5x triage_one_case[d1..d5] → collect_batch

Cleanup:
  rm -rf $PROJECT_ABS
DONE
