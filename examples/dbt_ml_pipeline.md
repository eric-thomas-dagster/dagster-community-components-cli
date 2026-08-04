# dbt + ML + dbt — A Python ML Model in the Middle of Your dbt DAG
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Components:**
- `dagster_dbt.DbtProjectComponent` (official dagster-dbt) — used twice with different `select` filters
- `ChurnPredictionComponent` (`assets/analytics/churn_prediction`)

**Script:** [`setup_dbt_ml_pipeline_demo.sh`](./setup_dbt_ml_pipeline_demo.sh)
**Cost:** **$0** — DuckDB + local sklearn heuristic scorer, no API keys, no cloud
**Duration:** ~30 seconds cold to green
**Validated:** 2026-07-06 — RUN_SUCCESS end-to-end; final marts table has ML-enriched customer risk classifications

## Why this matters

**The single most common "why Dagster over Airflow" question** is: *"Can I put a Python ML model between two sets of dbt models?"* — and in Airflow, the honest answer is "not really." Airflow treats dbt as one big opaque operator; you can't insert a Python step between individual models.

Dagster turns every dbt model into a **first-class asset**. A Python asset drops right in the middle of the DAG and cross-language lineage just works. dbt writes to DuckDB, the Python asset reads from DuckDB, writes back to DuckDB, next dbt model picks it up.

```
customers.csv                      churn_predictions
    ↓                                (Python DataFrame,
stg_customers  ─┐                    written to DuckDB
                │                    by DuckDBPandasIOManager)
                │                        │
orders.csv      │                        │
    ↓           │                        ▼
stg_orders  ───┴──▶ customer_features ──▶ (Python asset:      ──▶ dim_customer_with_risk
    (dbt SQL)      (dbt SQL)              ChurnPrediction)          (dbt SQL, joins the
                                                                     ML output back in
                                                                     via {{ source() }})
```

## The IO-manager trick (this is the whole point)

**Q: dbt writes to a database; Python reads/writes a DataFrame. How does that not break?**

**A: `DuckDBPandasIOManager` is the bridge.** When you configure it as the project's default IO manager:
- Python asset input → IO manager `SELECT * FROM customer_features` → pandas DataFrame handed to your Python code
- Python asset output → IO manager `CREATE TABLE churn_predictions AS SELECT * FROM ...` from the returned DataFrame
- Next dbt model → sees `main.churn_predictions` via `{{ source('main', 'churn_predictions') }}` — same DuckDB file, same schema, no serialization

No pickle files, no CSV round-trips, no glue code. Data lives in DuckDB the entire time.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`

That's it. **No API keys. No cloud. No paid services.**

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dbt_ml_pipeline_demo.sh -o setup_dbt_ml_pipeline_demo.sh
chmod +x setup_dbt_ml_pipeline_demo.sh
./setup_dbt_ml_pipeline_demo.sh                    # → dbt_ml_demo/
```

## What the script does

1. Scaffolds a fresh Dagster project via `uvx create-dagster project`.
2. Installs deps: `dagster-community-components`, `dagster-dbt`, `dbt-core`, `dbt-duckdb`, `scikit-learn`, `pandas`, `dagster-duckdb-pandas`.
3. Writes the dbt project:
   - `dbt_analytics/seeds/customers.csv` — 20 customers
   - `dbt_analytics/seeds/orders.csv` — 32 orders
   - `dbt_analytics/models/staging/stg_customers.sql`
   - `dbt_analytics/models/staging/stg_orders.sql`
   - `dbt_analytics/models/staging/customer_features.sql` — the "before ML" model
   - `dbt_analytics/models/marts/sources.yml` — declares `churn_predictions` as a source
   - `dbt_analytics/models/marts/dim_customer_with_risk.sql` — the "after ML" model
4. Writes three Dagster `defs.yaml`:
   - `dbt_staging/defs.yaml` — `DbtProjectComponent` with `select: "path:models/staging"` + explicit `op.name: dbt_staging_op`
   - `churn_model/defs.yaml` — `ChurnPredictionComponent` with `upstream_asset_key: customer_features`
   - `dbt_marts/defs.yaml` — `DbtProjectComponent` with `select: "path:models/marts"` + explicit `op.name: dbt_marts_op`
5. Configures `DuckDBPandasIOManager` as the project's default resource in `definitions.py`.
6. Runs `dbt seed` to load the CSVs into DuckDB.
7. Materializes the full DAG: `dagster asset materialize --select '*'`.

## Two dbt components, one dbt project

The demo splits the dbt work into **two `DbtProjectComponent` instances** on the same `dbt_analytics/` project, each selecting a different model directory:

```yaml
# dbt_staging/defs.yaml
type: dagster_dbt.DbtProjectComponent
attributes:
  project: "{{ project_root }}/dbt_analytics"
  select: "path:models/staging"
  op:
    name: dbt_staging_op          # ← explicit op name, avoids collision

# dbt_marts/defs.yaml
type: dagster_dbt.DbtProjectComponent
attributes:
  project: "{{ project_root }}/dbt_analytics"
  select: "path:models/marts"
  op:
    name: dbt_marts_op            # ← different op, so Dagster can order them
```

