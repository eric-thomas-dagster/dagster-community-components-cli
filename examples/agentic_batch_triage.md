# Agentic Batch Triage — Dynamic Fan-Out (Prefect `task.map()` in Dagster)

The **batch shape** of the agentic router: N cases processed in ONE run via Dagster's `DynamicOut`. Direct analog to Prefect's `task.map()`.

Different tradeoff from the per-case partition version in [agentic_router.md](agentic_router.md):

| Shape | Runs per N cases | Per-case retry | Per-case lineage | Best when |
|---|---|---|---|---|
| **Per-case partitions** (agentic_router.md) | N runs | ✓ each case retryable independently | ✓ each case is a Dagster asset with its own history | Low volume, per-case addressability matters, cases are named/tracked business entities |
| **Batch fan-out** (this walkthrough) | 1 run, N ops inside | ✓ via `retry_policy` on the process op | ✗ per-case rows in one batch DataFrame (no per-case asset in the catalog) | High volume, cases are transient (today's tickets), batch is the addressable unit |
| **Mix: partitioned batch + fan-out inside** | 1 run per partition (e.g. per day), N ops per run | ✓ via `retry_policy` on the process op | Per-day lineage on the batch; per-case rows within | Daily/hourly cron-driven batches over N records — the canonical production shape |

Shape maps 1:1 to what an SE moving from Prefect will recognize:
- Prefect: `flow` invokes `task.map(items)`, N parallel task runs inside the flow.
- Dagster: `@dg.op(out=DynamicOut())` yields N items, `.map(next_op)` runs `next_op` per item, `.collect()` gathers results. Wrapped in a `@dg.job` or a `@dg.graph_asset`.

## Three variants — same shape, different authoring surface

### Variant A — plain Python (`@graph_asset` with DynamicOut ops)

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_batch_triage_plain_demo.sh -o setup.sh
bash setup.sh
```

Everything in `defs/assets.py`. Raw `@dg.op(out=DynamicOut())` for discovery, `@dg.op` with a `retry_policy` for the per-case triage, `@dg.op` for collect, wired into a `@dg.graph_asset`. ~200 lines of Python.

```python
@dg.op(out=dg.DynamicOut(dict))
def _fan_out_cases(context, reports: pd.DataFrame):
    for _, row in reports.iterrows():
        yield dg.DynamicOutput(row.to_dict(), mapping_key=str(row["case_id"]))

@dg.op(required_resource_keys={"baggage_db"},
       retry_policy=dg.RetryPolicy(max_retries=1, delay=2))
def _triage_one_case(context, case: dict) -> dict:
    # ... full ReAct loop for this case ...

@dg.graph_asset(name="daily_triage_batch",
                ins={"reports": dg.AssetIn(key=dg.AssetKey("daily_baggage_reports"))})
def daily_triage_batch(reports):
    cases = _fan_out_cases(reports)
    triaged = cases.map(_triage_one_case)
    return _collect_batch(triaged.collect())
```

### Variant B — generic component (`DynamicFanoutAssetComponent`)

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_batch_triage_demo.sh -o setup.sh
bash setup.sh
```

User provides 3 Python callables (`discover_cases` / `triage_one_case` / `collect_batch`) in `helpers.py`. The component ([`dynamic_fanout_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/dynamic_fanout_asset)) wires them into a `@graph_asset` with `DynamicOut`:

```yaml
type: dagster_community_components.DynamicFanoutAssetComponent
attributes:
  asset_name: daily_triage_batch
  discover_callable_path: "agentic_batch_triage_demo.helpers:discover_cases"
  discover_kwargs:
    reports_csv: /path/to/daily_baggage_reports.csv
  process_callable_path: "agentic_batch_triage_demo.helpers:triage_one_case"
  process_kwargs:
    baggage_db_path: /path/to/baggage.duckdb
  collect_callable_path: "agentic_batch_triage_demo.helpers:collect_batch_default"
  mapping_key_field: case_id
  retry_max_retries: 1
```

Emits a **`@graph_asset`** (in the asset catalog with lineage). This is the asset-lineage sibling of [`dynamic_fanout_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/dynamic_fanout_job), which emits a `@dg.job` instead.

Optional `upstream_asset_key` on the component adds a proper Dagster dep — the graph_asset takes that asset as input and passes it into `discover` as `upstream=<value>`. Useful when discovery reads from another asset (e.g., a `dataframe_from_csv` upstream).

### Variant C — domain-specific: `llm_multi_path_router` in `fanout_mode`

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_batch_triage_router_demo.sh -o setup.sh
bash setup.sh
```

**The same `LlmMultiPathRouterComponent` the SE saw in `agentic_router.md`**, just with `fanout_mode: true`. Same YAML surface, different runtime shape:

```yaml
type: dagster_community_components.LlmMultiPathRouterComponent
attributes:
  asset_name: daily_triage_batch
  upstream_asset_key: daily_baggage_reports
  fanout_mode: true                  # THE FLIP
  partition_static_column: case_id   # mapping_key column for fan-out

  # Everything else identical to the per-case demo:
  task_template: "You are a baggage-loss triage agent for case {case_id}..."
  tools:
    - name: query_baggage_db
      tool_type: sql
      resource: baggage_db
      sql_template: "SELECT ... WHERE baggage_id = '{args}'"
    - name: send_care_voucher
      system_message: "..."
    # ...
  outputs:
    - name: delivery_request
      output_schema:
        baggage_id: "..."
        address: "..."
        delivery_id: "..."
    - name: voucher_issued
      output_schema:
        voucher_id: "..."
        amount_usd: "..."
    - name: escalation
      output_schema:
        case_summary: "..."
```

Produces:
- `daily_triage_batch` @graph_asset (unpartitioned by default) — inside, `fan_out_cases → N x triage_one_case → collect_batch`.
- 3 branch assets (`delivery_request`, `voucher_issued`, `escalation`) — unpartitioned DataFrames where each row is a case whose ReAct classification picked that branch.

This is the SE demo's punchline: **the same component that produces the per-case partition shape (`agentic_router.md`) also produces the batch fan-out shape** — one config flip.

## Mix and match: partitioned batch + fan-out inside

You can compose `partition_type: daily` (or hourly, static, dynamic) with `fanout_mode: true`. The batch asset gets a **partitions_def** on the graph_asset itself; the fan-out happens **inside each partition's run**.

Example — daily batch of tickets:

```yaml
type: dagster_community_components.LlmMultiPathRouterComponent
attributes:
  asset_name: daily_triage_batch
  upstream_asset_key: daily_baggage_reports_by_day  # daily-partitioned source

  fanout_mode: true

  # Daily partition on the batch itself:
  partition_type: daily
  partition_start: "2026-07-01"

  # Column identifying each case within the day's data (used as DynamicOutput mapping_key):
  fanout_mapping_key_column: case_id

  # If upstream is UNPARTITIONED but has a date column, use this to filter
  # to the current partition_key's rows. (When upstream shares the same
  # partitions_def, Dagster's IO manager handles the filter automatically —
  # leave this unset.)
  # fanout_batch_filter_column: report_date

  task_template: |
    Batch: {partition_key}
    Case {case_id} for {passenger} on {flight}, baggage {baggage_id}. ...

  # tools + outputs same as above
```

Run behavior:
- On the daily schedule (or manual), materialize `daily_triage_batch[2026-07-30]`.
- Inside that one run, N cases for the day fan out in parallel.
- Downstream branches are also daily-partitioned — `delivery_request[2026-07-30]` contains the delivery cases for that day.
- Next day's cron run: `daily_triage_batch[2026-07-31]`.

That's the canonical production shape. Partitions for the batch-level lineage story (which day / hour / static batch id materialized), fan-out inside for the parallelism.

## When to reach for which variant

| Variant | Reach for it when |
|---|---|
| **Plain Python** | You're evaluating Dagster and want the raw framework. Or you have exactly one router and don't need to reuse it. |
| **Generic `DynamicFanoutAssetComponent`** | You want fan-out but your `process` callable is bespoke Python — not necessarily an LLM agent. Any batch job over items (URL scraping, fraud checks, doc processing) fits. |
| **`LlmMultiPathRouterComponent` fanout_mode** | You want the router's agent-specific features (tools + branches + structured payloads) AND batch semantics. Same YAML analysts already learned for per-case mode. |
| **`LlmMultiPathRouterComponent` fanout_mode + partition_type** | Production shape: daily/hourly/static-partitioned batch, fan-out inside. Cron drives runs, DynamicOut drives parallelism. |
| **Per-case partitions (`agentic_router.md`)** | Cases are stable business entities (customer_id, tenant_id) — you want each in the catalog for months. |

## What the demo does

All three scripts seed a DuckDB `baggage_tracking` table with 5 records and a `daily_baggage_reports.csv` with 5 cases (d1..d5). One run of the fan-out:

1. **Discover / read**: pull the 5 cases into memory.
2. **Fan out**: emit 5 `DynamicOutput`s, `mapping_key` = each case's `case_id`.
3. **Process** (in parallel per the executor's slot count): each case runs its own ReAct loop — real SQL against DuckDB for `query_baggage_db`, LLM roleplay for the stub tools. Classifier emits per-branch payloads.
4. **Collect**: gather results, aggregate to per-branch DataFrames (variants A, C) or append to a DuckDB audit table (variant B).

Example audit output (LLM decisions vary run-to-run):

```
d1  Alice   delivery_request  Delivery organized for BAG-001 to 12 Baker St, London
d2  Bob     voucher_issued    BAG-002 not found; care voucher issued to Bob
d2  Bob     escalation        BAG-002 not found; needs human review
d3  Carol   delivery_request  BAG-003 awaiting delivery to 123 Main St, Brooklyn
d4  Dan     delivery_request  BAG-004 in transit to ORD, delivery scheduled
d5  Eve     voucher_issued    BAG-005 not found; care voucher issued to Eve
d5  Eve     escalation        BAG-005 not found; case needs review
```

## Related walkthroughs (unlisted)

- [`agentic_router.md`](agentic_router.md) — same component in per-case partition mode. Surfaced.
- [`agentic_router_plain.md`](agentic_router_plain.md) — the per-case shape in plain Python. Unlisted.

## Files

- Variant A (plain Python): [setup_agentic_batch_triage_plain_demo.sh](setup_agentic_batch_triage_plain_demo.sh)
- Variant B (generic component): [setup_agentic_batch_triage_demo.sh](setup_agentic_batch_triage_demo.sh)
- Variant C (router-specific): [setup_agentic_batch_triage_router_demo.sh](setup_agentic_batch_triage_router_demo.sh)

All unlisted from the main examples index — reach these via direct link during SE conversations.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_batch_triage_demo.sh \
  -o setup_agentic_batch_triage_demo.sh
bash setup_agentic_batch_triage_demo.sh
```

## See also

<!-- TODO: link related walkthroughs -->
