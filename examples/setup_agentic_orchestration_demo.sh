#!/usr/bin/env bash
# agentic_orchestration — the full agents + humans + legacy-systems story.
#
# End-to-end, partitioned per case (t1, t2, t3), 100% components:
#
#   tickets (source, CSV)
#      │  one row per ticket_id
#      ▼
#   triage_agent[ticket_id]   ← LLM #1: classify category + urgency
#      │
#      ▼
#   draft_response[ticket_id] ← LLM #2: draft customer-facing reply
#      │
#      ▼
#   response_approved[ticket_id]   ← HUMAN-IN-THE-LOOP GATE
#      │                              reads /tmp/approvals/<ticket_id>.json
#      │                              missing = approval_pending (Failure)
#      │                              approved:false = approval_rejected (Failure)
#      │                              approved:true = passthrough
#      ▼
#   response_sent[ticket_id]  ← sink: writes to /tmp/sent_responses/ (ticket system)
#   audit_log[ticket_id]      ← sink: appends to DuckDB audit table (legacy warehouse)
#
# Plus: approval_watcher (filesystem_monitor sensor) that watches
# /tmp/approvals/ for token drops → launches approve_and_send_job for the
# matching partition_key → response_approved + response_sent + audit_log
# all materialize for that ticket.
#
# The demo walks the 3-ticket scenario:
#   t1 → gets approved  → cascades to sent + audited
#   t2 → gets rejected  → downstream never runs
#   t3 → left pending   → demonstrates the wait state (drop token via UI/CLI
#                          later to see the sensor auto-progress it)
#
# NEED: OPENAI_API_KEY for the two agent steps. The gate + sensor + sinks
# work offline.

set -eo pipefail

PROJECT_DIR="${1:-agentic-orchestration-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — agent steps will error at materialize time."
  echo "  Set it and re-run to see the full flow: export OPENAI_API_KEY=sk-..."
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

# --- 4. Seed data + approval + sink dirs INSIDE project dir ---------------
# (Windows-portable: keep everything under $PROJECT_ABS)
mkdir -p data approvals sent_responses audit
cat > data/tickets.csv <<'CSV'
ticket_id,customer,subject,body
t1,acme corp,Password reset stuck,"Hi — I tried to reset my password 3 times and every link takes me to an error page saying 'Session expired'. Been locked out for two days. Please help ASAP."
t2,widgets inc,Feature request: dark mode,"Would love a dark theme for the dashboard. Currently kills my eyes at night. Any ETA? Also — happy to beta-test."
t3,globex,Invoice discrepancy on Feb bill,"Our Feb invoice shows 47 seats billed but our admin panel says we have 42 active users. That's $250 extra. Can you double-check and issue a credit if needed?"
CSV

APPROVAL_DIR="$PROJECT_ABS/approvals"
SENT_DIR="$PROJECT_ABS/sent_responses"

# --- 5. defs.yaml files ---------------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"
TICKETS="t1,t2,t3"

# 5.1 Source — tickets from CSV (unpartitioned; the CSV IS the ticket set)
mkdir -p "$DEFS/tickets"
cat > "$DEFS/tickets/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: tickets
  file_path: ${PROJECT_ABS}/data/tickets.csv
  group_name: source
YAML

# 5.2 Agent 1 — triage. LLM classifies category + urgency per ticket.
mkdir -p "$DEFS/triage_agent"
cat > "$DEFS/triage_agent/defs.yaml" <<YAML
type: dagster_community_components.LLMPromptExecutorComponent
attributes:
  asset_name: triage_agent
  upstream_asset_key: tickets
  provider: openai
  model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  input_column: body
  output_column: triage
  system_prompt: "You are a support triage classifier. Output ONE line of the form: category=<technical|billing|feature_request|other>; urgency=<low|medium|high>; brief=<one clause>."
  user_prompt_template: |
    Customer: {customer}
    Subject: {subject}
    Body: {body}

    Classify:
  temperature: 0.0
  max_tokens: 120
  partition_type: static
  partition_values: "${TICKETS}"
  partition_static_column: ticket_id
  group_name: agents
YAML

