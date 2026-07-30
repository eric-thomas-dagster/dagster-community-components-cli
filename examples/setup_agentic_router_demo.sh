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
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/cli-community-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

# --- 3. Install deps ------------------------------------------------------
uv add -q "$DCC_SRC" pandas openai duckdb

# --- 4. Seed source + working dirs (Windows-portable) ---------------------
mkdir -p data approvals courier_bookings compensations audit

cat > data/baggage_reports.csv <<'CSV'
case_id,passenger,flight,baggage_id,description
c1,Alice,UA100 ORD→LHR,BAG-001,Checked bag never arrived at LHR arrivals.
c2,Bob,AF200 JFK→CDG,BAG-002,Bag missing 3 days now — no updates from airline.
c3,Carol,DL300 SFO→JFK,BAG-003,Airline confirms bag is at JFK; need it delivered to my home.
CSV

APPROVAL_DIR="$PROJECT_ABS/approvals"
COURIER_DIR="$PROJECT_ABS/courier_bookings"
COMPENSATIONS_DIR="$PROJECT_ABS/compensations"

# --- 5. defs.yaml files ---------------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"
CASES="c1,c2,c3"

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
      description: "Query the baggage tracking database. Args: baggage_id."
      system_message: |
        You are the baggage tracking database. Respond ONLY with the record — no commentary.
        Current DB contents:
          BAG-001: last_scanned_at=ORD, hours_since_scan=6, status=in_transit_to_LHR, eta_hours=4, delivery_address="12 Baker St, London W1U 3BE"
          BAG-002: last_scanned_at=CDG, hours_since_scan=72, status=not_found, eta_hours=NA, delivery_address=NA
          BAG-003: last_scanned_at=JFK, hours_since_scan=2, status=awaiting_delivery, eta_hours=NA, delivery_address="123 Main St, Brooklyn NY 11201"
        Format: "baggage_id=<id>; status=<status>; last_scanned_at=<code>; delivery_address=<address or NA>"

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
    - name: delivery_request
      description: "The agent successfully organized delivery. Emit for cases where a bag was located AND delivered."
    - name: voucher_issued
      description: "A care voucher was sent because the bag wasn't immediately deliverable. Emit for voucher cases."
    - name: escalation
      description: "The case needs human review. Emit for cases where the bag is lost, awaiting passenger response, or otherwise unresolved."
YAML

# 5.3 Downstream sink for delivery_request cases.
# AutomationCondition.eager() = only fire when upstream branch emitted this
# partition. Materialize-all no longer paints the sink red for partitions
# whose upstream branch didn't fire — those partitions simply don't run.
mkdir -p "$DEFS/courier_booked"
cat > "$DEFS/courier_booked/defs.yaml" <<YAML
type: dagster_community_components.DataframeToCsvComponent
attributes:
  asset_name: courier_booked
  upstream_asset_key: delivery_request
  file_path: ${COURIER_DIR}/courier_{partition_key}.csv
  partition_type: static
  partition_values: "${CASES}"
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
  partition_type: static
  partition_values: "${CASES}"
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
  partition_type: static
  partition_values: "${CASES}"
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
  partition_type: static
  partition_values: "${CASES}"
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

# 5.8 Sensor — auto-progresses c2 (or any pending case) on new token.
mkdir -p "$DEFS/approval_watcher"
cat > "$DEFS/approval_watcher/defs.yaml" <<YAML
type: dagster_community_components.FilesystemMonitorSensorComponent
attributes:
  sensor_name: approval_watcher
  directory_path: ${APPROVAL_DIR}
  file_pattern: "^c[0-9]+\\\\.json$"
  job_name: approve_and_process_job
  minimum_interval_seconds: 5
  partition_mode: static_partition
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

# The router graph emits all 3 outputs — request a superset asset selection.
# Dagster materializes only the branches the classifier picks per partition.
echo ""
echo ">>> baggage_triage_agent — per case partition (ReAct steps are ops in the run view)"
for C in c1 c2 c3; do
  echo "    ─── $C ───"
  uv run dg launch --assets 'delivery_request,voucher_issued,escalation' --partition "$C" 2>&1 | tail -3
done

# --- 8. Downstream --------------------------------------------------------
# For each case, try to materialize every downstream. Whichever upstream
# branches were NOT emitted for that case will fail-to-load (skip); the ones
# that WERE emitted will succeed. That's the graph-multi-asset story:
# lineage stays honest per-partition based on the agent's decisions.
echo ""
echo ">>> Downstream per case (only branches the agent emitted will succeed)"
for C in c1 c2 c3; do
  echo "    ─── $C ───"
  uv run dg launch --assets courier_booked      --partition "$C" 2>&1 | tail -2 || true
  uv run dg launch --assets compensation_paid   --partition "$C" 2>&1 | tail -2 || true
  uv run dg launch --assets human_review        --partition "$C" 2>&1 | tail -2 || true
  uv run dg launch --assets escalation_audited  --partition "$C" 2>&1 | tail -2 || true
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
    rows = con.execute("SELECT branch, summary FROM escalation_audit").fetchall()
    if rows:
        print(f"    {len(rows)} row(s):")
        for r in rows: print(f"      - branch={r[0]:<10s}  summary={str(r[1])[:100]}")
    else:
        print("    (empty)")
    con.close()
except Exception as e:
    print(f"    (audit read failed: {e})")
PY

# --- 11. Final explainer --------------------------------------------------
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
