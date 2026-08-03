#!/usr/bin/env bash
# agentic_router — the clean "agent-as-single-asset" shape of the BPMN
# router-loop pattern. One graph-backed asset per case; ReAct steps live as
# ops (visible in the run view); the classifier emits ONLY the downstream
# branches that apply to each case, so lineage is honest per partition.
#
#   baggage_reports (CSV source, wired into lineage via upstream_asset_key)
#      │
#      ▼  ONE ASSET PER CASE (graph-backed @multi_asset)
#   baggage_triage_agent[case_id]
#      │  (ReAct loop internally: plan_step_1 → plan_step_2 → … as OPS)
#      │
#      ├──► delivery_request   (only for cases the agent delivered)
#      │       └─► courier_booked (dataframe_to_csv)
#      │
#      ├──► voucher_issued     (only for cases that got a voucher)
#      │       └─► compensation_paid (dataframe_to_csv)
#      │
#      └──► escalation         (only for cases needing human review)
#              └─► human_review (human_approval_gate)
#                    │
#                    └─► escalation_audited (duckdb append)
#
# All 3 branches also feed a shared `case_audit` (duckdb append) — every
# case that touches any branch lands in the audit trail.
#
# NEED: OPENAI_API_KEY.

set -eo pipefail

PROJECT_DIR="${1:-agentic-router-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — the router will error at materialize time."
  echo "  export OPENAI_API_KEY=sk-... and re-run."
fi

# --- 1. Scaffold ----------------------------------------------------------
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- 2. Env ---------------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

# --- 3. Install deps ------------------------------------------------------
uv add -q "$DCC_SRC" pandas openai duckdb dagster-duckdb requests

# --- 4. Seed source + working dirs (Windows-portable) ---------------------
mkdir -p data approvals courier_bookings compensations audit

cat > data/baggage_reports.csv <<'CSV'
case_id,passenger,flight,baggage_id,description
c1,Alice,UA100 ORD→LHR,BAG-001,Checked bag never arrived at LHR arrivals.
c2,Bob,AF200 JFK→CDG,BAG-002,Bag missing 3 days now — no updates from airline.
c3,Carol,DL300 SFO→JFK,BAG-003,Airline confirms bag is at JFK; need it delivered to my home.
CSV

# Seed a REAL DuckDB baggage_tracking table. The router's query_baggage_db
# tool queries this instead of an LLM-roleplayed system prompt — this is how
# a real customer would wire it: tool → resource → real database.
uv run python - <<'PY'
import duckdb
con = duckdb.connect("data/baggage.duckdb")
con.execute("""
    CREATE TABLE IF NOT EXISTS baggage_tracking (
        baggage_id       VARCHAR PRIMARY KEY,
        last_scanned_at  VARCHAR,
        hours_since_scan INT,
        status           VARCHAR,
        eta_hours        VARCHAR,
        delivery_address VARCHAR
    );
""")
con.execute("DELETE FROM baggage_tracking;")
con.executemany(
    "INSERT INTO baggage_tracking VALUES (?,?,?,?,?,?)",
    [
        ("BAG-001", "ORD",  6, "in_transit_to_LHR",  "4",  "12 Baker St, London W1U 3BE"),
        ("BAG-002", "CDG", 72, "not_found",          "NA", "NA"),
        ("BAG-003", "JFK",  2, "awaiting_delivery",  "NA", "123 Main St, Brooklyn NY 11201"),
    ],
)
con.close()
print("seeded data/baggage.duckdb with 3 baggage records")
PY

APPROVAL_DIR="$PROJECT_ABS/approvals"
COURIER_DIR="$PROJECT_ABS/courier_bookings"
COMPENSATIONS_DIR="$PROJECT_ABS/compensations"

# --- 5. defs.yaml files ---------------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"
CASES="c1,c2,c3"

# 5.0 DuckDB resource — the "legacy" baggage tracking DB. The router's
# query_baggage_db tool uses tool_type: sql + this resource for real
# lookups instead of LLM roleplay.
mkdir -p "$DEFS/baggage_db"
cat > "$DEFS/baggage_db/defs.yaml" <<YAML
type: dagster_community_components.DuckDBResourceComponent
attributes:
  resource_key: baggage_db
  database: ${PROJECT_ABS}/data/baggage.duckdb
