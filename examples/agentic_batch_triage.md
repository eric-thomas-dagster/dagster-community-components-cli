# Agentic Batch Triage — Dynamic Fan-Out (Prefect `task.map()` in Dagster)

The **batch shape** of the agentic router: N cases per day processed in ONE run via Dagster's `DynamicOut`. Direct analog to Prefect's `task.map()`.

Different tradeoff from the per-case partition version in [agentic_router.md](agentic_router.md):

| Shape | Runs per N cases | Per-case retry | Per-case lineage | Best when |
|---|---|---|---|---|
| **Per-case partitions** (agentic_router.md) | N runs | ✓ each case retryable independently | ✓ each case is a Dagster asset with its own history | Low volume, per-case addressability matters, cases are named/tracked business entities |
| **Batch fan-out** (this walkthrough) | 1 run, N ops inside | ✓ via `retry_policy` on the process op | ✗ per-case rows in one batch DataFrame (no per-case asset in the catalog) | High volume, cases are transient (today's tickets), batch is the addressable unit |

The shape maps 1:1 to what an SE moving from Prefect will recognize:

- Prefect: `flow` runs, invokes `task.map(items)`, each item becomes a parallel task run inside the flow.
- Dagster: `@dg.op(out=DynamicOut())` yields N items, `.map(next_op)` runs `next_op` per item in parallel, `.collect()` gathers results. Wrapped in a `@dg.job` or a `@dg.graph_asset`.

## Two versions of the same story

### Variant A — plain Python (`@graph_asset` with DynamicOut ops)

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_batch_triage_plain_demo.sh \
  -o setup_agentic_batch_triage_plain_demo.sh
bash setup_agentic_batch_triage_plain_demo.sh
```

Everything in `defs/assets.py`:

```python
@dg.op(out=dg.DynamicOut(dict))
def _fan_out_cases(context, reports: pd.DataFrame):
    for _, row in reports.iterrows():
        yield dg.DynamicOutput(row.to_dict(), mapping_key=str(row["case_id"]))

@dg.op(
    required_resource_keys={"baggage_db"},
    retry_policy=dg.RetryPolicy(max_retries=1, delay=2),
)
def _triage_one_case(context, case: dict) -> dict:
    # ... full ReAct loop for this case
    # ... classifier
    return {"case_id": ..., "picked": [...], "emit_payloads": {...}, ...}

@dg.op
def _collect_batch(context, triaged: list) -> pd.DataFrame:
    # aggregate all per-case results into one DataFrame
    ...

@dg.graph_asset(
    name="daily_triage_batch",
    ins={"reports": dg.AssetIn(key=dg.AssetKey("daily_baggage_reports"))},
)
def daily_triage_batch(reports):
    cases = _fan_out_cases(reports)
    triaged = cases.map(_triage_one_case)
    return _collect_batch(triaged.collect())
```

Emits ONE asset (`daily_triage_batch`) whose materialization is a DataFrame of the day's N outcomes. Downstream `daily_batch_audit` writes to DuckDB.

Run view: `_fan_out_cases → 5x _triage_one_case[d1..d5] → _collect_batch → daily_triage_batch`.

### Variant B — components (`DynamicFanoutJobComponent`)

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_batch_triage_demo.sh \
  -o setup_agentic_batch_triage_demo.sh
bash setup_agentic_batch_triage_demo.sh
```

Uses [`DynamicFanoutJobComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/dynamic_fanout_job) — a generic wrapper around the fan-out pattern. User writes 3 callables in `helpers.py`; YAML wires them into a job:

```yaml
# defs/daily_triage_job/defs.yaml
type: dagster_community_components.DynamicFanoutJobComponent
attributes:
  job_name: daily_triage_batch_job

  discover_callable_path: "agentic_batch_triage_demo.helpers:discover_cases"
  discover_kwargs:
    reports_csv: /path/to/daily_baggage_reports.csv

  process_callable_path: "agentic_batch_triage_demo.helpers:triage_one_case"
  process_kwargs:
    baggage_db_path: /path/to/baggage.duckdb

  collect_callable_path: "agentic_batch_triage_demo.helpers:collect_batch_default"

  mapping_key_field: case_id       # DynamicOutput mapping_key per item
  retry_max_retries: 1             # per-item retry
  retry_delay_seconds: 2
  fail_on_empty: true
```

`helpers.py` (~150 lines): three plain Python functions — `discover_cases(reports_csv)`, `triage_one_case(case, baggage_db_path)`, `collect_batch(results, audit_db_path)`. The component wires them into ops + a `@dg.job`.

**What comes out**: a JOB (not an asset). Runs on demand or on schedule. In the UI you'll see the run graph `_discover → 5x _process[d1..d5] → _collect`, same as the plain-Python version — just no `@graph_asset` wrapping it so nothing shows in the Assets tab.

**Key note**: `DynamicFanoutJobComponent` produces a **job**, not an asset. This is Dagster looking most Prefect-like — a task-oriented, run-triggered flow. Assets are still the primary Dagster mental model; jobs live alongside for when the run itself IS the deliverable (batch processing, ETL where the output is a side-effect write).

## What both versions actually do

Both scripts seed a DuckDB `baggage_tracking` table with 5 records and a `daily_baggage_reports.csv` with 5 cases (d1..d5). One run of the fan-out:

1. Discover / read: pull the 5 cases into memory.
2. Fan out: emit 5 `DynamicOutput`s, mapping_key = each case's `case_id`.
3. Process (in parallel per the executor's slot count): each case runs its own ReAct loop — real SQL against DuckDB for `query_baggage_db`, LLM roleplay for the other tools. Classifier at the end emits per-branch payloads.
4. Collect: gather all 5 results, flatten into one DataFrame (one row per case-branch combo), append to the audit warehouse.

Example audit output (LLM decisions vary run-to-run):

```
d1  Alice   delivery_request  Delivery organized for BAG-001 to 12 Baker St, London
d2  Bob     voucher_issued    BAG-002 not found; care voucher issued to Bob
d2  Bob     escalation        BAG-002 not found; care voucher issued to Bob
d3  Carol   delivery_request  BAG-003 awaiting delivery to 123 Main St, Brooklyn
d4  Dan     delivery_request  BAG-004 in transit to ORD, delivery scheduled
d5  Eve     voucher_issued    BAG-005 not found; care voucher issued to Eve
d5  Eve     escalation        BAG-005 not found; case needs review
```

## Per-case partitions vs batch fan-out — how to choose

**Reach for per-case partitions (`agentic_router.md`) when:**
- Cases are named, tracked business entities you want in the asset catalog (customer_id, tenant_id, ticket_id that stays with you for months)
- You want to be able to re-run a single case months later and see its full lineage
- You want per-case gating: some cases need a human, others don't — partitions map cleanly to that
- Volume is low-to-medium (dozens to low hundreds per day) — one run per case is fine
- The audit story per case matters more than throughput

**Reach for batch fan-out (this walkthrough) when:**
- Cases are transient — today's tickets, this hour's transactions, the current queue
- Volume is high (hundreds to thousands per batch) — you don't want N runs, you want one run with N parallel ops
- The batch itself is the addressable unit ("today's triage") not the individual cases
- You want Prefect-style `task.map` semantics that SEs will recognize

**Common hybrid**: batch fan-out for the *processing*, per-case dynamic partitions for the *escalation* subset. Cases that hit the escalation branch inside a batch run register a per-case partition on a downstream `escalation` asset — now escalations get the per-case lineage story while the routine 80% stays as a batch.

## Where the ReAct loop lives — chain of ops vs one op with a Python loop

Both variants above have the ReAct loop **inside a single `@dg.op`** (`_triage_one_case`) with a Python `for` loop. That's the natural fit when the parallelism is at the CASE level (fan out across cases) — you don't need parallelism per ReAct iteration within a case.

If you did want per-iteration parallelism (say, 3 tools called in parallel at one step), you'd nest another `DynamicOut` inside the case op. Rare in practice — LLM decides tools sequentially anyway.

For visibility, the choice is the same as [`agentic_router_plain.md`](agentic_router_plain.md)'s Variant A vs B:
- **One op with a loop**: iterations show in the op's logs. Simpler.
- **Chain of ops per iteration**: each ReAct step is its own op box in the run view. More graph noise.

For batch fan-out specifically, one op with a loop per case is the honest default — the interesting parallelism is across cases, not within a case's iterations.

## Files

- Plain: [setup_agentic_batch_triage_plain_demo.sh](setup_agentic_batch_triage_plain_demo.sh) + generated `defs/assets.py`
- Components: [setup_agentic_batch_triage_demo.sh](setup_agentic_batch_triage_demo.sh) + generated `defs/daily_triage_job/defs.yaml` + `helpers.py`

Both unlisted from the main examples index — reach these directly from this walkthrough or from the related [`agentic_router.md`](agentic_router.md).
