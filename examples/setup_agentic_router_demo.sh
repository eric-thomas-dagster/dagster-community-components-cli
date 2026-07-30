#!/usr/bin/env bash
# agentic_router — the full router-loop pattern from Prefect/BPMN "agentic
# orchestration" diagrams, but every step + tool call + human decision is
# a first-class Dagster asset.
#
# Story: baggage-loss triage (matches the airline BPMN example).
#
# Per case (partition_key = c1 / c2 / c3):
#   1. iterative_supervisor_agent — planner LOOPS with a bounded tool set:
#        - query_baggage_db
#        - inquire_with_airport
#        - request_info_from_passenger
#        - send_care_voucher
#        - organize_delivery
#        - inform_passenger
#      Each iteration is its own asset (agent_step_1..agent_step_5) showing
#      what the planner decided + which tool was called + the tool output.
#
#   2. agent_final_answer — synthesizer reads all step trajectories, writes
#      the final resolution plan.
#
#   3. resolution — LLM classifier reads the synthesis and emits routing:
#        {status: auto_resolved | needs_human_review,
#         compensate: true | false,
#         amount_usd: int,
#         reason: str}
#
#   4. human_review — HumanApprovalGate. Setup script mirrors the "auto-
#      approve when confident" pattern by writing the token itself for
#      auto_resolved cases; for needs_human_review it leaves the token
#      unwritten so the gate PENDS until a human (or sensor) drops it.
#
#   5. notification_sent — DataframeToCsv sink (stand-in for CRM push).
#   6. compensation_paid — DataframeToCsv sink (stand-in for finance/AP).
#   7. case_audit — DuckDBTableWriter append into a "legacy warehouse."
#
# Plus: approval_watcher (filesystem_monitor, partition_mode: static) —
# watches the approvals dir; on new token, launches approve_and_process_job
# for that partition and downstream cascades automatically.
#
# NEED: OPENAI_API_KEY. All other pieces work offline.

set -eo pipefail

PROJECT_DIR="${1:-agentic-router-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — agent + LLM steps will error at materialize time."
  echo "  Set it and re-run: export OPENAI_API_KEY=sk-..."
fi

# --- 1. Fresh project scaffold --------------------------------------------
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
uv add -q "$DCC_SRC" pandas openai duckdb

# --- 4. Seed data + working dirs (Windows-portable — everything under project) ---
mkdir -p data approvals notifications compensations audit

cat > data/baggage_reports.csv <<'CSV'
case_id,passenger,flight,baggage_id,description
c1,Alice,UA100 ORD→LHR,BAG-001,Checked bag never arrived at LHR arrivals.
c2,Bob,AF200 JFK→CDG,BAG-002,Bag missing 3 days now — no updates from airline.
c3,Carol,DL300 SFO→JFK,BAG-003,Airline confirms bag is at JFK; need it delivered to my home.
CSV

APPROVAL_DIR="$PROJECT_ABS/approvals"
NOTIFICATIONS_DIR="$PROJECT_ABS/notifications"
COMPENSATIONS_DIR="$PROJECT_ABS/compensations"

# --- 5. defs.yaml files ---------------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"
CASES="c1,c2,c3"

# 5.1 Source — the 3 baggage reports (unpartitioned CSV load)
mkdir -p "$DEFS/baggage_reports"
cat > "$DEFS/baggage_reports/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: baggage_reports
  file_path: ${PROJECT_ABS}/data/baggage_reports.csv
  group_name: source
YAML