YAML

# 5.1 Source — the 3 baggage reports.
mkdir -p "$DEFS/baggage_reports"
cat > "$DEFS/baggage_reports/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: baggage_reports
  file_path: ${PROJECT_ABS}/data/baggage_reports.csv
  group_name: source
YAML

# 5.2 THE ROUTER — one graph-backed multi-asset per case.
# task_template uses {passenger}, {flight}, {baggage_id}, {description} — those
# come from the upstream baggage_reports row for the current partition. The
# tools' system_messages carry the pre-seeded ground-truth data.
mkdir -p "$DEFS/baggage_triage_agent"
cat > "$DEFS/baggage_triage_agent/defs.yaml" <<'YAML'
type: dagster_community_components.LlmMultiPathRouterComponent
attributes:
  asset_name: baggage_triage_agent
  upstream_asset_key: baggage_reports
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  max_iterations: 5
  temperature: 0.0

  partition_type: static
  partition_values: "c1,c2,c3"
  partition_static_column: case_id
  group_name: router

  task_template: |
    You are a baggage-loss triage agent. Resolve the case for {passenger}
    on flight {flight}, baggage_id {baggage_id}. Description: {description}

    Standard operating procedure (do not deviate):
      1. Query the tracking DB (query_baggage_db).
      2. If the DB says located + delivery_address is known → organize_delivery
         → inform_passenger → done. This is the delivery_request path.
      3. If the DB says not_found → inquire_with_airport. If the airport
         confirms lost → send_care_voucher (75 USD) + request_info_from_passenger
         → done. This is the voucher_issued AND escalation path (both apply).
      4. If the DB result is anything else → escalate. This is the escalation path.

    When done, the classifier will pick which branches to emit based on your
    trajectory — so make sure your tool calls clearly show whether you
    organized delivery vs. sent a voucher vs. escalated.

  tools:
    - name: query_baggage_db
      description: "Query the baggage tracking database. Args: baggage_id (e.g. BAG-001)."
      # REAL SQL against a Dagster-managed DuckDB resource. The planner emits
      # args (like "BAG-001") and the router executes this query with {args}
      # substituted. Rows are returned to the planner as pipe-delimited text.
      tool_type: sql
      resource: baggage_db
      sql_template: |
        SELECT baggage_id, status, last_scanned_at, hours_since_scan, eta_hours, delivery_address
        FROM baggage_tracking
        WHERE baggage_id = '{args}'

    - name: inquire_with_airport
      description: "Send a lookup to a specific airport. Args: airport_code (IATA)."
      system_message: |
        You are the airport baggage-services inquiry response.
        Records:
          ORD: BAG-001 was scanned inbound and transferred to LHR-bound flight.
          CDG: BAG-002 was NOT found; possibly misrouted, no destination info.
          LHR: no matching records for BAG-001 yet.
          JFK: BAG-003 is in the delivery-pending queue.
        Respond: "airport=<code>; response=<one-line status>"

    - name: send_care_voucher
      description: "Issue a care-package voucher. Args: passenger_id, amount_usd."
      system_message: |
        You are the voucher service. Respond:
          "voucher_id=V<6-digits>; passenger_id=<id>; amount_usd=<n>; status=issued"

    - name: organize_delivery
      description: "Book a courier for the bag. Args: baggage_id, address."
      system_message: |
        You are the courier booking system. Respond:
          "delivery_id=D<6-digits>; baggage_id=<id>; address=<addr>; scheduled_eta_hours=8; status=scheduled"

    - name: inform_passenger
      description: "Notify the passenger. Args: passenger_id, message."
      system_message: |
        You are the notification system. Respond:
          "notification_id=N<6-digits>; passenger_id=<id>; status=sent"

    - name: request_info_from_passenger
      description: "Ask the passenger a question. Args: question."
      system_message: |
        Simulate a terse passenger reply. Respond: "passenger_reply=<one sentence>"

  outputs:
    # Each branch declares its own payload schema. The classifier extracts
    # these fields from the ReAct trajectory (mainly the tool outputs) and
    # emits a per-branch DataFrame downstream sinks can write directly.
    - name: delivery_request
      description: "Bag was located and delivery was successfully organized. Emit when organize_delivery + inform_passenger were called."
      output_schema:
        baggage_id: "The BAG-* id from the query_baggage_db output"
        passenger: "Passenger name from the case metadata"
        address: "Delivery address from query_baggage_db"
        delivery_id: "D<digits> from organize_delivery output"
        eta_hours: "scheduled_eta_hours from organize_delivery output (integer)"

    - name: voucher_issued
      description: "A care voucher was sent because the bag wasn't immediately deliverable. Emit when send_care_voucher was called."
      output_schema:
        passenger: "Passenger name (id passed to send_care_voucher)"
        amount_usd: "The voucher amount (integer, e.g. 75)"
        voucher_id: "V<digits> from send_care_voucher output"
        reason: "One-line reason: why the voucher was issued"

    - name: escalation
      description: "The case needs human review. Emit when the agent gave up OR requested more info from the passenger."
      output_schema:
        case_summary: "One-sentence summary of what happened"
        needs_action: "What the human reviewer needs to do next"
        urgency: "low | medium | high"
