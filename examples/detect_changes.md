# Detect Changes demo

Diff yesterday's customer snapshot against today's, classify each row as
**insert / update / delete / unchanged**.

```
csv (yesterday) ─┐
                 ├─→ detect_changes → CSV
csv (today)     ─┘
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Yesterday: C001-C004 |
| 2 | `file_ingestion` | ingestion | Today: C001-C003, C005 (no C004); C003's plan_tier upgraded |
| 3 | `detect_changes` | transformation | Outer-merge + classify |
| 4 | `dataframe_to_csv` | sink | Write changeset |

## Configuration

```yaml
business_key_columns: [customer_id]
compare_columns: [plan_tier, country]
include_unchanged: true     # set false to drop unchanged rows
change_type_column: change_type
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_detect_changes_demo.sh | bash
cd detect-changes-demo && uv run dg launch --assets '*'
```

## Expected output

5 rows with the following `change_type` values:

| customer_id | change_type | reason |
|---|---|---|
| C001 | unchanged | identical in both snapshots |
| C002 | unchanged | identical in both snapshots |
| C003 | update | plan_tier free → pro |
| C004 | delete | only in yesterday |
| C005 | insert | only in today |

## Why it's useful

CDC-friendly. Pipe the output into `alter_row` to mark rows for a downstream
sink, into `summarize` for daily-changes dashboards, or into a Slack/email
digest of "what changed today".

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Generate two synthetic customer datasets (schema_type: customers):
      - baseline: 300 rows, random_state 1
      - current:  400 rows, random_state 1  (overlaps baseline plus new rows)
    Use detect_changes to classify each customer_id as
    added / removed / modified / unchanged between the two snapshots.
    Write the change report to /tmp/customer_changes.csv.
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

Live-validated on gpt-4o-mini: **4/5 clean picks in 10s, ~$0.0043 total cost.** Outputs written: `/tmp/customer_changes.csv`.


After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.
