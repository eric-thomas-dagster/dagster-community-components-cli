#!/usr/bin/env bash
# agentic_router_plain_simple — same asset graph shape as the plain demo,
# but the router is a plain @asset with the ReAct loop as an in-body
# `for` loop. No @op, no @graph_asset. Trade the per-step op boxes in the
# run view for ~100 lines less code + dynamic iteration count + no
# inter-step IO manager serialization overhead.
#
# When to reach for this shape vs. the graph_asset+ops version:
#   - You don't need per-step visibility in the run UI (loop iterations
#     just show up in the asset's compute logs instead)
#   - You want dynamic iteration count (loop breaks when the planner says
#     done — no forced max_iterations ops declared at parse time)
#   - You want fewer moving parts / less code to maintain

set -eo pipefail

PROJECT_DIR="${1:-agentic-router-plain-simple-demo}"

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
mkdir -p "$DAGSTER_HOME" data approvals courier_bookings compensations audit

# Seed the DuckDB baggage_tracking table (identical to the graph-asset demo).
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
print("seeded data/baggage.duckdb")
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
"""Plain Dagster — SIMPLER shape: router as plain @asset (no ops).

Compare to the graph_asset+ops version at
setup_agentic_router_plain_demo.sh — same asset graph, same UI, same
partitions. The only differences are:
  - Router is a plain @asset with an in-body `for` loop
  - No plan_step_1..N ops declared at parse time
  - Loop breaks dynamically when the planner says done
  - Per-step logs show up in the asset's compute logs, not as op boxes

~100 fewer lines of code."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import dagster as dg
import pandas as pd
from dagster import AssetExecutionContext, SensorEvaluationContext
from dagster_duckdb import DuckDBResource
from openai import OpenAI

# ─── Config ────────────────────────────────────────────────────────────────
PROJECT_ABS = Path(__file__).resolve().parents[3]
BAGGAGE_DB = str(PROJECT_ABS / "data" / "baggage.duckdb")
BAGGAGE_CSV = str(PROJECT_ABS / "data" / "baggage_reports.csv")
APPROVAL_DIR = PROJECT_ABS / "approvals"
COURIER_DIR = PROJECT_ABS / "courier_bookings"
COMP_DIR = PROJECT_ABS / "compensations"
AUDIT_DB = str(PROJECT_ABS / "audit" / "case_audit.duckdb")

MODEL = "gpt-4o-mini"
MAX_ITER = 5
CASES = ["c1", "c2", "c3"]
BRANCHES = ["delivery_request", "voucher_issued", "escalation"]

router_partitions = dg.StaticPartitionsDefinition(CASES)
branch_partitions = {b: dg.DynamicPartitionsDefinition(name=f"{b}_cases") for b in BRANCHES}

BRANCH_SCHEMA: Dict[str, List[str]] = {
    "delivery_request": ["baggage_id", "passenger", "address", "delivery_id", "eta_hours"],
    "voucher_issued": ["passenger", "amount_usd", "voucher_id", "reason"],
    "escalation": ["case_summary", "needs_action", "urgency"],
}

# ─── Tools (identical to the graph-asset demo) ─────────────────────────────
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
    "inquire_with_airport": "You are an airport baggage services response. Respond: 'airport=<code>; response=<one-line>'. Records: ORD: BAG-001 transferred to LHR. CDG: BAG-002 NOT found. LHR: no records. JFK: BAG-003 in delivery queue.",
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

# ─── The router — plain @asset. No ops. In-body ReAct loop. ────────────────
@dg.asset(
    key=dg.AssetKey("baggage_reports"),
    group_name="source",
)
def baggage_reports() -> pd.DataFrame:
    return pd.read_csv(BAGGAGE_CSV)

@dg.asset(
    key=dg.AssetKey("baggage_triage_agent"),
    partitions_def=router_partitions,
    ins={"upstream": dg.AssetIn(key=dg.AssetKey("baggage_reports"))},
    required_resource_keys={"baggage_db"},
    group_name="router",
    kinds={"ai", "agent", "router"},
    description="Router agent — ReAct loop is a plain Python `for` in the compute body.",
)
def baggage_triage_agent(context: AssetExecutionContext, upstream: pd.DataFrame) -> dict:
    case_id = context.partition_key
    row = upstream[upstream["case_id"] == case_id].iloc[0].to_dict()

    task = (
        f"You are a baggage-loss triage agent. Resolve case {case_id} for {row['passenger']} "
        f"on flight {row['flight']}, baggage_id {row['baggage_id']}. Description: {row['description']}\n\n"
        "SOP: 1) Query the DB. 2) If located + address known → organize_delivery + inform_passenger → done. "
        "3) If not_found → inquire_with_airport; then send_care_voucher + request_info → done. "
        "Branches: delivery_request, voucher_issued, escalation."
    )
    client = _llm()

    # ─── ReAct loop as PLAIN PYTHON ────────────────────────────────────────
    trajectory: List[Dict[str, Any]] = []
    for i in range(1, MAX_ITER + 1):
        prior_txt = "\n".join(
            f"Step {t['iteration']}: {t.get('tool')}({t.get('args')}) → {str(t.get('tool_output',''))[:200]}"
            for t in trajectory
        ) or "(no prior work)"
        resp = client.chat.completions.create(
            model=MODEL, temperature=0.0, max_tokens=300,
            messages=[
                {"role": "system", "content": "You are a strict tool-picking planner. Reply ONLY with JSON."},
                {"role": "user", "content": (
                    f"Task:\n{task}\n\nPrior work:\n{prior_txt}\n\n"
                    "Tools: query_baggage_db, inquire_with_airport, request_info_from_passenger, "
                    "send_care_voucher, organize_delivery, inform_passenger.\n\n"
                    'Reply: {"done": bool, "tool": "<name>|null", "args": "<string>|null", "reasoning": "<one clause>"}'
                )},
            ],
        )
        plan = json.loads(_strip_fences(resp.choices[0].message.content or ""))
        context.log.info(f"[step {i}] plan: done={plan.get('done')} tool={plan.get('tool')} "
                         f"args={str(plan.get('args'))[:80]} reasoning={plan.get('reasoning','')[:120]}")
        if plan.get("done"):
            break
        tool_name = plan["tool"]
        args_str = str(plan.get("args") or "")
        tool_output = _call_tool(tool_name, args_str, context.resources.baggage_db)
        context.log.info(f"[step {i}] tool_output: {tool_output[:200]}")
        trajectory.append({
            "iteration": i, "done": False, "tool": tool_name,
            "args": args_str, "reasoning": plan.get("reasoning", ""),
            "tool_output": tool_output,
        })

    # ─── Classifier — one more LLM call, in-line ────────────────────────────
    traj_txt = "\n".join(
        f"{t['iteration']}: {t.get('tool')}({t.get('args')}) → {str(t.get('tool_output',''))[:250]}"
        for t in trajectory
    ) or "(no tools)"
    schema_txt = "\n".join(f"  {b}: fields {BRANCH_SCHEMA[b]}" for b in BRANCHES)
    resp = client.chat.completions.create(
        model=MODEL, temperature=0.0, max_tokens=500,
        messages=[
            {"role": "system", "content": (
                "Classify the trajectory into which branches to emit AND extract branch payloads. "
                'Reply ONLY with JSON: {"emit": {"<branch>": {<fields>}, ...}, "summary": "<line>"}. '
                "Fill EVERY field for each picked branch from the trajectory. Omit branches that don't apply."
            )},
            {"role": "user", "content": f"Trajectory:\n{traj_txt}\n\nBranches:\n{schema_txt}"},
        ],
    )
    cls = json.loads(_strip_fences(resp.choices[0].message.content or ""))
    payloads = {k: (v if isinstance(v, dict) else {}) for k, v in cls.get("emit", {}).items() if k in BRANCHES}
    picked = list(payloads)
    summary = str(cls.get("summary", ""))
    context.log.info(f"classifier picked: {picked} — {summary}")

    for branch in picked:
        context.instance.add_dynamic_partitions(f"{branch}_cases", [case_id])
        context.log.info(f"registered case {case_id!r} on {branch}_cases; payload={payloads[branch]}")

    context.add_output_metadata({
        "picked": dg.MetadataValue.text(", ".join(picked) or "(none)"),
        "summary": dg.MetadataValue.text(summary),
        "n_iterations": dg.MetadataValue.int(len(trajectory)),
        "trajectory": dg.MetadataValue.md(
            "\n".join(
                f"**Step {t['iteration']}** ({t.get('tool')})\n"
                f"- args: `{t.get('args')}`\n- output: `{str(t.get('tool_output',''))[:400]}`"
                for t in trajectory
            ) or "*(no tools invoked)*"
        ),
    })

    return {
        "picked": picked,
        "emit_payloads": payloads,
        "summary": summary,
        "n_iterations": len(trajectory),
        "trajectory": trajectory,
    }

# ─── Branch assets — same shape as the ops-based demo ──────────────────────
def _make_branch_asset(bname: str, schema: List[str]):
    @dg.asset(
        key=dg.AssetKey(bname),
        group_name="router",
        kinds={"ai", "agent", "branch"},
        partitions_def=branch_partitions[bname],
        deps=[dg.AssetDep(asset=dg.AssetKey("baggage_triage_agent"),
                          partition_mapping=dg.AllPartitionMapping())],
    )
    def _branch(context: AssetExecutionContext) -> pd.DataFrame:
        case_id = context.partition_key
        router_val = context.op_execution_context.load_asset_value(
            dg.AssetKey("baggage_triage_agent"), partition_key=case_id)
        payload = (router_val.get("emit_payloads") or {}).get(bname, {}) or {}
        return pd.DataFrame([{"case_id": case_id, **{f: payload.get(f) for f in schema}}])
    _branch.__name__ = bname
    return _branch

delivery_request = _make_branch_asset("delivery_request", BRANCH_SCHEMA["delivery_request"])
voucher_issued = _make_branch_asset("voucher_issued", BRANCH_SCHEMA["voucher_issued"])
escalation = _make_branch_asset("escalation", BRANCH_SCHEMA["escalation"])

# ─── Downstream sinks — identical to the ops demo ──────────────────────────
def _csv_sink(name: str, upstream: str, out_dir: Path, pdef):
    @dg.asset(
        key=dg.AssetKey(name),
        group_name="sinks",
        kinds={"csv"},
        partitions_def=pdef,
        automation_condition=dg.AutomationCondition.eager(),
        ins={"upstream": dg.AssetIn(key=dg.AssetKey(upstream))},
    )
    def _sink(context: AssetExecutionContext, upstream: pd.DataFrame) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        upstream.to_csv(out_dir / f"{name}_{context.partition_key}.csv", index=False)
    _sink.__name__ = name
    return _sink

courier_booked = _csv_sink("courier_booked", "delivery_request", COURIER_DIR, branch_partitions["delivery_request"])
compensation_paid = _csv_sink("compensation_paid", "voucher_issued", COMP_DIR, branch_partitions["voucher_issued"])

# ─── Human gate — identical to the ops demo ────────────────────────────────
_approval_check = dg.AssetCheckSpec(name="approved", asset=dg.AssetKey("human_review"),
                                    description="WARN if pending, ERROR if rejected.")

@dg.asset(
    key=dg.AssetKey("human_review"),
    group_name="human_in_the_loop",
    kinds={"human", "approval"},
    partitions_def=branch_partitions["escalation"],
    ins={"upstream": dg.AssetIn(key=dg.AssetKey("escalation"))},
    check_specs=[_approval_check],
)
def human_review(context: AssetExecutionContext, upstream: pd.DataFrame):
    case_id = context.partition_key
    token_file = APPROVAL_DIR / f"{case_id}.json"
    empty = upstream.iloc[0:0].copy()
    if not token_file.exists():
        yield dg.Output(empty, metadata={"status": "approval_pending", "token_file": str(token_file)})
        yield dg.AssetCheckResult(check_name="approved", passed=False,
                                  severity=dg.AssetCheckSeverity.WARN,
                                  description=f"approval_pending — no token at {token_file}")
        return
    token = json.loads(token_file.read_text())
    if not token.get("approved"):
        yield dg.Output(empty, metadata={"status": "approval_rejected",
                                         "approver": token.get("approver", "unknown"),
                                         "reason": token.get("reason", "")})
        yield dg.AssetCheckResult(check_name="approved", passed=False,
                                  severity=dg.AssetCheckSeverity.ERROR,
                                  description=f"rejected by {token.get('approver')}: {token.get('reason')}")
        return
    yield dg.Output(upstream, metadata={"status": "approved", "approver": token.get("approver", "unknown"),
                                        "reason": token.get("reason", "")})
    yield dg.AssetCheckResult(check_name="approved", passed=True,
                              severity=dg.AssetCheckSeverity.WARN,
                              description=f"approved by {token.get('approver')}")

@dg.asset(
    key=dg.AssetKey("escalation_audited"),
    group_name="sinks",
    kinds={"duckdb"},
    partitions_def=branch_partitions["escalation"],
    automation_condition=dg.AutomationCondition.eager(),
    ins={"upstream": dg.AssetIn(key=dg.AssetKey("human_review"))},
)
def escalation_audited(context: AssetExecutionContext, upstream: pd.DataFrame) -> None:
    import duckdb
    Path(AUDIT_DB).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(AUDIT_DB)
    df = upstream.copy()
    df["audited_at"] = datetime.now(timezone.utc).isoformat()
    con.register("df", df)
    con.execute("CREATE TABLE IF NOT EXISTS escalation_audit AS SELECT * FROM df WHERE 1=0")
    con.execute("INSERT INTO escalation_audit SELECT * FROM df")
    con.close()

approve_and_process_job = dg.define_asset_job(
    name="approve_and_process_job",
    selection=[dg.AssetKey("human_review"), dg.AssetKey("escalation_audited")],
    description="Materialize the escalation approval gate + audit for one case.",
)

@dg.sensor(name="approval_watcher", minimum_interval_seconds=5, job=approve_and_process_job)
def approval_watcher(context: SensorEvaluationContext):
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    cursor = float(context.cursor) if context.cursor else 0.0
    run_requests, latest = [], cursor
    for name in os.listdir(APPROVAL_DIR):
        if not name.endswith(".json"): continue
        path = APPROVAL_DIR / name
        mtime = path.stat().st_mtime
        if mtime <= cursor: continue
        case_id = Path(name).stem
        run_requests.append(dg.RunRequest(run_key=f"{case_id}-{mtime}", partition_key=case_id))
        latest = max(latest, mtime)
    return dg.SensorResult(run_requests=run_requests, cursor=str(latest)) if run_requests \
        else dg.SensorResult(skip_reason="no new tokens")
PY

# --- definitions.py --------------------------------------------------------
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
echo ">>> baggage_reports"
uv run dg launch --assets baggage_reports 2>&1 | tail -2

echo ""
echo ">>> baggage_triage_agent — per case (ReAct loop in the asset's compute logs)"
for C in c1 c2 c3; do
  echo "    ─── $C ───"
  uv run dg launch --assets baggage_triage_agent --partition "$C" 2>&1 | tail -2
done

echo ""
echo ">>> Branch assets + downstream sinks per case"
for BRANCH in delivery_request voucher_issued escalation; do
  for C in c1 c2 c3; do
    uv run dg launch --assets "$BRANCH" --partition "$C" 2>&1 | tail -1 || true
  done
done
for C in c1 c2 c3; do
  uv run dg launch --assets courier_booked --partition "$C" 2>&1 | tail -1 || true
  uv run dg launch --assets compensation_paid --partition "$C" 2>&1 | tail -1 || true
  uv run dg launch --assets human_review --partition "$C" 2>&1 | tail -1 || true
  uv run dg launch --assets escalation_audited --partition "$C" 2>&1 | tail -1 || true
done

cat <<DONE

✓ agentic_router_plain_simple demo done.

Same asset graph shape as agentic_router_plain.md's Variant A (graph_asset
+ ops), but the router is a plain @asset with the ReAct loop as an in-body
Python for-loop. ~100 fewer lines of code, dynamic iteration count.

Browse:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000

Compare — click into a partition's run:
  - Variant A (ops):   plan_step_1 → plan_step_2 → … → classify_and_register
  - Variant B (this):  one big "baggage_triage_agent" step; iterations show
    up in that step's LOG (not as separate op boxes)

Cleanup:
  rm -rf $PROJECT_ABS
DONE