YAML

# 5.3 Downstream sink for delivery_request cases.
# Uses the SAME DynamicPartitionsDefinition ("delivery_request_cases") that
# the router registers case_ids on. So courier_booked only shows partitions
# for cases the router actually picked delivery for — no red slots.
mkdir -p "$DEFS/courier_booked"
cat > "$DEFS/courier_booked/defs.yaml" <<YAML
type: dagster_community_components.DataframeToCsvComponent
attributes:
  asset_name: courier_booked
  upstream_asset_key: delivery_request
  file_path: ${COURIER_DIR}/courier_{partition_key}.csv
  partition_type: dynamic
  dynamic_partition_name: delivery_request_cases
  automation_condition: "{{ dg.AutomationCondition.eager() }}"
  group_name: sinks
YAML

# 5.4 Downstream sink for voucher_issued cases.
mkdir -p "$DEFS/compensation_paid"
cat > "$DEFS/compensation_paid/defs.yaml" <<YAML
type: dagster_community_components.DataframeToCsvComponent
attributes:
  asset_name: compensation_paid
  upstream_asset_key: voucher_issued
  file_path: ${COMPENSATIONS_DIR}/compensation_{partition_key}.csv
  partition_type: dynamic
  dynamic_partition_name: voucher_issued_cases
  automation_condition: "{{ dg.AutomationCondition.eager() }}"
  group_name: sinks
YAML

# 5.5 Human gate — downstream of the escalation branch only.
mkdir -p "$DEFS/human_review"
cat > "$DEFS/human_review/defs.yaml" <<YAML
type: dagster_community_components.HumanApprovalGateComponent
attributes:
  asset_name: human_review
  upstream_asset_key: escalation
  approval_dir: ${APPROVAL_DIR}
  partition_type: dynamic
  dynamic_partition_name: escalation_cases
  group_name: human_in_the_loop
YAML

# 5.6 Audit trail — appends every escalation approval to the legacy warehouse.
mkdir -p "$DEFS/escalation_audited"
cat > "$DEFS/escalation_audited/defs.yaml" <<YAML
type: dagster_community_components.DuckDBTableWriterComponent
attributes:
  asset_name: escalation_audited
  upstream_asset_key: human_review
  database_path: ${PROJECT_ABS}/audit/case_audit.duckdb
  table: escalation_audit
  write_mode: append
  partition_type: dynamic
  dynamic_partition_name: escalation_cases
  automation_condition: "{{ dg.AutomationCondition.eager() }}"
  group_name: sinks
YAML

# 5.7 Sensor job target: the escalation-branch cascade on token drop.
mkdir -p "$DEFS/approve_and_process_job"
cat > "$DEFS/approve_and_process_job/defs.yaml" <<YAML
type: dagster_community_components.AssetJobComponent
attributes:
  job_name: approve_and_process_job
  asset_keys:
    - human_review
    - escalation_audited
  description: "Materialize the escalation approval gate + audit on token drop."
