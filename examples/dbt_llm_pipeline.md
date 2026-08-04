# dbt + LLM + dbt — LangChain in the Middle of Your dbt DAG
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Components:**
- `dagster_dbt.DbtProjectComponent` (official) — used twice with different `select` filters + explicit `op.name`
- `LangChainChainAssetComponent` (`assets/ai/langchain_chain_asset`)

**Script:** [`setup_dbt_llm_pipeline_demo.sh`](./setup_dbt_llm_pipeline_demo.sh)
**Cost:** ~$0.01 per run (13 LLM calls at ~150 tokens each on gpt-4o-mini)
**Duration:** ~45 seconds cold to green
**Companion to:** [dbt + ML pipeline](./dbt_ml_pipeline.md) — same shape, sklearn scorer instead of LLM

## Why this matters

The [dbt + ML demo](./dbt_ml_pipeline.md) shows a classical Python ML asset between two dbt sets. This demo shows the same pattern with **a generative LLM in the middle** — a LangChain chain that makes one API call per row of the upstream dbt table, generates a personalized retention email, parses the JSON response into DataFrame columns, and hands the result off to the next dbt model for final joining.

The pattern is unchanged. What changes is what "the middle step" does:
- **ML version** → scores customers (classical statistics)
- **LLM version** → writes text for each customer (generative)

Both fit into the same Dagster shape. That's the point.

```
customers.csv                       retention_outreach
    ↓                                (LLM output written
stg_customers  ─┐                    to DuckDB with 2
                │                    JSON-parsed columns:
orders.csv      │                    email_subject + body)
    ↓           │                        │
stg_orders  ───┴──▶ customer_features ──▶ (LLM row-wise:      ──▶ customer_outreach_plan
    (dbt SQL)      (dbt SQL)              LangChain chain            (dbt SQL, joins the
                                          calls gpt-4o-mini)          LLM output back in)
```

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `OPENAI_API_KEY` — get one at https://platform.openai.com/api-keys

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dbt_llm_pipeline_demo.sh -o setup_dbt_llm_pipeline_demo.sh
chmod +x setup_dbt_llm_pipeline_demo.sh
./setup_dbt_llm_pipeline_demo.sh
```

## What the script does

1. Scaffolds a fresh Dagster project via `uvx create-dagster project`.
2. Installs deps including `langchain-openai`, `dagster-dbt`, `dbt-duckdb`, `dagster-duckdb-pandas`.
3. Writes the dbt project (8 customers × 13 orders, 3 staging models, 1 marts model).
4. Writes three Dagster `defs.yaml`:
   - `dbt_staging/defs.yaml` — dbt for staging models with `op.name: dbt_staging_op`
   - `llm_outreach/defs.yaml` — `LangChainChainAssetComponent` with `upstream_asset_key: customer_features` and `parse_json: true`
   - `dbt_marts/defs.yaml` — dbt for marts models with `op.name: dbt_marts_op`
5. Configures `DuckDBPandasIOManager` as the project's IO manager.
6. Runs `dbt seed` then materializes the full DAG.

## The LLM asset config

```yaml
type: dagster_community_components.LangChainChainAssetComponent
attributes:
  asset_name: retention_outreach
  upstream_asset_key: customer_features
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.4
  max_tokens: 400

  system_message: |
    You are a B2B customer-success writer. For each customer, write a short
    retention outreach email in JSON with two keys: 'retention_email_subject'
    (max 10 words) and 'retention_email_body' (2-3 sentences, warm, specific,
    referencing their plan and industry). Output ONLY the JSON object.

  prompt_template: |
    Customer profile:
      email: {email}
      plan: {plan}
      industry: {industry}
      total_orders: {total_orders}
      total_revenue: {total_revenue}
      days_since_last_order: {days_since_last_order}

  # Parse the JSON response and expand the two keys into DataFrame columns
  # so the dbt marts model can `SELECT ro.retention_email_subject`.
  parse_json: true
```

**Key move — `parse_json: true`.** Without it, the LLM returns a raw string in one `chain_output` column. With it, `langchain_chain_asset` parses the LLM's JSON response and unpacks its top-level keys as separate DataFrame columns. That's how the downstream dbt marts model can reference `ro.retention_email_subject` and `ro.retention_email_body` by name.

## The marts model

```sql
select
  f.customer_id, f.email, f.plan, f.industry, f.total_orders, f.total_revenue,
  case
    when f.days_since_last_order is null           then 'never_ordered'
    when f.days_since_last_order > 180             then 'dormant_180d'
    when f.days_since_last_order > 90              then 'lapsing_90d'
    else 'active'
  end                                                     as engagement_stage,
  ro.retention_email_subject,
  ro.retention_email_body
from {{ ref('customer_features') }} f
left join {{ source('main', 'retention_outreach') }} ro on ro.customer_id = f.customer_id
order by f.days_since_last_order desc nulls last
```

The marts model joins the dbt-computed features with the LLM-generated columns via the standard `{{ source() }}` mechanism — dbt has no idea the source table came from Python.

## Extension ideas

- **Different LLM per row.** Set `model: {model_column}` in `prompt_template` and add a `model_column` column to `customer_features` — route enterprise customers to `openai/gpt-4o`, others to `openai/gpt-4o-mini`. Or use the [Vercel AI Gateway agent](./vercel_ai_gateway_agent.md) as the middle asset instead of LangChain, and route across providers with `fallback_models`.
- **Multi-step reasoning.** Swap `langchain_chain_asset` for `langgraph_agent` — plan → draft → critique → finalize per customer, with the final email in the last node's output.
- **Structured evaluation.** Add a `llm_judge` asset downstream to score each generated email on tone / brand fit / factual grounding before the marts join.
- **Persist prompts + responses for auditing.** Add asset metadata capturing full prompts + responses per row — Dagster's asset materialization history becomes your LLM audit trail.

## See also

- [dbt + ML + dbt (classical ML mid-DAG)](./dbt_ml_pipeline.md) — same shape, sklearn scorer.
- [LangGraph Agent](./langgraph_agent.md) — multi-step LangGraph pipeline as a single asset (no dbt).
- [Vercel AI Gateway Agent](./vercel_ai_gateway_agent.md) — multi-provider LLM routing (drop-in replacement for the LangChain middle asset).
