# Churn prediction — synthetic customer aggregates → risk scores
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

A 3-component pipeline that generates 200 synthetic customer-level
aggregate rows (`last_activity`, `total_orders`, `total_revenue`,
`lifetime_days`), runs `churn_prediction` (rule-based: inactivity
threshold + risk factors), writes per-customer risk scores + tier +
recommended action.

## Pipeline

```
file_ingestion → churn_prediction → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Load 200 synthetic per-customer aggregates |
| 2 | `churn_prediction` | analytics | Score each customer (inactivity threshold = 60d, lookback = 365d) and assign a risk level + recommendation |
| 3 | `dataframe_to_csv` | sink | Write per-customer report |

## Components used

- `churn_prediction`
- `dataframe_to_csv`
- `synthetic_data_generator`

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_churn_demo.sh | bash
cd churn-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/churn_predictions.csv` — every customer with:

- `days_inactive` — derived from `last_activity`
- `churn_risk_score` — 0–1 weighted score
- `churn_risk_level` — Low / Medium / High / Critical Risk
- `recommended_action` — text recommendation per tier
- `risk_factors` — comma-separated contributing factors
- `activity_trend` — derived signal

Risk distribution from a real run:

```
Critical Risk    72   (36%)   — 91+ days inactive, intervention urgent
Low Risk         52   (26%)
Medium Risk      52   (26%)
High Risk        24   (12%)
```

## What this demo shows

- **First demo using `churn_prediction`** — rule-based scoring (no ML
  model needed) that combines inactivity, order frequency, revenue,
  and lifetime into a weighted score. `include_risk_factors: true`
  attaches the contributing factors per row, so the output is
  interpretable not just numeric.
- **Customer-level aggregates as input.** The component expects one
  row per customer with the listed columns. To get there from
  transaction-level data, run `summarize` upstream (`group_by:
  customer_id`, `aggregations: {revenue: sum, txn_id: count, ...}`).
- **The synthetic-data heredoc pattern again.** For per-customer rollups
  with realistic distributions across "fresh / dormant / churned"
  cohorts, an inline Python heredoc is the right tool —
  `time_series_generator` is for time-series, not aggregates.

## Extending

Pair with `customer_segmentation` (RFM scoring) on the same input — the
two components answer different questions (who's churning soon vs. who
falls into Champion / At Risk / etc. RFM segments). Combine for a richer
CDP view.

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can build this pipeline from a natural-language task — no defs.yaml per component. Drop this in a single defs.yaml and run `dg utils refresh-defs-state`:

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Generate 800 synthetic customer rows (schema_type: customers). Then:
      1. Add columns via formula: is_active_int = is_active.astype(int),
         churned = (lifetime_value < 3000).astype(int).
      2. One-hot encode the state column with drop_first=true.
      3. Fit a random_forest_model to predict churned from
         [lifetime_value, is_active_int, and all state_* one-hot columns].
         Use test_size=0.2, random_state=42.
      4. Write the test-set predictions (with probabilities) to /tmp/churn_preds.csv.
  include_ids: [synthetic_data_generator, random_forest_model]
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prefilter_llm: true
  prefilter_max_entries: 40
  max_iterations: 12
  defs_state: { management_type: LOCAL_FILESYSTEM, refresh_if_dev: false }
```

Live-validated on gpt-4o-mini: **5/5 clean picks in 13.6s, ~$0.0043 total cost.** Outputs written: `/tmp/churn_preds.csv` — 800 rows with `churned` label + one-hot state + `predicted` column from the RF model.

After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
