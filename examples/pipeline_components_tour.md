# Pipeline Components Tour

**One YAML declares a whole pipeline.** The `MLPipelineComponent`, `AgenticPipelineComponent`, and sibling `polars_pipeline` / `warehouse_pipeline` / `pyspark_pipeline` / `snowpark_pipeline` all share the same `source: + steps: + outputs:` shape. Different domains, same YAML idiom, same governance and CI story.

This tour walks that pattern end-to-end in ~45 minutes.

---

## Suggested reading order

### 1. [wine_ml.md](./wine_ml.md) — 8 ways to write the same ML pipeline

The **shape selector.** Same wine dataset, 8 implementations from raw `@asset` chains (imperative Python, closest to a traditional job-runner style) all the way to one-YAML `MLPipelineComponent` (fully declarative).

**Read this first** to see the range — you don't have to skip straight to YAML. Dagster supports the whole gradient: imperative → helpers → ops+graph → declarative one-liner. Each rung is a working, runnable variant.

### 2. [wine_ml_pipeline_component.md](./wine_ml_pipeline_component.md) — deep dive on `MLPipelineComponent`

The one-YAML shape in detail. **27 ops** covering feature engineering (`impute` / `scale` / `pca` / `polynomial_features` / …), hyperparameter tuning (`grid_search` / `random_search`), evaluation (`evaluate` / `confusion_matrix` / `cross_validate`), interpretability (`importance` / `shap_values`), model persistence (`save_model`).

Includes:
- Sources: `url` / `file` / `warehouse_query` / `upstream_asset`
- Sinks: first-class assets, CSVs, Parquet, warehouse tables (with Pattern A per-partition tables or Pattern B `partition_column:` for the analytics-friendly single-table shape)
- **"Partitioning — the production pattern"** section showing `{partition_key}` in `warehouse_query` source + `post_processing:` block declaring daily partitions

### 3. [agentic_pipeline.md](./agentic_pipeline.md) — same pattern for LLM / agent workflows

Sibling component for the LLM domain. **5 ops in v1**: `llm_call`, `route` (router picks specialist), `debate` (N proposers + arbitrator), `critique_loop` (drafter/critic iteration), `synthesize` (fan-in).

Contains the **"Why Dagster (not just a job runner)"** section — the sharpest pitch for the assets model over a run-and-log-it approach:

- Every step's decision (router pick, arbitrator reasoning, critique history) is a **browsable versioned asset with typed metadata**: `cost_usd` (Float), `latency_ms` (Int), `tokens_total` (Int), `model_fingerprint` (Text), `materialized_at` (Timestamp), `op` (Text) — no log-grepping.
- **Dagster+ Insights** — promote the numeric metadata (`cost_usd`, `latency_ms`, `tokens_total`, `n_llm_calls`) into custom metrics from the Dagster+ UI (a few clicks, no code). Once promoted, dashboards + per-metric alerts follow — no separate metrics-export pipeline to build.
- **Per-op Dagster kinds** — filter the whole catalog to "show me every `debate` step" or "every `route` step" across every pipeline.
- **Time-travel to any partition** — the demo ships 3 dates, each = different question, each = independently browsable decision.
- **Deploying to Dagster+ Serverless** — relative paths, ephemeral filesystem story, how to swap file sinks for warehouse `table_sinks` for durable outputs.

---

## The rest of the pipeline-component family

The three walkthroughs above cover ML + agentic. The pattern extends to four more execution engines — same `source: + steps: + outputs:` YAML idiom, different backend:

| Component | Domain | Execution engine | Walkthrough |
|---|---|---|---|
| `ml_pipeline` | ML training + evaluation | sklearn / XGBoost / LightGBM | ← covered above |
| `agentic_pipeline` | LLM / agent workflows | LiteLLM (100+ providers) | ← covered above |
| `polars_pipeline` | DataFrame transforms | polars lazy engine — filter/projection fusion, parallel exec inside one asset | [polars_pipeline.md](./polars_pipeline.md) |
| `pyspark_pipeline` | Big-data DataFrame transforms | PySpark / Catalyst — predicate pushdown, join reordering, cluster-scale execution | [pyspark_pipeline.md](./pyspark_pipeline.md) |
| `snowpark_pipeline` | Warehouse-native DataFrame ops | Snowpark — lazy chain compiles to ONE Snowflake SQL statement, no data flows through Python | [snowpark_pipeline.md](./snowpark_pipeline.md) |
| `warehouse_pipeline` | SQL CTE chain | Any warehouse (Snowflake / BigQuery / Redshift / Postgres / DuckDB) — multi-step compiled to ONE CTAS | [warehouse_native_pipeline.md](./warehouse_native_pipeline.md) |

**Why the whole family matters:** one YAML shape means governance / review / CI templates transfer across ML, transforms, warehouse SQL, and agentic workflows. Learn the pattern once — apply it to every domain that has a "source → chained steps → sinks" story.

---

## Runnable demos

Both demos scaffold a fresh Dagster project + install the component + write a working `defs.yaml`. `dg dev` opens the Dagster UI at `http://localhost:3000` where you can browse the asset graph and click to materialize.

**Wine ML pipeline** (no auth required):

```bash
curl -sL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_component_demo.sh | bash -s my-wine-demo
cd my-wine-demo && uv run dg dev
```

**Agentic pipeline** (needs `OPENAI_API_KEY`, ~$0.003 total per 3-partition backfill):

```bash
export OPENAI_API_KEY=sk-...
curl -sL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_pipeline_demo.sh | bash -s my-agent-demo
cd my-agent-demo && uv run dg dev
```

Both projects also work headlessly for CI: `uv run dg launch --assets '*'` (unpartitioned) or `--partition <key>` (partitioned demo).

---

## Component reference (raw docs)

If you want the raw component READMEs — full op menu, schema, escape hatches — they live in the component templates repo:

- **[MLPipelineComponent](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/analytics/ml_pipeline/README.md)** — 27 ops + `sklearn_class:` escape hatch for XGBoost / LightGBM / catboost / any sklearn-compatible estimator
- **[AgenticPipelineComponent](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/ai/agentic_pipeline/README.md)** — 5 ops + the "Why Dagster" metadata table showing every typed field the UI renders

---

## Browsable component registry

Beyond the two pipeline components, the community registry has **~962 components** covering ingestion / transforms / IO managers / sensors / sinks / resources / integrations for the long tail of vendors and workflows:

**Registry UI:** https://dagster-component-ui.vercel.app/

Search, filter by category, view schemas + example YAMLs in-browser.
