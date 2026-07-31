#!/usr/bin/env bash
# agentic_router_plain_minimal — the SIMPLEST Dagster shape of the router.
# No partitions. No fanout. No ops. Just chained @assets with a Python
# loop inside. This is the shape a Prefect user would recognize
# most naturally — closest 1:1 with Prefect's imperative flow model.
#
# What's intentionally OMITTED (relative to the other agentic_router demos):
#   - No partitions per case → all cases handled in one asset materialization
#   - No DynamicOut fan-out → sequential loop inside the router asset
#   - No graph_asset + ops → just plain @asset chains
#   - No human_approval_gate, no sensor → the router runs to completion
#
# What you still get for free (Dagster over "just Python"):
#   - The asset catalog (each stage is an addressable asset with lineage)
#   - Materialization history (past runs of each asset are queryable)
#   - Downstream fan-out via normal @asset deps (each branch is an asset)
#   - dg dev UI with the asset graph
#   - Same primitives you'd use to add partitions/fanout/gates LATER
#
# Read this side-by-side with the fuller versions to see what each
# feature costs to add and what it buys.

set -eo pipefail

PROJECT_DIR="${1:-agentic-router-plain-minimal-demo}"

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

# --- Seed the DuckDB baggage_tracking table (same as other demos) ----------
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
])
con.close()
print("seeded 3 baggage records")
PY

cat > data/baggage_reports.csv <<'CSV'
case_id,passenger,flight,baggage_id,description
c1,Alice,UA100 ORD→LHR,BAG-001,Checked bag never arrived at LHR arrivals.
c2,Bob,AF200 JFK→CDG,BAG-002,Bag missing 3 days now — no updates from airline.
c3,Carol,DL300 SFO→JFK,BAG-003,Airline confirms bag is at JFK; need it delivered to my home.
CSV

