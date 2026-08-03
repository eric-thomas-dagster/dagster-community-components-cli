#!/usr/bin/env bash
# agentic_batch_triage — dynamic fan-out, done via components.
#
# Uses DynamicFanoutJobComponent — a generic wrapper around Dagster's
# `@dg.op(out=DynamicOut()) → .map() → .collect()` pattern. The user
# provides 3 Python callables (discover / process / collect) as
# `module:function` paths in YAML. The component builds the ops, wires
# them into a @dg.job with DynamicOut, and optionally attaches a schedule.
#
# What comes out of it:
#   - A JOB (not an asset). Runs on demand or on schedule.
#   - Fan-out happens inside ONE run (parallel per the executor's slots).
#   - Prefect analog: this is what Prefect users think of as `task.map()`.

set -eo pipefail

PROJECT_DIR="${1:-agentic-batch-triage-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — the job will error at run time."
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

uv add -q "$DCC_SRC" pandas openai duckdb requests

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

# --- helpers.py: the 3 callables the component's YAML will reference -----
PKG="$(ls src/ | head -1)"
cat > "src/$PKG/helpers.py" <<'PY'
"""User-provided callables for DynamicFanoutJobComponent.

The component wires these into ops + a @dg.job at YAML-load time. Users
write plain Python here; the framework handles fan-out, retry, collect."""
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import duckdb
import pandas as pd

MODEL = "gpt-4o-mini"
MAX_ITER = 5
BRANCH_SCHEMA = {
    "delivery_request": ["baggage_id", "passenger", "address", "delivery_id", "eta_hours"],
    "voucher_issued":   ["passenger", "amount_usd", "voucher_id", "reason"],
    "escalation":       ["case_summary", "needs_action", "urgency"],
}
BRANCHES = list(BRANCH_SCHEMA)

TOOL_ROLEPLAY_PROMPTS = {
    "inquire_with_airport": "You are an airport baggage services response. Respond: 'airport=<code>; response=<one-line>'.",
    "send_care_voucher": "You are the voucher service. Respond: 'voucher_id=V<6-digits>; passenger_id=<id>; amount_usd=<n>; status=issued'.",
    "organize_delivery": "You are the courier booking system. Respond: 'delivery_id=D<6-digits>; baggage_id=<id>; address=<addr>; scheduled_eta_hours=8; status=scheduled'.",
    "inform_passenger": "You are the notification system. Respond: 'notification_id=N<6-digits>; passenger_id=<id>; status=sent'.",
    "request_info_from_passenger": "Simulate a terse passenger reply. Respond: 'passenger_reply=<one sentence>'.",
}

def _openai():
    from openai import OpenAI
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def _strip_fences(raw: str) -> str:
    raw = raw.strip().strip("`")
    return raw[4:].strip() if raw.startswith("json") else raw

def _query_baggage_db(baggage_id: str, db_path: str) -> str:
    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT baggage_id, status, last_scanned_at, hours_since_scan, eta_hours, delivery_address "
        "FROM baggage_tracking WHERE baggage_id = ?",
        [baggage_id.strip().strip('"').strip("'")],
    ).fetchall()
    con.close()
    if not rows:
        return f"baggage_id={baggage_id}; status=unknown_baggage_id"
    r = rows[0]
    return (f"baggage_id={r[0]}; status={r[1]}; last_scanned_at={r[2]}; "
            f"hours_since_scan={r[3]}; eta_hours={r[4]}; delivery_address={r[5]}")