# 5.3 Agent 2 — specialist writes a draft reply per ticket.
mkdir -p "$DEFS/draft_response"
cat > "$DEFS/draft_response/defs.yaml" <<YAML
type: dagster_community_components.LLMPromptExecutorComponent
attributes:
  asset_name: draft_response
  upstream_asset_key: triage_agent
  provider: openai
  model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  input_column: body
  output_column: draft
  system_prompt: "You are a support specialist. Draft a concise (3-5 sentence) customer-facing reply. If billing/feature-request, do NOT commit to specific dollar amounts or dates — flag those for human review."
  user_prompt_template: |
    Triage: {triage}
    Customer: {customer}
    Subject: {subject}
    Body: {body}

    Draft reply:
  temperature: 0.3
  max_tokens: 300
  partition_type: static
  partition_values: "${TICKETS}"
  partition_static_column: ticket_id
  group_name: agents
YAML

# 5.4 HUMAN GATE — the primitive. Reads approvals/<ticket_id>.json.
mkdir -p "$DEFS/response_approved"
cat > "$DEFS/response_approved/defs.yaml" <<YAML
type: dagster_community_components.HumanApprovalGateComponent
attributes:
  asset_name: response_approved
  upstream_asset_key: draft_response
  approval_dir: ${APPROVAL_DIR}
  partition_type: static
  partition_values: "${TICKETS}"
  group_name: human_in_the_loop
YAML

# 5.5 Sink — post approved response to the ticketing system (simulated as CSV).
mkdir -p "$DEFS/response_sent"
cat > "$DEFS/response_sent/defs.yaml" <<YAML
type: dagster_community_components.DataframeToCsvComponent
attributes:
  asset_name: response_sent
  upstream_asset_key: response_approved
  file_path: ${SENT_DIR}/sent_response_{partition_key}.csv
  partition_type: static
  partition_values: "${TICKETS}"
  group_name: sinks
YAML

# 5.6 Sink — audit trail into a "legacy warehouse" (DuckDB).
mkdir -p "$DEFS/audit_log"
cat > "$DEFS/audit_log/defs.yaml" <<YAML
type: dagster_community_components.DuckDBTableWriterComponent
attributes:
  asset_name: audit_log
  upstream_asset_key: response_approved
  database_path: ${PROJECT_ABS}/audit/audit.duckdb
  table: response_audit
  write_mode: append
  partition_type: static
  partition_values: "${TICKETS}"
  partition_static_column: ticket_id
  group_name: sinks
YAML

# 5.7 Job — what the approval sensor will launch when a token drops.
mkdir -p "$DEFS/approve_and_send_job"
cat > "$DEFS/approve_and_send_job/defs.yaml" <<YAML
type: dagster_community_components.AssetJobComponent
attributes:
  job_name: approve_and_send_job
  asset_keys:
    - response_approved
    - response_sent
    - audit_log
  description: "Materialize the approval gate + downstream sinks for one ticket partition."
YAML

# 5.8 Sensor — watches approval dir, launches the job with partition_key=<file stem>.
mkdir -p "$DEFS/approval_watcher"
cat > "$DEFS/approval_watcher/defs.yaml" <<YAML
type: dagster_community_components.FilesystemMonitorSensorComponent
attributes:
  sensor_name: approval_watcher
  directory_path: ${APPROVAL_DIR}
  file_pattern: "^t[0-9]+\\\\.json$"
  job_name: approve_and_send_job
  minimum_interval_seconds: 5
  partition_mode: static_partition
  # file "t3.json" → partition_key "t3" — matches static values above.
  partition_key_template: "{file_stem}"
YAML

# --- 6. dg check defs -----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

# --- 7. Upstream flow — tickets → triage → draft (all 3 partitions) -------
echo ""
echo ">>> tickets (unpartitioned)"
uv run dg launch --assets tickets 2>&1 | tail -3

if [ -z "$OPENAI_API_KEY" ]; then
  cat <<MSG

! OPENAI_API_KEY not set — skipping agent + gate + sink steps.
Set it and re-run to see the full agentic flow:

  export OPENAI_API_KEY=sk-...
  bash setup_agentic_orchestration_demo.sh $PROJECT_DIR

Cleanup: rm -rf $PROJECT_ABS
MSG
  exit 0
fi

echo ""
echo ">>> triage_agent + draft_response for each ticket partition"
for T in t1 t2 t3; do
  echo "    ─── $T ───"
  uv run dg launch --assets triage_agent   --partition "$T" 2>&1 | tail -2
  uv run dg launch --assets draft_response --partition "$T" 2>&1 | tail -2
