# DataFrame basics — 9 fundamental shape-preserving transforms
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end** — RUN_SUCCESS in seconds. Synthetic 60-row sales
dataset fans out through 9 standard pandas operations, each exposed as a
typed Dagster component.

```
sales (synthetic source: 3 regions × 4 categories × 5 days)
       │
       ├── sales_filtered      ← filter (active rows + revenue > 50)
       ├── sales_sorted        ← sort (by date, revenue desc)
       ├── sales_unique        ← unique_dedup (subset=[transaction_id])
       ├── sales_slim          ← select_columns (5-col subset)
       ├── sales_cleansed      ← data_cleansing (trim/normalize/fillna)
       ├── revenue_by_region   ← summarize (group_by + aggregate)
       ├── ranked_categories   ← rank (per-region rank by revenue)
       └── running_revenue     ← running_total (cumulative per region)

monthly_metrics (long-format)
       │
       └── metrics_transposed  ← transpose (long → wide)
```

## Components used

| Component | Pandas equivalent |
|---|---|
| `filter` | `df.query("...")` |
| `sort` | `df.sort_values(...)` |
| `unique_dedup` | `df.drop_duplicates(...)` |
| `select_columns` | `df[[cols]]` / `df.drop(cols)` |
| `data_cleansing` | trim + case-normalize + fillna pipeline |
| `summarize` | `df.groupby(...).agg(...)` |
| `rank` | `df.groupby(...).rank(...)` |
| `running_total` | `df.groupby(...).cumsum()` |
| `transpose` | `df.set_index(...).T.reset_index()` |

## Cost

**$0.** Pandas only.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dataframe_basics_demo.sh | bash
cd dataframe-basics-demo
uv run dg launch --assets '*'
uv run dg dev   # http://localhost:3000
```

## Why one demo per family

Each component is a thin pydantic-backed wrapper around a single pandas
operation. Composed together, they replace much of the bespoke
"ETL helper" Python that data teams typically write — and they show up
as first-class assets with lineage, partitioning, and freshness in the
Dagster catalog.

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Take 500 synthetic customer rows (schema_type: customers) and chain these transforms in order:
      1. filter to active customers only (is_active is true).
      2. sort by lifetime_value descending.
      3. deduplicate on email.
      4. select columns [customer_id, first_name, last_name, email, city, state, lifetime_value].
      5. cleanse text columns (trim + lowercase first_name, last_name, email, city, state).
      6. group by state, computing count and mean lifetime_value.
      7. rank the states by mean_lifetime_value descending.
      8. add a running_total column of count, sorted by rank.
    Write the final result to /tmp/state_ranking.csv.
  include_ids: ['synthetic_data_generator']
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prefilter_llm: true
  prefilter_max_entries: 40
  max_iterations: 20
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: false
```

Live-validated on gpt-4o-mini: **13/15 clean picks in 35s, ~$0.0108 total cost.** Outputs written: `/tmp/state_ranking.csv`.


After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.

## See also

<!-- TODO: link related walkthroughs -->
