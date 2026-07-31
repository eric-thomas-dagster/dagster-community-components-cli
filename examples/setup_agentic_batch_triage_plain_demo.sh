#!/usr/bin/env bash
# agentic_batch_triage_plain — daily batch triage via dynamic fan-out.
#
# Different shape from agentic_router*:
#   - Per-case partition version: 1 case = 1 partition = 1 run. Fine-grained
#     retry per case, per-case lineage.
#   - THIS version (fan-out):     N cases per day are processed IN ONE RUN via
#     Dagster's DynamicOut. The router @graph_asset reads all cases for the
#     day, emits a DynamicOutput per case, .map()'s each to a per-case ReAct
#     triage op, then .collect()'s into a batch summary.
#
# Why an SE audience wants to see this:
#   - It's Dagster's direct answer to Prefect's `task.map()` shape.
#   - It shows RUNTIME-DECIDED parallelism (N ops spun up based on today's
#     ticket count) inside a single graph execution.
#   - Fewer runs when volume is high — one job per batch, not N per batch.

set -eo pipefail

PROJECT_DIR="${1:-agentic-batch-triage-plain-demo}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — router will error at materialize time."
fi

rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

uv add -q pandas openai duckdb dagster-duckdb

export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME" data audit

# --- Seed a small DuckDB baggage table -----------------------------------
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

# One day's worth of reports — 5 cases instead of 3, to make the fanout
# visibly more than 1.
cat > data/daily_baggage_reports.csv <<'CSV'
case_id,passenger,flight,baggage_id,description
d1,Alice,UA100 ORD→LHR,BAG-001,Checked bag never arrived at LHR.
d2,Bob,AF200 JFK→CDG,BAG-002,Bag missing 3 days — no updates.
d3,Carol,DL300 SFO→JFK,BAG-003,Airline confirms bag at JFK; need delivery.
d4,Dan,AA150 SFO→ORD,BAG-004,Bag not on carousel at ORD.
d5,Eve,BA200 JFK→LHR,BAG-005,Bag missing 4 days — filed police report.
CSV