def _call_tool(name: str, args: str, db_path: str) -> str:
    if name == "query_baggage_db":
        try:
            parsed = json.loads(args) if args.strip().startswith("{") else None
            baggage_id = parsed.get("baggage_id") if isinstance(parsed, dict) else args
        except Exception:
            baggage_id = args
        return _query_baggage_db(str(baggage_id), db_path)
    if name in TOOL_ROLEPLAY_PROMPTS:
        resp = _openai().chat.completions.create(
            model=MODEL, temperature=0.0, max_tokens=200,
            messages=[
                {"role": "system", "content": TOOL_ROLEPLAY_PROMPTS[name]},
                {"role": "user", "content": args},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    raise ValueError(f"unknown tool: {name}")


# ─── The 3 callables DynamicFanoutJobComponent wires up ────────────────────

def discover_cases(*, reports_csv: str) -> List[Dict[str, Any]]:
    """Discovery: return the list of items to fan out over."""
    df = pd.read_csv(reports_csv)
    return df.to_dict(orient="records")


def triage_one_case(case: Dict[str, Any], *, baggage_db_path: str) -> Dict[str, Any]:
    """Process: full ReAct loop + classifier for ONE case. Called via .map()."""
    case_id = case["case_id"]
    task = (
        f"You are a baggage-loss triage agent. Resolve case {case_id} for {case['passenger']} "
        f"on flight {case['flight']}, baggage_id {case['baggage_id']}. Description: {case['description']}\n\n"
        "SOP: query DB; if located+address → organize_delivery + inform_passenger; "
        "if not_found → inquire_with_airport; then send_care_voucher + request_info. "
        "Branches: delivery_request, voucher_issued, escalation."
    )
    client = _openai()
    trajectory = []
    for i in range(1, MAX_ITER + 1):
        prior_txt = "\n".join(
            f"{t['iteration']}: {t.get('tool')}({t.get('args')}) → {str(t.get('tool_output',''))[:200]}"
            for t in trajectory
        ) or "(no prior)"
        resp = client.chat.completions.create(
            model=MODEL, temperature=0.0, max_tokens=300,
            messages=[
                {"role": "system", "content": "You are a strict tool-picking planner. Reply ONLY with JSON."},
                {"role": "user", "content": (
                    f"Task:\n{task}\n\nPrior:\n{prior_txt}\n\n"
                    "Tools: query_baggage_db, inquire_with_airport, request_info_from_passenger, "
                    "send_care_voucher, organize_delivery, inform_passenger.\n\n"
                    'Reply: {"done": bool, "tool": "<name>|null", "args": "<string>|null", "reasoning": "<one clause>"}'
                )},
            ],
        )
        plan = json.loads(_strip_fences(resp.choices[0].message.content or ""))
        if plan.get("done"): break
        tool_output = _call_tool(plan["tool"], str(plan.get("args") or ""), baggage_db_path)
        trajectory.append({"iteration": i, "tool": plan["tool"], "args": plan.get("args"),
                           "reasoning": plan.get("reasoning",""), "tool_output": tool_output})

    traj_txt = "\n".join(f"{t['iteration']}: {t.get('tool')}({t.get('args')}) → "
                         f"{str(t.get('tool_output',''))[:250]}" for t in trajectory) or "(no tools)"
    schema_txt = "\n".join(f"  {b}: fields {BRANCH_SCHEMA[b]}" for b in BRANCHES)
    resp = client.chat.completions.create(
        model=MODEL, temperature=0.0, max_tokens=500,
        messages=[
            {"role": "system", "content": (
                "Classify + extract payloads. Reply ONLY JSON: "
                '{"emit": {"<branch>": {<fields>}, ...}, "summary": "<line>"}.'
            )},
            {"role": "user", "content": f"Trajectory:\n{traj_txt}\n\nBranches:\n{schema_txt}"},
        ],
    )
    cls = json.loads(_strip_fences(resp.choices[0].message.content or ""))
    payloads = {k: (v if isinstance(v, dict) else {}) for k, v in cls.get("emit", {}).items() if k in BRANCHES}
    return {
        "case_id": case_id, "passenger": case["passenger"],
        "picked": list(payloads), "emit_payloads": payloads,
        "summary": cls.get("summary",""), "n_iterations": len(trajectory),
    }


def collect_batch(results: List[Dict[str, Any]], *, audit_db_path: str) -> Dict[str, Any]:
    """Collect: aggregate + audit the whole batch."""
    from datetime import datetime, timezone
    rows = []
    for r in results:
        for branch, payload in (r.get("emit_payloads") or {}).items():
            rows.append({
                "case_id": r["case_id"], "passenger": r["passenger"],
                "branch": branch, "summary": r["summary"],
                "payload_json": json.dumps(payload),
                "audited_at": datetime.now(timezone.utc).isoformat(),
            })
    if not rows:
        return {"cases": len(results), "audited": 0}
    df = pd.DataFrame(rows)
    Path(audit_db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(audit_db_path)
    con.register("df", df)
    con.execute("CREATE TABLE IF NOT EXISTS daily_audit AS SELECT * FROM df WHERE 1=0")
    con.execute("INSERT INTO daily_audit SELECT * FROM df")
    con.close()
    return {"cases": len(results), "audited": len(df)}
PY

# --- YAML: DynamicFanoutAssetComponent — asset-lineage version -----------
# (Was DynamicFanoutJobComponent, now upgraded to the asset variant so the
# fan-out lives in the asset catalog with proper lineage.)
DEFS="src/$PKG/defs"
mkdir -p "$DEFS/daily_triage_batch"

cat > "$DEFS/daily_triage_batch/defs.yaml" <<YAML
type: dagster_community_components.DynamicFanoutAssetComponent
attributes:
  asset_name: daily_triage_batch
  group_name: batch_router
  description: "Daily batch triage — fan out to N per-case ReAct triages, collect into one DataFrame."

  # 3 callable paths — plain Python functions in helpers.py above.
  discover_callable_path: "${PKG}.helpers:discover_cases"
  discover_kwargs:
    reports_csv: ${PROJECT_ABS}/data/daily_baggage_reports.csv

  process_callable_path: "${PKG}.helpers:triage_one_case"
  process_kwargs:
    baggage_db_path: ${PROJECT_ABS}/data/baggage.duckdb

  collect_callable_path: "${PKG}.helpers:collect_batch_default"

  # Use case_id as the DynamicOutput mapping_key → stable per-item retries.
  mapping_key_field: case_id

  # Per-item retry policy (a bad LLM call on ONE case doesn't kill the batch)
  retry_max_retries: 1
  retry_delay_seconds: 2
  retry_backoff: exponential

  fail_on_empty: true

  kinds: [ai, agent, fanout]
YAML

# collect_batch needs the audit DB path — the DynamicFanoutAssetComponent's
# collect_callable takes only (results,), so we wrap to bind the path.
cat >> "src/$PKG/helpers.py" <<PY


# Wrapper because collect_callable_path only takes (results,) — no kwargs.
# In production you'd pass paths via env vars or use a real Dagster resource;
# here we bind at import time for demo simplicity.
_AUDIT_DB = "${PROJECT_ABS}/audit/daily_audit.duckdb"

def collect_batch_default(results):
    return collect_batch(results, audit_db_path=_AUDIT_DB)
PY

# --- dg check + launch ----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

if [ -z "$OPENAI_API_KEY" ]; then
  echo ""
  echo "! OPENAI_API_KEY not set — skipping job run."
  echo "  Cleanup: rm -rf $PROJECT_ABS"
  exit 0
fi

echo ""
echo ">>> daily_triage_batch — asset materialization (discover → 5x process → collect)"
uv run dg launch --assets daily_triage_batch 2>&1 | tail -3

echo ""
echo ">>> Batch audit contents:"
uv run python - <<PY
import duckdb
try:
    con = duckdb.connect("$PROJECT_ABS/audit/daily_audit.duckdb", read_only=True)
    rows = con.execute("SELECT case_id, passenger, branch, summary FROM daily_audit").fetchall()
    print(f"{len(rows)} row(s):")
    for r in rows:
        print(f"  {r[0]:>3s}  {r[1]:<8s}  {r[2]:<20s}  {str(r[3])[:60]}")
    con.close()
except Exception as e:
    print(f"(audit read failed: {e})")
PY

# --- Dagster+ Serverless prep -------------------------------------------
# Make the project deployable via `dagster-cloud serverless deploy-docker`.
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/lib/serverless_prep.sh | bash

cat <<DONE

✓ agentic_batch_triage demo done.

Component version — DynamicFanoutAssetComponent wraps discover → .map → .collect
around the 3 user callables in helpers.py. YAML declares the wiring; Python
lives in a separate file. Fan-out lives in the ASSET catalog (not a job).

Browse:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # Assets tab → daily_triage_batch → materialize → run view shows:
  #   _discover → 5 x process[d1..d5] → collect

Compare to the plain Python version:
  https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_batch_triage_plain_demo.sh

Cleanup:
  rm -rf $PROJECT_ABS
DONE