# 5.2 ROUTER — iterative supervisor agent with 6 tools, partitioned by case.
# Task string embeds all 3 cases so the tools have enough context to answer.
# In production the case metadata would be loaded from the upstream DataFrame
# via a `data:` field on the agent; here we inline for the demo.
mkdir -p "$DEFS/baggage_agent"
cat > "$DEFS/baggage_agent/defs.yaml" <<'YAML'
type: dagster_community_components.IterativeSupervisorAgentComponent
attributes:
  step_asset_prefix: agent_step
  synthesis_asset_name: agent_final_answer
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  max_iterations: 5
  temperature: 0.0
  partition_type: static
  partition_values: "c1,c2,c3"
  group_name: router_agent
  task: |
    You are a baggage-loss triage agent for an airline. Resolve case {partition_key}.

    Case data (from the ticketing system):
      c1: passenger=Alice, flight=UA100 ORD→LHR, baggage_id=BAG-001, description="Checked bag never arrived at LHR arrivals."
      c2: passenger=Bob, flight=AF200 JFK→CDG, baggage_id=BAG-002, description="Bag missing 3 days now — no updates from airline."
      c3: passenger=Carol, flight=DL300 SFO→JFK, baggage_id=BAG-003, description="Airline confirms bag is at JFK; need it delivered to my home."

    Standard operating procedure (do not deviate):
      1. Query the baggage tracking DB for this baggage_id (via query_baggage_db).
      2. If the bag is located → organize delivery (organize_delivery) → inform the passenger (inform_passenger) → declare done.
      3. If the bag is NOT found → inquire with the last-known airport (inquire_with_airport).
         3a. If the airport can locate → organize delivery + inform passenger → done.
         3b. If still not found → send a care voucher (send_care_voucher, amount 75) AND request more info from the passenger (request_info_from_passenger). Then declare done — this case will need human review to close.

    Available tools (call by name, don't invent):
      - query_baggage_db(baggage_id)
      - inquire_with_airport(airport_code)
      - request_info_from_passenger(question)
      - send_care_voucher(passenger_id, amount_usd)
      - organize_delivery(baggage_id, address)
      - inform_passenger(passenger_id, message)

    When done, the synthesizer output should include either:
      "OUTCOME: resolved — <one-line summary>" (bag located + delivery organized)
    OR:
      "OUTCOME: needs_human_review — <one-line summary>" (bag not found; voucher sent, awaiting passenger reply)

  tools:
    - name: query_baggage_db
      description: "Query the baggage tracking database for a case. Args: baggage_id (string like BAG-001)."
      system_message: |
        You are the baggage tracking database. Respond ONLY with the tracking record — no commentary.
        Current DB contents:
          BAG-001: last_scanned_at=ORD, hours_since_scan=6, status=in_transit_to_LHR, eta_hours=4, delivery_address="12 Baker St, London W1U 3BE"
          BAG-002: last_scanned_at=CDG, hours_since_scan=72, status=not_found, eta_hours=NA, delivery_address=NA
          BAG-003: last_scanned_at=JFK, hours_since_scan=2, status=awaiting_delivery, eta_hours=NA, delivery_address="123 Main St, Brooklyn NY 11201"
        Given a baggage_id, respond in the form:
          "baggage_id=<id>; status=<status>; last_scanned_at=<code>; eta_hours=<n or NA>; delivery_address=<address or NA>"
        If the baggage_id is unknown, respond: "baggage_id=<id>; status=unknown_baggage_id"

    - name: inquire_with_airport
      description: "Send a lookup inquiry to a specific airport's baggage services. Args: airport_code (IATA)."
      system_message: |
        You are the airport-baggage-services inquiry response system. Respond ONLY with the airport response — no commentary.
        Records:
          ORD: BAG-001 was scanned inbound at 08:14 and transferred to LHR-bound flight.
          CDG: BAG-002 was NOT found in any bag hall or oversize storage; possibly misrouted to unknown destination.
          LHR: no matching records for BAG-001 yet (may still be in transit).
          JFK: BAG-003 is in the delivery-pending queue.
        Given an airport_code, respond in the form:
          "airport=<code>; response=<one-line status>"

    - name: request_info_from_passenger
      description: "Send a question to the passenger and get their (simulated) reply. Args: question (short string)."
      system_message: |
        You are simulating the passenger's reply. Keep it terse.
        Respond: "passenger_reply=<one sentence>"

    - name: send_care_voucher
      description: "Issue a care-package voucher to the passenger. Args: passenger_id (string), amount_usd (integer)."
      system_message: |
        You are the voucher issuance service. Respond only with the confirmation:
          "voucher_id=V<random-6-digits>; passenger_id=<id>; amount_usd=<amount>; status=issued"

    - name: organize_delivery
      description: "Book a courier to deliver the bag. Args: baggage_id (string), address (string)."
      system_message: |
        You are the courier booking system. Respond only with:
          "delivery_id=D<random-6-digits>; baggage_id=<id>; address=<address>; scheduled_eta_hours=8; status=scheduled"

    - name: inform_passenger
      description: "Send a message to the passenger. Args: passenger_id (string), message (short string)."
      system_message: |
        You are the passenger notification system. Respond only with:
          "notification_id=N<random-6-digits>; passenger_id=<id>; message=<message>; status=sent"
YAML

# 5.3 Resolution — LLM classifier reads agent_final_answer, outputs routing JSON.
mkdir -p "$DEFS/resolution"
cat > "$DEFS/resolution/defs.yaml" <<YAML
type: dagster_community_components.LLMPromptExecutorComponent
attributes:
  asset_name: resolution
  upstream_asset_key: agent_final_answer
  provider: openai
  model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  input_column: answer
  output_column: routing
  system_prompt: |
    You classify a baggage-loss agent's final answer. Return ONE line ONLY of the form:
      status=<auto_resolved|needs_human_review>; compensate=<true|false>; amount_usd=<integer>; reason=<one clause>

    Strict rules:
      - status=auto_resolved ONLY when the answer contains the marker 'OUTCOME: resolved' — i.e. the agent successfully organized delivery AND informed the passenger.
      - status=needs_human_review when the answer contains 'OUTCOME: needs_human_review' — i.e. bag not located, voucher issued, passenger info requested.
      - compensate=true ONLY when a care voucher was sent (search the transcript for 'voucher') — 75 USD default.
      - compensate=false in all other cases (auto-resolved delivery counts as good service, no comp needed).
      - amount_usd=0 when compensate=false.
  user_prompt_template: |
    Agent's final answer for case {partition_key}:
    {answer}

    Classify:
  temperature: 0.0
  max_tokens: 150
  partition_type: static
  partition_values: "${CASES}"
  group_name: routing
YAML

# 5.4 HUMAN GATE — same primitive from the earlier agentic demo.
mkdir -p "$DEFS/human_review"
cat > "$DEFS/human_review/defs.yaml" <<YAML
type: dagster_community_components.HumanApprovalGateComponent
attributes:
  asset_name: human_review
  upstream_asset_key: resolution
  approval_dir: ${APPROVAL_DIR}
  partition_type: static
  partition_values: "${CASES}"
  group_name: human_in_the_loop
YAML

# 5.5 Sink — notification post-back to CRM/ticketing system (per case).
mkdir -p "$DEFS/notification_sent"
cat > "$DEFS/notification_sent/defs.yaml" <<YAML
type: dagster_community_components.DataframeToCsvComponent
attributes:
  asset_name: notification_sent
  upstream_asset_key: human_review
  file_path: ${NOTIFICATIONS_DIR}/notification_{partition_key}.csv
  partition_type: static
  partition_values: "${CASES}"
  group_name: sinks
YAML

# 5.6 Sink — compensation record (per case). Always writes; downstream
# consumers filter by routing.compensate to actually pay.
mkdir -p "$DEFS/compensation_paid"
cat > "$DEFS/compensation_paid/defs.yaml" <<YAML
type: dagster_community_components.DataframeToCsvComponent
attributes:
  asset_name: compensation_paid
  upstream_asset_key: human_review
  file_path: ${COMPENSATIONS_DIR}/compensation_{partition_key}.csv
  partition_type: static
  partition_values: "${CASES}"
  group_name: sinks
YAML

# 5.7 Sink — audit trail into the "legacy warehouse" (DuckDB, append).
mkdir -p "$DEFS/case_audit"
cat > "$DEFS/case_audit/defs.yaml" <<YAML
type: dagster_community_components.DuckDBTableWriterComponent
attributes:
  asset_name: case_audit
  upstream_asset_key: human_review
  database_path: ${PROJECT_ABS}/audit/case_audit.duckdb
  table: baggage_case_audit
  write_mode: append
  partition_type: static
  partition_values: "${CASES}"
  group_name: sinks
YAML

# 5.8 Job — what the approval sensor launches on token drop.
mkdir -p "$DEFS/approve_and_process_job"
cat > "$DEFS/approve_and_process_job/defs.yaml" <<YAML
type: dagster_community_components.AssetJobComponent
attributes:
  job_name: approve_and_process_job
  asset_keys:
    - human_review
    - notification_sent
    - compensation_paid
    - case_audit
  description: "Materialize human_review + downstream sinks for one case partition on token drop."
YAML

# 5.9 Sensor — watches approvals dir, launches per-partition on token arrival.
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

# --- 7. Upstream — source + agent + resolution for each partition ---------
echo ""
echo ">>> baggage_reports (unpartitioned source)"
uv run dg launch --assets baggage_reports 2>&1 | tail -3

if [ -z "$OPENAI_API_KEY" ]; then
  cat <<MSG

! OPENAI_API_KEY not set — skipping router + resolution + gate + sinks.
Set it and re-run to see the full agentic router flow:

  export OPENAI_API_KEY=sk-...
  bash setup_agentic_router_demo.sh $PROJECT_DIR

Cleanup: rm -rf $PROJECT_ABS
MSG
  exit 0
fi

# The agent's 5 step assets + synthesizer materialize together for each partition
# via a single --assets glob per partition. Materialize each case.
echo ""
echo ">>> Router agent — 5 step assets + agent_final_answer, per case partition"
AGENT_ASSETS="agent_step_1,agent_step_2,agent_step_3,agent_step_4,agent_step_5,agent_final_answer"
for C in c1 c2 c3; do
  echo "    ─── $C ───"
  uv run dg launch --assets "$AGENT_ASSETS" --partition "$C" 2>&1 | tail -3
done

echo ""
echo ">>> resolution — LLM classifies each case's agent output"
for C in c1 c2 c3; do
  echo "    ─── $C ───"
  uv run dg launch --assets resolution --partition "$C" 2>&1 | tail -2
done

# --- 8. Auto-write approval tokens for auto_resolved cases ----------------
# In a production wire-up, an assessor asset upstream of human_review would
# write these tokens itself when resolution.status=='auto_resolved'. The
# gate is stateless — it can't tell if a human or an LLM wrote the token.
# Here the setup script does that step so the demo is self-contained; the
# walkthrough shows the pattern for wiring it into the graph.
echo ""
echo ">>> Reading each case's resolution + auto-writing approval tokens for auto_resolved"
uv run python - <<PY
import os, json, glob
os.environ["DAGSTER_HOME"] = "$DAGSTER_HOME"
import pickle
from pathlib import Path

storage = Path("$PROJECT_ABS/.dagster_home/storage/resolution")
approval_dir = Path("$APPROVAL_DIR")
for pk_file in sorted(storage.iterdir()):
    if not pk_file.is_file():
        continue
    with open(pk_file, "rb") as f:
        df = pickle.load(f)
    row = df.iloc[0]
    pk = pk_file.name
    routing = str(row.get("routing", "")).strip()
    print(f"    {pk}: {routing[:120]}")
    if "status=auto_resolved" in routing:
        token = {
            "approved": True,
            "approver": "auto_resolver (llm-driven, high-confidence)",
            "reason": routing[:200],
        }
        (approval_dir / f"{pk}.json").write_text(json.dumps(token, indent=2))
        print(f"      → auto-approval token written to approvals/{pk}.json")
    elif "status=needs_human_review" in routing:
        print(f"      → NO token written — case will PEND at human_review, awaiting a human")
    else:
        print(f"      → resolution unclear ({routing[:80]}...) — leaving unwritten")
PY

# --- 9. Materialize human_review + downstream sinks for each case ---------
echo ""
echo ">>> human_review + downstream sinks per partition"
echo "    (auto-resolved cases pass immediately; needs_human_review pends)"
for C in c1 c2 c3; do
  echo "    ─── $C ───"
  uv run dg launch --assets human_review    --partition "$C" 2>&1 | tail -3 || true
  # Downstream sinks only proceed if the gate passed.
  uv run dg launch --assets notification_sent --partition "$C" 2>&1 | tail -3 || true
  uv run dg launch --assets compensation_paid --partition "$C" 2>&1 | tail -3 || true
  uv run dg launch --assets case_audit        --partition "$C" 2>&1 | tail -3 || true
done

# --- 10. Show the state ---------------------------------------------------
echo ""
echo ">>> Notifications on disk:"
ls -1 "$NOTIFICATIONS_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"

echo ""
echo ">>> Compensation records on disk:"
ls -1 "$COMPENSATIONS_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"

echo ""
echo ">>> Audit warehouse contents (DuckDB):"
uv run python - <<PY
import duckdb
try:
    con = duckdb.connect("$PROJECT_ABS/audit/case_audit.duckdb", read_only=True)
    rows = con.execute("SELECT n_tool_calls, routing FROM baggage_case_audit").fetchall()
    if rows:
        print(f"    {len(rows)} row(s) audited (n_tool_calls + routing per case):")
        for r in rows:
            n, routing = r
            print(f"      - {n} tool calls | {str(routing)[:100]}")
    else:
        print("    (empty)")
    con.close()
except Exception as e:
    print(f"    (audit read failed: {e})")
PY

# --- 11. Explainer of what to try next in the UI --------------------------
cat <<DONE

✓ agentic_router demo done.

Final state:
  c1 (BAG-001, in_transit_to_LHR)     → router resolved → auto-approved → sent + audited ✓
  c3 (BAG-003, at JFK, delivery-ready)→ router resolved → auto-approved → sent + audited ✓
  c2 (BAG-002, not_found at CDG)      → router exhausted tools → needs_human_review → PENDS at human_review, waiting for a token

Browse the graph:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000

In the UI you'll see (per case partition):
  - 5 agent_step_N assets + agent_final_answer
    (click a step to see planner reasoning + tool called + tool output)
  - resolution → shows the routing classification per case
  - human_review → c1/c3 approved (auto), c2 approval_pending
  - notification_sent / compensation_paid / case_audit downstream

Approve c2 manually and watch approval_watcher cascade:

  echo '{"approved": true, "approver": "you", "reason": "voucher sent, case will re-open when passenger replies"}' \\
    > $APPROVAL_DIR/c2.json

Within ~5s, approval_watcher fires → launches approve_and_process_job with
partition_key=c2 → human_review[c2] + notification_sent[c2] +
compensation_paid[c2] + case_audit[c2] all materialize on their own.

Reject c2 instead:
  echo '{"approved": false, "approver": "you", "reason": "insufficient evidence — escalate to airport ops"}' \\
    > $APPROVAL_DIR/c2.json

Cleanup:
  rm -rf $PROJECT_ABS
DONE