Without the explicit op names, both components would default to op name `dbt_analytics` (the project's name) and collide. With them, Dagster generates two separate ops and can materialize:
1. `dbt_staging_op` — `dbt build --select path:models/staging`
2. `churn_predictions` (Python)
3. `dbt_marts_op` — `dbt build --select path:models/marts`

**You can also use two separate dbt projects** if you want physical separation — same pattern, more scaffolding.

## The Python asset

Middle of the DAG — reads `customer_features` (dbt output), writes `churn_predictions`:

```yaml
# churn_model/defs.yaml
type: dagster_community_components.ChurnPredictionComponent
attributes:
  asset_name: churn_predictions
  upstream_asset_key: customer_features
  inactivity_threshold_days: 90
  lookback_days: 180
  include_risk_factors: true
```

`ChurnPredictionComponent` is a heuristic scorer (no training data required — that's what makes the demo zero-config). Its output DataFrame has columns:

- `customer_id`
- `days_inactive`
- `activity_trend`
- `churn_risk_score` (0-100)
- `churn_risk_level` (`Low` / `Medium` / `High` / `Critical`)
- `recommended_action`
- `risk_factors`

## The marts model reads Python output as a source

```sql
-- dbt_analytics/models/marts/dim_customer_with_risk.sql
select
  f.customer_id, f.email, f.plan,
  cp.churn_risk_score, cp.churn_risk_level, cp.risk_factors,
  case
    when f.plan = 'premium' and cp.churn_risk_level in ('Critical', 'High') then 'premium_immediate_outreach'
    when f.plan = 'premium' and cp.churn_risk_level = 'Medium' then 'premium_proactive_engagement'
    when f.plan = 'basic' and cp.churn_risk_level in ('Critical', 'High') then 'basic_upgrade_offer'
    else 'monitor'
  end as business_action
from {{ ref('customer_features') }} f
left join {{ source('main', 'churn_predictions') }} cp on cp.customer_id = f.customer_id
order by cp.churn_risk_score desc nulls last
```

`{{ source('main', 'churn_predictions') }}` — dbt sees the Python asset's DuckDB table as a source. dagster-dbt maps this to the Python asset's Dagster asset key for lineage.

## Validated run output (2026-07-06)

Final marts table:

```
 customer_id              email     plan  total_orders  total_revenue churn_risk_level             business_action
           9   ivan@example.com  premium             2         555.00         Critical  premium_immediate_outreach
           4   dave@example.com    basic             1          30.00         Critical         basic_upgrade_offer
          12  laura@example.com  premium             3         655.00         Critical  premium_immediate_outreach
          14   nate@example.com  premium             2         595.00         Critical  premium_immediate_outreach
           2    bob@example.com    basic             2         105.00         Critical         basic_upgrade_offer
           1  alice@example.com  premium             4         556.25         Critical  premium_immediate_outreach
           5    eve@example.com  premium             3         480.50         Critical  premium_immediate_outreach
           6  frank@example.com    basic             1          55.00         Critical         basic_upgrade_offer
```

Each row is a customer with columns from `customer_features` (dbt) joined with columns from the Python ML asset — one clean table for the CRM team.

## Bugs surfaced during validation

1. **`DbtDocsEnrichedProjectComponent` was missing from the lazy loader.** Added the registration. (Doesn't block this demo since we use the official `dagster_dbt.DbtProjectComponent`.)
2. **`DbtDocsEnrichedProjectComponent.build_defs_from_state()` signature mismatch** with the current `dagster-dbt` base class. Not fixed as part of this demo — filed as a follow-up. This demo uses the official component instead.
3. **Op-name collision** when two `DbtProjectComponent` instances share a project name. Solved via explicit `op.name` — this is the recommended pattern for split-dbt-with-Python-in-between.
4. **Schema-prefixed asset keys.** Setting `+schema: staging` in `dbt_project.yml` makes asset keys `["staging", "customer_features"]`, which breaks `upstream_asset_key: customer_features`. Fix: keep the default `main` schema so keys stay flat.

## Extension ideas

- **Real sklearn training.** Swap `ChurnPredictionComponent` for `LogisticRegressionModelComponent` or `GradientBoostingModelComponent`. Requires labeled training data.
- **Multiple ML models mid-DAG.** Add `linear_regression_model` for LTV prediction, `naive_bayes_model` for text category classification, etc. — each is another Python asset that fits between dbt models.
- **Move from DuckDB to Snowflake / BigQuery.** Swap the IO manager (`SnowflakeIOManager` etc.). Same dbt models, same Python assets, different backend.
- **Add reverse-ETL downstream.** After `dim_customer_with_risk`, wire in a `hubspot_sync` or `slack_notification` sink to push the highest-risk customers to a Customer Success workflow.
- **Add data-quality checks.** Wrap the `customer_features` model with a `dagster_asset_check` component that fails the run if the row count drops below expectations.

## See also

- [Warehouse migration playbook](./warehouse_migration.md) — same shape but for one-time SQL DB → warehouse moves.
- [Airports Clustering demo](./airports_cluster.md) — another ML-in-the-middle pattern (KMeans + Dagster asset lineage).
- [dbt Cloud sensor](./dbt_cloud.md) — the same trick when your dbt runs on dbt Cloud instead of local `dbt-core`.