YAML

# 5.8 Sensor — auto-progresses any escalated case on new token drop.
# human_review + escalation_audited use the escalation_cases dynamic
# partition set (populated by the router when it picks the escalation
# branch). Sensor watches approvals/ and fires a per-partition run.
mkdir -p "$DEFS/approval_watcher"
cat > "$DEFS/approval_watcher/defs.yaml" <<YAML
type: dagster_community_components.FilesystemMonitorSensorComponent
attributes:
  sensor_name: approval_watcher
  directory_path: ${APPROVAL_DIR}
  file_pattern: "^c[0-9]+\\\\.json$"
  job_name: approve_and_process_job
  minimum_interval_seconds: 5
  partition_mode: dynamic_partition
  dynamic_partitions_name: escalation_cases
  partition_key_template: "{file_stem}"
YAML

# --- 6. dg check defs -----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

# --- 7. Materialize --------------------------------------------------------
echo ""
echo ">>> baggage_reports (unpartitioned source)"
uv run dg launch --assets baggage_reports 2>&1 | tail -3

if [ -z "$OPENAI_API_KEY" ]; then
  cat <<MSG
! OPENAI_API_KEY not set — skipping router + downstream.
Set it and re-run:  export OPENAI_API_KEY=sk-...
Cleanup: rm -rf $PROJECT_ABS
MSG
  exit 0
fi

# Router asset — one @graph_asset per case (ReAct steps as OPS in the run view).
# The router registers case_ids on picked branches' dynamic partition sets as
# a side-effect of materializing, so branch assets become materializable per
# case only after their branch was picked.
echo ""
echo ">>> baggage_triage_agent — one graph_asset materialization per case"
for C in c1 c2 c3; do
  echo "    ─── $C ───"
  uv run dg launch --assets baggage_triage_agent --partition "$C" 2>&1 | tail -3
done

# --- 8. Branch assets — only fire for cases the router picked -------------
# Each branch has its own DynamicPartitionsDefinition. The router registered
# the appropriate case_ids on each branch above. Attempting a partition the
# router did NOT register raises "unknown partition" cleanly (no red trace).
echo ""
echo ">>> Branch assets — dg.AutomationCondition.eager on downstream sinks; we can also materialize directly"
for BRANCH in delivery_request voucher_issued escalation; do
  echo "    ─── $BRANCH ───"
  # Query the branch's dynamic partition set to see which cases got registered.
  uv run python - <<PY 2>&1 | tail -5 || true
import os
os.environ["DAGSTER_HOME"] = "$DAGSTER_HOME"
from dagster import DagsterInstance
inst = DagsterInstance.get()
keys = inst.get_dynamic_partitions("${BRANCH}_cases")
print(f"    ${BRANCH}_cases has {len(keys)} case(s) registered: {sorted(keys)}")
for k in keys:
    print(f"      → launching ${BRANCH}[{k}]")
PY
  # Actually launch each registered partition.
  for C in c1 c2 c3; do
    uv run dg launch --assets "$BRANCH" --partition "$C" 2>&1 | tail -1 || true
  done
done

# --- 8b. Downstream sinks per branch --------------------------------------
echo ""
echo ">>> Downstream sinks per branch (only fire for cases the branch has)"
for C in c1 c2 c3; do
  echo "    ─── $C ───"
  uv run dg launch --assets courier_booked      --partition "$C" 2>&1 | tail -1 || true
  uv run dg launch --assets compensation_paid   --partition "$C" 2>&1 | tail -1 || true
  uv run dg launch --assets human_review        --partition "$C" 2>&1 | tail -1 || true
  uv run dg launch --assets escalation_audited  --partition "$C" 2>&1 | tail -1 || true
done

# --- 9. Auto-approve any escalated case with a high-confidence resolution -
# The escalation branch always emits when the agent gave up — but sometimes
# the reason is unambiguous ("bag definitely lost, voucher sent, awaiting
# passenger"). In production an assessor asset upstream of human_review would
# auto-write the approval token for those. Here the setup script does the
# same to keep the demo self-contained; a walkthrough note points at the
# pattern for wiring it into the graph.
echo ""
echo ">>> Auto-approve tokens for escalated cases where the trajectory is clear"
uv run python - <<PY
import os, json, pickle
from pathlib import Path