# --- assets.py in defs/ ---------------------------------------------------
PKG="$(ls src/ | head -1)"
cat > "src/$PKG/defs/assets.py" <<'PY'
"""Daily batch triage via DynamicOut fan-out.

  daily_baggage_reports  (unpartitioned DataFrame with N cases)
        │
        ▼  @graph_asset:  daily_triage_batch
        │    _fan_out_cases (@op, DynamicOut)  — emits N DynamicOutputs
        │       │
        │       ├── _triage_one_case[d1]  — full ReAct loop for d1
        │       ├── _triage_one_case[d2]  — full ReAct loop for d2   } .map()
        │       ├── _triage_one_case[d3]  — …                        }
        │       ├── _triage_one_case[d4]  — …                        }
        │       └── _triage_one_case[d5]  — full ReAct loop for d5   }
        │
        │    _collect_batch(triaged.collect())  — DataFrame of all outcomes
        │
        ▼  daily_batch_audit (@asset)  — writes the batch DataFrame to DuckDB.

Compare to the per-case partitioning shape in agentic_router.md:
  - Per-case: N cases = N runs. Retry a single case cheaply. Per-case lineage.
  - Fan-out (this): N cases = 1 run. Retry the whole batch OR a single item
    via Dagster's op-level retry_policy on _triage_one_case.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import dagster as dg
import pandas as pd
from dagster import AssetExecutionContext, OpExecutionContext
from dagster_duckdb import DuckDBResource
from openai import OpenAI

PROJECT_ABS = Path(__file__).resolve().parents[3]
BAGGAGE_DB = str(PROJECT_ABS / "data" / "baggage.duckdb")
BAGGAGE_CSV = str(PROJECT_ABS / "data" / "daily_baggage_reports.csv")
AUDIT_DB = str(PROJECT_ABS / "audit" / "daily_audit.duckdb")

MODEL = "gpt-4o-mini"
MAX_ITER = 5
BRANCHES = ["delivery_request", "voucher_issued", "escalation"]
BRANCH_SCHEMA: Dict[str, List[str]] = {
    "delivery_request": ["baggage_id", "passenger", "address", "delivery_id", "eta_hours"],
    "voucher_issued": ["passenger", "amount_usd", "voucher_id", "reason"],
    "escalation": ["case_summary", "needs_action", "urgency"],
}

# ─── Tools (same shape as the per-case demo) ───────────────────────────────
def _llm() -> OpenAI:
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def _query_baggage_db(baggage_id: str, resource: DuckDBResource) -> str:
    with resource.get_connection() as con:
        rows = con.execute(
            "SELECT baggage_id, status, last_scanned_at, hours_since_scan, eta_hours, delivery_address "
            "FROM baggage_tracking WHERE baggage_id = ?",
            [baggage_id.strip().strip('"').strip("'")],
        ).fetchall()
    if not rows:
        return f"baggage_id={baggage_id}; status=unknown_baggage_id"
    r = rows[0]
    return (f"baggage_id={r[0]}; status={r[1]}; last_scanned_at={r[2]}; "
            f"hours_since_scan={r[3]}; eta_hours={r[4]}; delivery_address={r[5]}")

TOOL_ROLEPLAY_PROMPTS = {
    "inquire_with_airport": "You are an airport baggage services response. Respond: 'airport=<code>; response=<one-line>'. Records: ORD: some bags found. CDG: not found. LHR: no records. JFK: in delivery queue. SFO: transferred.",
    "send_care_voucher": "You are the voucher service. Respond: 'voucher_id=V<6-digits>; passenger_id=<id>; amount_usd=<n>; status=issued'.",
    "organize_delivery": "You are the courier booking system. Respond: 'delivery_id=D<6-digits>; baggage_id=<id>; address=<addr>; scheduled_eta_hours=8; status=scheduled'.",
    "inform_passenger": "You are the notification system. Respond: 'notification_id=N<6-digits>; passenger_id=<id>; status=sent'.",
    "request_info_from_passenger": "Simulate a terse passenger reply. Respond: 'passenger_reply=<one sentence>'.",
}

def _call_tool(name: str, args: str, baggage_db: DuckDBResource) -> str:
    if name == "query_baggage_db":
        try:
            parsed = json.loads(args) if args.strip().startswith("{") else None
            baggage_id = parsed.get("baggage_id") if isinstance(parsed, dict) else args
        except Exception:
            baggage_id = args
        return _query_baggage_db(str(baggage_id), baggage_db)
    if name in TOOL_ROLEPLAY_PROMPTS:
        resp = _llm().chat.completions.create(
            model=MODEL, temperature=0.0, max_tokens=200,
            messages=[
                {"role": "system", "content": TOOL_ROLEPLAY_PROMPTS[name]},
                {"role": "user", "content": args},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    raise ValueError(f"unknown tool: {name}")

def _strip_fences(raw: str) -> str:
    raw = raw.strip().strip("`")
    return raw[4:].strip() if raw.startswith("json") else raw

# ─── Fan-out op: N cases → N DynamicOutputs ────────────────────────────────
@dg.op(
    name="fan_out_cases",
    out=dg.DynamicOut(dict),
    description="Emit one DynamicOutput per case for parallel per-case triage.",
)
def _fan_out_cases(context: OpExecutionContext, reports: pd.DataFrame):
    for _, row in reports.iterrows():
        case = row.to_dict()
        context.log.info(f"fanning out case {case['case_id']}: {case['passenger']}")
        # mapping_key = case_id so per-case retries are addressable + stable
        yield dg.DynamicOutput(case, mapping_key=str(case["case_id"]))

# ─── Per-case triage op — one op invocation per case (via .map) ────────────
@dg.op(
    name="triage_one_case",
    required_resource_keys={"baggage_db"},
    retry_policy=dg.RetryPolicy(max_retries=1, delay=2),  # per-case retry
    description="Run the ReAct loop + classifier for a single case. Called via .map() from fan_out_cases.",
)
def _triage_one_case(context: OpExecutionContext, case: dict) -> dict:
    case_id = case["case_id"]
    task = (
        f"You are a baggage-loss triage agent. Resolve case {case_id} for {case['passenger']} "
        f"on flight {case['flight']}, baggage_id {case['baggage_id']}. Description: {case['description']}\n\n"
        "SOP: query DB; if located+address → organize_delivery + inform_passenger; "
        "if not_found → inquire_with_airport; then send_care_voucher + request_info. "
        "Branches: delivery_request, voucher_issued, escalation."
    )
    client = _llm()
    trajectory: List[Dict[str, Any]] = []
    for i in range(1, MAX_ITER + 1):
        prior_txt = "\n".join(f"{t['iteration']}: {t.get('tool')}({t.get('args')}) → "
                              f"{str(t.get('tool_output',''))[:200]}" for t in trajectory) or "(no prior)"
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
        context.log.info(f"[{case_id} step {i}] plan: done={plan.get('done')} tool={plan.get('tool')}")
        if plan.get("done"): break
        tool_output = _call_tool(plan["tool"], str(plan.get("args") or ""), context.resources.baggage_db)
        trajectory.append({"iteration": i, "tool": plan["tool"], "args": plan.get("args"),
                           "reasoning": plan.get("reasoning", ""), "tool_output": tool_output})

    # Classifier
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
    context.log.info(f"[{case_id}] picked: {list(payloads)} — {cls.get('summary','')}")
    return {
        "case_id": case_id,
        "passenger": case["passenger"],
        "picked": list(payloads),
        "emit_payloads": payloads,
        "summary": cls.get("summary", ""),
        "n_iterations": len(trajectory),
    }

# ─── Collect op: assemble the batch DataFrame ──────────────────────────────
@dg.op(
    name="collect_batch",
    description="Assemble all per-case triage results into a batch DataFrame.",
)
def _collect_batch(context: OpExecutionContext, triaged: list) -> pd.DataFrame:
    rows = []
    for r in triaged:
        for branch, payload in (r.get("emit_payloads") or {}).items():
            rows.append({
                "case_id": r["case_id"],
                "passenger": r["passenger"],
                "branch": branch,
                "summary": r["summary"],
                "payload_json": json.dumps(payload),
            })
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["case_id","passenger","branch","summary","payload_json"])
    context.log.info(f"collected {len(df)} branch outcome(s) across {df['case_id'].nunique() if len(df) else 0} case(s)")
    return df

# ─── Source: today's baggage reports ───────────────────────────────────────
@dg.asset(
    key=dg.AssetKey("daily_baggage_reports"),
    group_name="source",
    kinds={"csv"},
)
def daily_baggage_reports() -> pd.DataFrame:
    return pd.read_csv(BAGGAGE_CSV)

# ─── The fanout: @graph_asset with DynamicOut → .map → .collect ────────────
@dg.graph_asset(
    name="daily_triage_batch",
    ins={"reports": dg.AssetIn(key=dg.AssetKey("daily_baggage_reports"))},
    group_name="batch_router",
    kinds={"ai", "agent", "fanout"},
    description="Daily batch triage — fan out to N per-case triage ops in one run.",
)
def daily_triage_batch(reports):
    cases = _fan_out_cases(reports)
    triaged = cases.map(_triage_one_case)
    return _collect_batch(triaged.collect())

# ─── Downstream: audit the whole batch to a legacy warehouse ───────────────
@dg.asset(
    key=dg.AssetKey("daily_batch_audit"),
    group_name="sinks",
    kinds={"duckdb"},
    ins={"batch": dg.AssetIn(key=dg.AssetKey("daily_triage_batch"))},
    description="Append the day's triage outcomes to the legacy audit warehouse.",
)
def daily_batch_audit(context: AssetExecutionContext, batch: pd.DataFrame) -> None:
    import duckdb
    Path(AUDIT_DB).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(AUDIT_DB)
    df = batch.copy()
    df["audited_at"] = datetime.now(timezone.utc).isoformat()
    con.register("df", df)
    con.execute("CREATE TABLE IF NOT EXISTS daily_audit AS SELECT * FROM df WHERE 1=0")
    con.execute("INSERT INTO daily_audit SELECT * FROM df")
    con.close()
    context.log.info(f"appended {len(df)} row(s) to daily_audit")
    context.add_output_metadata({
        "rows_audited": dg.MetadataValue.int(len(df)),
        "cases": dg.MetadataValue.text(", ".join(sorted(df["case_id"].unique()))) if len(df) else dg.MetadataValue.text("(none)"),
    })
PY

cat > "src/$PKG/definitions.py" <<'PY'
from pathlib import Path
from dagster import Definitions, definitions, load_from_defs_folder
from dagster_duckdb import DuckDBResource
from .defs import assets as A

@definitions
def defs():
    return Definitions.merge(
        load_from_defs_folder(path_within_project=Path(__file__).parent),
        Definitions(resources={"baggage_db": DuckDBResource(database=A.BAGGAGE_DB)}),
    )
PY

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
echo ">>> daily_triage_batch — ONE run, N cases fan out inside"
uv run dg launch --assets daily_triage_batch 2>&1 | tail -3

echo ""
echo ">>> daily_batch_audit — append batch to DuckDB"
uv run dg launch --assets daily_batch_audit 2>&1 | tail -3

echo ""
echo ">>> Batch audit contents:"
uv run python - <<PY
import duckdb
con = duckdb.connect("$PROJECT_ABS/audit/daily_audit.duckdb", read_only=True)
rows = con.execute("SELECT case_id, passenger, branch, summary FROM daily_audit").fetchall()
for r in rows:
    print(f"  {r[0]:>3s}  {r[1]:<8s}  {r[2]:<20s}  {str(r[3])[:60]}")
con.close()
PY

cat <<DONE

✓ agentic_batch_triage_plain demo done.

What just happened:
  - 5 baggage reports for today (d1..d5) loaded from CSV
  - ONE run of daily_triage_batch — fanned out to 5 per-case triage ops IN
    THAT SAME RUN via DynamicOut. Each op ran its own ReAct loop over the
    same tool set.
  - Results .collect()'d into a 5-row DataFrame
  - daily_batch_audit appended to the legacy DuckDB warehouse.

Browse:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # Click daily_triage_batch's most recent run — you'll see fan_out_cases,
  # then 5 per-case triage_one_case[d1..d5] boxes running in parallel, then
  # collect_batch. Classic DynamicOut fanout.

Compare to the per-case partition demo (agentic_router.md):
  - Per-case:  5 runs, per-case retry/lineage, catalog carries partition-per-case
  - This:      1 run, per-case op retry via retry_policy, catalog has 1 batch asset

Cleanup:
  rm -rf $PROJECT_ABS
DONE