done

# --- 8. GATE — no approvals yet. All 3 partitions must fail approval_pending.
echo ""
echo ">>> response_approved BEFORE any tokens drop — expect all 3 to fail approval_pending"
for T in t1 t2 t3; do
  echo "    ─── $T (expect approval_pending Failure) ───"
  uv run dg launch --assets response_approved --partition "$T" 2>&1 | tail -3 || true
done

# --- 9. Drop approval tokens: t1 approved, t2 rejected, t3 left pending ---
echo ""
echo ">>> Dropping approval tokens: t1=approved, t2=rejected, t3=intentionally left pending"

cat > "$APPROVAL_DIR/t1.json" <<'JSON'
{
  "approved": true,
  "approver": "eric@dagsterlabs.com",
  "reason": "Password-reset flow is a known issue; reply is accurate. Ship it."
}
JSON

cat > "$APPROVAL_DIR/t2.json" <<'JSON'
{
  "approved": false,
  "approver": "eric@dagsterlabs.com",
  "reason": "Draft commits to a Q3 ETA but roadmap won't decide until next planning cycle. Reject — rewrite without ETA."
}
JSON

echo "    ($APPROVAL_DIR/t1.json → approved)"
echo "    ($APPROVAL_DIR/t2.json → rejected)"
echo "    (t3.json intentionally missing — approval_pending state)"

# --- 10. Materialize the gate for each. t1 succeeds, t2 rejects, t3 pends.
echo ""
echo ">>> response_approved after tokens dropped"
for T in t1 t2 t3; do
  case "$T" in
    t1) NOTE="approved passthrough" ;;
    t2) NOTE="expect approval_rejected Failure" ;;
    t3) NOTE="expect approval_pending Failure (no token)" ;;
  esac
  echo "    ─── $T ($NOTE) ───"
  uv run dg launch --assets response_approved --partition "$T" 2>&1 | tail -3 || true
done

# --- 11. Downstream sinks for the ONE ticket that passed the gate (t1) ----
echo ""
echo ">>> Downstream sinks — response_sent + audit_log for t1 only (gate blocked t2/t3)"
uv run dg launch --assets response_sent --partition t1 2>&1 | tail -3
uv run dg launch --assets audit_log    --partition t1 2>&1 | tail -3

# --- 12. Show the state -----------------------------------------------------
echo ""
echo ">>> Sent responses on disk:"
ls -1 "$SENT_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"

echo ""
echo ">>> Audit warehouse contents (DuckDB):"
uv run python - <<PY
import duckdb
try:
    con = duckdb.connect("$PROJECT_ABS/audit/audit.duckdb", read_only=True)
    rows = con.execute("SELECT ticket_id, customer, subject FROM response_audit").fetchall()
    if rows:
        print(f"    {len(rows)} row(s) audited:")
        for r in rows:
            print(f"      - {r[0]:>3s}  {r[1]:<15s}  {r[2]}")
    else:
        print("    (empty)")
    con.close()
except Exception as e:
    print(f"    (audit read failed: {e})")
PY

# --- 13. Explainer of what to try next in the UI --------------------------
cat <<DONE

✓ agentic_orchestration demo done.

Final state (browse in the UI):
  t1 → approved → sent + audited  ✓
  t2 → rejected → downstream never ran  ✗ (approval_rejected)
  t3 → pending  → downstream never ran  … (approval_pending, waiting for token)

Browse the graph:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000

In the UI you'll see 5 asset groups: source / agents / human_in_the_loop / sinks
and one sensor: approval_watcher (starts running by default).

Watch the sensor auto-progress t3 while dg dev is running:

  echo '{"approved": true, "approver": "eric", "reason": "credited $250"}' \\
    > $APPROVAL_DIR/t3.json

Within a few seconds, approval_watcher fires, launches approve_and_send_job
--partition t3, and you'll see response_approved[t3] + response_sent[t3] +
audit_log[t3] materialize on their own. No manual click. That's the
"agents + human + legacy warehouse" loop closed end-to-end.

Reject a ticket via edit-then-drop:
  echo '{"approved": false, "approver": "eric", "reason": "…"}' \\
    > $APPROVAL_DIR/<ticket_id>.json
  # sensor fires → gate fails approval_rejected → downstream stays untouched.

Cleanup:
  rm -rf $PROJECT_ABS
DONE