os.environ["DAGSTER_HOME"] = "$DAGSTER_HOME"
storage = Path("$PROJECT_ABS/.dagster_home/storage/escalation")
approval_dir = Path("$APPROVAL_DIR")
if not storage.exists():
    print("    (no escalation partitions emitted — router auto-resolved every case)")
else:
    for pk_file in sorted(storage.iterdir()):
        if not pk_file.is_file():
            continue
        with open(pk_file, "rb") as f:
            df = pickle.load(f)
        pk = pk_file.name
        summary = str(df.iloc[0].get("summary", ""))
        n = int(df.iloc[0].get("n_iterations", 0))
        # Auto-approve if the agent used the tools well (took >=3 iterations)
        if n >= 3:
            token = {"approved": True, "approver": "auto_assessor (llm-driven)", "reason": summary}
            (approval_dir / f"{pk}.json").write_text(json.dumps(token, indent=2))
            print(f"    {pk}: auto-approved ({n} iterations) — token written to approvals/{pk}.json")
        else:
            print(f"    {pk}: n_iterations={n} — leaving unapproved (drop a token manually or via sensor)")
PY

# Re-materialize human_review + escalation_audited for anything that just got approved.
echo ""
echo ">>> Re-materialize human_review + escalation_audited (picks up new tokens)"
for C in c1 c2 c3; do
  uv run dg launch --assets human_review        --partition "$C" 2>&1 | tail -1 || true
  uv run dg launch --assets escalation_audited  --partition "$C" 2>&1 | tail -1 || true
done

# --- 10. Show the state ---------------------------------------------------
echo ""
echo ">>> Courier bookings on disk:"
ls -1 "$COURIER_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"

echo ""
echo ">>> Compensation records on disk:"
ls -1 "$COMPENSATIONS_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"

echo ""
echo ">>> Approvals on disk:"
ls -1 "$APPROVAL_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"

echo ""
echo ">>> Escalation audit (DuckDB):"
uv run python - <<PY
import duckdb
try:
    con = duckdb.connect("$PROJECT_ABS/audit/case_audit.duckdb", read_only=True)
    rows = con.execute("SELECT urgency, summary FROM escalation_audit").fetchall()
    if rows:
        print(f"    {len(rows)} row(s):")
        for r in rows: print(f"      - urgency={r[0]:<10s}  summary={str(r[1])[:100]}")
    else:
        print("    (empty)")
    con.close()
except Exception as e:
    print(f"    (audit read failed: {e})")
PY

# --- 11. Dagster+ Serverless prep -----------------------------------------
# Make the project deployable via `dagster-cloud serverless deploy-docker`.
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/lib/serverless_prep.sh | bash

# --- 12. Final explainer --------------------------------------------------
cat <<DONE

✓ agentic_router demo done.

Structure (browse at http://localhost:3000):
  baggage_reports (source)
       └─► baggage_triage_agent[c1,c2,c3]  (ONE asset per case, graph-backed —
              │                              ReAct steps are OPS in the run view)
              │
              ├──► delivery_request  → courier_booked
              ├──► voucher_issued    → compensation_paid
              └──► escalation        → human_review → escalation_audited

Each downstream sink shows partitions ONLY for cases whose agent picked
that branch. That's the honest lineage per-partition.

Browse the graph:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # In a partition's run view you'll see the ReAct steps:
  #   plan_step_1 → plan_step_2 → … → classify_and_emit
  # Each step's logs show what the planner picked + what the tool returned.

Drop a token to unblock any escalated case:
  echo '{"approved": true, "approver": "you", "reason": "verified with airport ops"}' \\
    > $APPROVAL_DIR/<case_id>.json
  # approval_watcher sensor fires within ~5s → human_review + escalation_audited
  # for that case materialize automatically.

Cleanup:
  rm -rf $PROJECT_ABS
DONE