# --- assets.py (in defs/, autoloaded) --------------------------------------
PKG="$(ls src/ | head -1)"
cat > "src/$PKG/defs/assets.py" <<'PY'
"""The MINIMAL agentic router in Dagster.

Bare minimum shape — closest to what a Prefect user would recognize:
  - No partitions
  - No DynamicOut / .map() fan-out
  - No @graph_asset + @op composition
  - No human gate, no sensor
  - Just chained @dg.assets with a Python for-loop inside the router

Compare to the fuller demos in agentic_router.md, agentic_router_plain.md
(Variant A: graph_asset + ops, Variant B: plain @asset with loop, per-case
partitions everywhere) and agentic_batch_triage.md (adds DynamicOut fan-out).

Each of those adds ONE feature on top of this baseline. Read them as a
progression, not alternatives.

Prefect analog:
  Prefect                       Dagster equivalent (here)
  ─────────                     ────────────────────────
  @flow                    ~=   @dg.asset  (chained, unpartitioned)
  @task called in a loop   ~=   Python for-loop inside the compute
  Prefect result store     ~=   Dagster IO manager (default filesystem)
  Flow run history         ~=   Materialization history (per asset)
  Flow parameters          ~=   would be Dagster config/partition — omitted here

What Dagster gives you even in this stripped-down shape:
  - Each stage is an addressable asset with its own materialization history
  - `dg dev` UI graphs the pipeline out of the box
  - Downstream fan-out via normal @asset ins is graph-visible
  - Every primitive here is a stepping stone to partitions/fanout/gates later
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import dagster as dg
import pandas as pd
from dagster import AssetExecutionContext
from dagster_duckdb import DuckDBResource
from openai import OpenAI

# ─── Config ────────────────────────────────────────────────────────────────
PROJECT_ABS = Path(__file__).resolve().parents[3]
BAGGAGE_DB = str(PROJECT_ABS / "data" / "baggage.duckdb")
BAGGAGE_CSV = str(PROJECT_ABS / "data" / "baggage_reports.csv")
AUDIT_DB = str(PROJECT_ABS / "audit" / "case_audit.duckdb")

MODEL = "gpt-4o-mini"
MAX_ITER = 5
BRANCHES = ["delivery_request", "voucher_issued", "escalation"]
BRANCH_SCHEMA: Dict[str, List[str]] = {
    "delivery_request": ["baggage_id", "passenger", "address", "delivery_id", "eta_hours"],
    "voucher_issued":   ["passenger", "amount_usd", "voucher_id", "reason"],
    "escalation":       ["case_summary", "needs_action", "urgency"],
}

# ─── Tools (real SQL for query_baggage_db; LLM roleplay for the rest) ──────
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
    "inquire_with_airport": "Respond: 'airport=<code>; response=<one-line>'.",
    "send_care_voucher": "Respond: 'voucher_id=V<6-digits>; passenger_id=<id>; amount_usd=<n>; status=issued'.",
    "organize_delivery": "Respond: 'delivery_id=D<6-digits>; baggage_id=<id>; address=<addr>; scheduled_eta_hours=8; status=scheduled'.",
    "inform_passenger": "Respond: 'notification_id=N<6-digits>; passenger_id=<id>; status=sent'.",
    "request_info_from_passenger": "Respond: 'passenger_reply=<one sentence>'.",
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
            messages=[{"role": "system", "content": TOOL_ROLEPLAY_PROMPTS[name]},
                      {"role": "user", "content": args}],
        )
        return (resp.choices[0].message.content or "").strip()
    raise ValueError(f"unknown tool: {name}")

def _strip_fences(raw: str) -> str:
    raw = raw.strip().strip("`")
    return raw[4:].strip() if raw.startswith("json") else raw

def _triage_one_case(case: Dict[str, Any], baggage_db: DuckDBResource, log) -> Dict[str, Any]:
    """The ReAct loop + classifier for ONE case — plain Python, no Dagster."""
    case_id = case["case_id"]
    task = (
        f"You are a baggage-loss triage agent. Resolve case {case_id} for {case['passenger']} "
        f"on flight {case['flight']}, baggage_id {case['baggage_id']}. Description: {case['description']}\n\n"
        "SOP: query DB; if located + address known → organize_delivery + inform_passenger → done. "
        "If not_found → inquire_with_airport; then send_care_voucher + request_info → done. "
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
        log.info(f"[{case_id} step {i}] plan: done={plan.get('done')} tool={plan.get('tool')}")
        if plan.get("done"): break
        tool_output = _call_tool(plan["tool"], str(plan.get("args") or ""), baggage_db)
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
    return {"case_id": case_id, "passenger": case["passenger"],
            "picked": list(payloads), "emit_payloads": payloads,
            "summary": cls.get("summary",""), "n_iterations": len(trajectory)}

# ─── The pipeline — 5 chained @assets. No partitions, no fanout. ───────────

@dg.asset(group_name="source", kinds={"csv"})
def baggage_reports() -> pd.DataFrame:
    """Load all cases from CSV — Prefect analog: a task that reads a file."""
    return pd.read_csv(BAGGAGE_CSV)

@dg.asset(
    group_name="router",
    kinds={"ai", "agent"},
    required_resource_keys={"baggage_db"},
    description="ReAct loop + classifier for EVERY case, sequential in a Python for-loop.",
)
def triage_batch(context: AssetExecutionContext, baggage_reports: pd.DataFrame) -> pd.DataFrame:
    """Process every case in a sequential Python for-loop.

    No fan-out, no partitions. Whichever case is first in the CSV runs first;
    the next runs after. If one case takes 30s, the total is N × ~30s.

    This is Prefect's most natural shape: a flow that loops through items.
    Trade-offs vs Dagster's fan-out (agentic_batch_triage.md):
      - Simpler: one asset, one Python loop, no DynamicOut plumbing
      - Slower: sequential vs parallel
      - No per-case retry: if case 3/5 fails, cases 1-2 already ran; on
        retry, all 5 re-run from scratch
    """
    rows = []
    for _, row in baggage_reports.iterrows():
        r = _triage_one_case(row.to_dict(), context.resources.baggage_db, context.log)
        rows.append(r)
    df = pd.DataFrame(rows)
    context.log.info(f"triaged {len(df)} case(s)")
    context.add_output_metadata({
        "n_cases": dg.MetadataValue.int(len(df)),
        "picked_summary": dg.MetadataValue.text(
            ", ".join(f"{r['case_id']}={r['picked']}" for r in rows)
        ),
    })
    return df

# ─── Branch filters — unpartitioned @asset per branch ──────────────────────
def _make_branch_filter(bname: str, schema: List[str]):
    @dg.asset(
        name=bname,
        group_name="branches",
        kinds={"ai", "agent", "branch"},
        description=f"Rows of the batch whose classification picked {bname!r}.",
    )
    def _branch(context: AssetExecutionContext, triage_batch: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for _, r in triage_batch.iterrows():
            payload = (r.get("emit_payloads") or {}).get(bname)
            if payload is None:
                continue
            rows.append({
                "case_id": r["case_id"],
                "passenger": r["passenger"],
                "summary": r["summary"],
                **{f: payload.get(f) for f in schema},
            })
        df = pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["case_id", "passenger", "summary"] + list(schema))
        context.add_output_metadata({
            "n_rows": dg.MetadataValue.int(len(df)),
            "case_ids": dg.MetadataValue.text(", ".join(df["case_id"]) if len(df) else "(none)"),
        })
        return df
    _branch.__name__ = bname
    return _branch

delivery_request = _make_branch_filter("delivery_request", BRANCH_SCHEMA["delivery_request"])
voucher_issued = _make_branch_filter("voucher_issued", BRANCH_SCHEMA["voucher_issued"])
escalation = _make_branch_filter("escalation", BRANCH_SCHEMA["escalation"])

# ─── Sink: append the whole batch to the audit warehouse ────────────────────
@dg.asset(
    group_name="sinks", kinds={"duckdb"},
    description="Append every branch outcome to the legacy audit warehouse.",
)
def case_audit(
    context: AssetExecutionContext,
    delivery_request: pd.DataFrame,
    voucher_issued: pd.DataFrame,
    escalation: pd.DataFrame,
) -> None:
    import duckdb
    frames = [
        delivery_request.assign(branch="delivery_request"),
        voucher_issued.assign(branch="voucher_issued"),
        escalation.assign(branch="escalation"),
    ]
    combined = pd.concat([f for f in frames if len(f) > 0], ignore_index=True) if any(len(f) for f in frames) else pd.DataFrame()
    if combined.empty:
        context.log.info("no rows to audit")
        return
    combined["audited_at"] = datetime.now(timezone.utc).isoformat()
    Path(AUDIT_DB).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(AUDIT_DB)
    con.register("df", combined)
    con.execute("CREATE TABLE IF NOT EXISTS case_audit AS SELECT * FROM df WHERE 1=0")
    con.execute("INSERT INTO case_audit SELECT * FROM df")
    con.close()
    context.log.info(f"appended {len(combined)} audit row(s)")
    context.add_output_metadata({"rows_audited": dg.MetadataValue.int(len(combined))})
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
  exit 0
fi

echo ""
echo ">>> Materialize the whole pipeline (unpartitioned — one shot)"
uv run dg launch --assets 'baggage_reports,triage_batch,delivery_request,voucher_issued,escalation,case_audit' 2>&1 | tail -3

echo ""
echo ">>> Audit contents:"
uv run python - <<PY
import duckdb
con = duckdb.connect("$PROJECT_ABS/audit/case_audit.duckdb", read_only=True)
rows = con.execute("SELECT case_id, passenger, branch, summary FROM case_audit").fetchall()
print(f"{len(rows)} row(s):")
for r in rows:
    print(f"  {str(r[0]):>3s}  {str(r[1]):<10s}  {str(r[2]):<20s}  {str(r[3])[:60]}")
con.close()
PY

cat <<DONE

✓ agentic_router_plain_minimal demo done.

The SIMPLEST Dagster shape of the router: no partitions, no fan-out, no
ops, no gates. Chained @assets with a Python loop inside the router. 3
cases handled in one materialization of triage_batch, then filtered into
3 branch assets, then audited to DuckDB.

Read the setup script's assets.py side-by-side with the fuller versions to
see what each feature costs to add:

  This (minimal) → +DynamicOut fan-out    = agentic_batch_triage_plain
  This (minimal) → +partitions per case   = agentic_router_plain (Variant B)
  This (minimal) → +ops + graph_asset     = agentic_router_plain (Variant A)
  This (minimal) → +human gate + sensor   = agentic_router.md components version

Prefect users: this shape is your natural starting point. A flow with a
loop that calls tasks. Every subsequent Dagster feature (partitions, fanout,
gates, sensors) is an incremental addition on top.

Browse:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000

Cleanup:
  rm -rf $PROJECT_ABS
DONE
