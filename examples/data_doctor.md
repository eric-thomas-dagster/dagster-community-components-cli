# Data Doctor — the agent decides *what* the pipeline does

**Components:** `synthetic_data_generator` (with new `inject_dq_issues` flag), `dataframe_describe`, `langchain_chain_asset`, **`data_remediation_asset`** (new)

**Script:** [`setup_data_doctor_demo.sh`](./setup_data_doctor_demo.sh)
**Cost:** ~$0.005 per run (one gpt-4o-mini call per column, ~8 columns)
**Validated:** 2026-07-07 — RUN_SUCCESS end-to-end; agent correctly diagnosed 4.85% nulls in `amount` (picked `fill_nulls_with_median`) and 62% nulls in `merchant` (picked `fill_nulls_with_mode`); passed clean columns through as `none`; cleaned DataFrame had zero nulls.

## Why this exists

Every DQ pipeline is hard-coded: *"if nulls > 3%, drop the row; if outliers detected, clip at z=3."* The rules are frozen at pipeline-write time — they can't adapt to what the data actually looks like this run. What if instead the **agent** looks at each column's stats and picks a remediation, from a bounded safe list, based on what it sees?

That's this demo. It's the good shape of an "AI in the pipeline": the agent doesn't write code, doesn't invent operations, doesn't touch SQL. It **picks by name** from a fixed action space. Every choice is logged with a reason. The pipeline stays auditable and reproducible while adapting per-run to the actual data.

```
raw_transactions      (synthetic_data_generator + inject_dq_issues=true)
        ↓
dq_profile           (dataframe_describe — one row per column with stats)
        ↓
remediation_plan     (langchain_chain_asset — gpt-4o-mini picks ONE action per column)
        ↓
cleaned_transactions (data_remediation_asset — applies the agent's plan)
        ↓
verification_profile (dataframe_describe — proves the issues are gone)
```

## The new primitive — `data_remediation_asset`

This is what makes the pattern work. Fields:

| Field | Type | Description |
|---|---|---|
| `asset_name` | string | Output asset name. |
| `upstream_data_key` | string | Asset key producing the DataFrame to remediate. |
| `plan_key` | string | Asset key producing the plan DataFrame. |
| `fail_on_unknown_action` | bool | Default `false` — log-and-skip on unrecognized action names (agent hallucinations). |

### Bounded action space (fixed)

| Action | Params | Effect |
|---|---|---|
| `none` | — | Pass-through (agent decided no action needed). |
| `drop_nulls` | — | Drop rows where the column is null. |
| `fill_nulls` | `{value}` | Fill with a constant. |
| `fill_nulls_with_median` | — | Fill numeric nulls with median. |
| `fill_nulls_with_mean` | — | Fill numeric nulls with mean. |
| `fill_nulls_with_mode` | — | Fill nulls with the most common value. |
| `cast_type` | `{dtype}` | Cast column to a pandas dtype. |
| `dedup` | `{subset?}` | Drop duplicate rows. |
| `clip_outliers` | `{z_max}` | Clip values outside ±`z_max` standard deviations. |
| `filter_range` | `{min?, max?}` | Keep only rows in numeric range. |
| `strip_whitespace` | — | Strip leading/trailing whitespace from strings. |

The agent picks actions **by name**. It cannot invent code. Every applied action + its reason lands in asset metadata → full audit trail.

## Prerequisites

- `uv` + `OPENAI_API_KEY`

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_data_doctor_demo.sh -o setup_data_doctor_demo.sh
chmod +x setup_data_doctor_demo.sh
./setup_data_doctor_demo.sh
```

## Validated run output (2026-07-07)

The agent's plan (one row per column, gpt-4o-mini's picks):

```
column           dtype    null_pct   action                  reason
transaction_id   str      0.00       none                    No issues detected; null_pct is 0.
account_id       str      0.00       none                    No issues detected; null_pct is 0.
timestamp        str      0.00       none                    No issues detected; null_pct is 0.
type             str      0.00       none                    No issues detected; null_pct is 0.
amount           float64  4.85       fill_nulls_with_median  null_pct > 3 for a numeric column,
                                                             so filling with the median is appropriate.
merchant         str     62.46       fill_nulls_with_mode    null_pct > 3, categorical col,
                                                             so filling with the mode is appropriate.
category         str      0.00       none                    No issues detected; null_pct is 0.
status           str      0.00       none                    No issues detected; null_pct is 0.
```

Before/after row null counts:

```
BEFORE:                      AFTER (cleaned_transactions):
  amount:     15 nulls         amount:     0 nulls
  merchant:  193 nulls         merchant:   0 nulls
  (others):    0 nulls         (others):   0 nulls
```

Full audit trail lands in `cleaned_transactions` asset metadata (`plan_summary` field).

## What makes this "agentic" without being unsafe

- **Bounded action space.** The 11 actions are the only things the agent can execute. It cannot invent new ones (they're rejected — configurable).
- **Auditable.** Every action's `reason` is stored in the plan DataFrame. Re-running with the same inputs is deterministic modulo LLM temperature.
- **Composable.** The plan is just a DataFrame — you can inspect it in the UI before letting it apply, gate it on an asset check, route it through a review step, etc.
- **Transparent lineage.** The agent's decision appears as a Dagster asset (`remediation_plan`), not a hidden runtime side effect.

Compare to "LLM writes SQL" or "LLM emits Python": those have unbounded blast radius, no schema, no reproducibility guarantees.

## Extension patterns

- **Multi-action per column.** V1 picks one action per column. To pick multiple (e.g., `strip_whitespace` + `drop_nulls` on `email`), have the LLM emit an array; iterate in `data_remediation_asset`. The action space is already ordered-idempotent so applying twice is safe.
- **Human-in-the-loop.** Add an `asset_check` on `remediation_plan` that raises if any picked action is destructive (`drop_nulls`, `dedup`, `filter_range`) — force manual approval before `cleaned_transactions` runs.
- **Slack alert on plan.** Add a downstream `slack_notification` that posts the plan summary to `#data-quality` for review.
- **Bring your real data.** Replace `synthetic_data_generator` with `snowflake_query_asset` / `bigquery_query_asset` / your source of choice. The rest of the pipeline is source-agnostic.
- **Sensor-driven.** Wrap the pipeline in a sensor: whenever the source table gets new data, evaluate DQ; if issues > threshold, trigger the remediation job. See the [Adaptive Backfill Detective](./adaptive_backfill.md) demo for the sensor shape.

## The family of agentic-pipeline demos

Data Doctor is the first of three that use the "agent picks from a bounded action space" pattern:

1. **Data Doctor** *(this demo)* — agent picks DQ remediations per column.
2. **Adaptive Triage Router** — agent classifies incoming rows and routes to a picked downstream sink (billing / bug / churn / etc.).
3. **Adaptive Backfill Detective** — sensor + agent decides per-partition fill strategy when gaps appear (re-ingest / interpolate / skip / alert).

The common pattern: **agent picks by name from a bounded, safe set. Dagster executes.** The pipeline stays declarative + auditable; the picks stay adaptive.

## Related

- [PII detection + LLM redaction check](./pii_redaction.md) — different agentic shape: LLM as a **fresh-eyes double-checker** on statistical output.
- [Data Quality agent (LLM narration)](./data_quality_agent.md) — same DQ upstream, different LLM job: **narrate** anomalies for on-call, don't fix them.
- [dbt + LLM + dbt (mid-DAG generative)](./dbt_llm_pipeline.md) — LLM writes new columns (structured output → new marts), not remediations.
