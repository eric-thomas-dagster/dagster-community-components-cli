# Data combination — joins, unions, reshape, coerce

**Validated end-to-end** — RUN_SUCCESS in seconds. 6 synthetic source
assets feed 7 combination/coercion transforms.

```
orders + customers   → orders_with_customers   ← dataframe_join (left)
q1_sales + q2_sales  → all_sales               ← dataframe_union (concat)
orders               → orders_with_metrics     ← formula (computed cols)
orders               → orders_typed            ← type_coercer (string → typed)
orders               → orders_dates_parsed     ← datetime_parser (parse + extract Y/M/D)
tags_data            → exploded_tags           ← array_exploder (list col → row-per-element)
raw_sensors          → filled_sensors          ← ts_filler (forward-fill date gaps)
```

## Components used

| Component | What it does |
|---|---|
| `dataframe_join` | `pd.merge` — left/right/inner/outer/cross; on= or left_on/right_on |
| `dataframe_union` | `pd.concat` of N upstreams; outer/inner join on columns |
| `formula` | Add computed columns from pandas expressions: `revenue: price * qty` |
| `type_coercer` | Per-column dtype coercion: `int`, `float`, `bool`, `datetime`, `json`. errors: 'raise' \| 'coerce' \| 'ignore' |
| `datetime_parser` | Parse a string column → datetime. Optionally extract `_year`, `_month`, `_day`, `_hour`, `_dow` columns. |
| `array_exploder` | `df.explode(col)` — one row per array element |
| `ts_filler` | Reindex by date frequency + fill missing rows. ffill/bfill/interpolate, optionally per-group |

## Cost

**$0.** Pandas only.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_data_combination_demo.sh | bash
cd data-combination-demo
uv run dg launch --assets '*'
uv run dg dev   # http://localhost:3000
```

## YAML pitfalls — `on:` is a reserved keyword in YAML 1.1

`dataframe_join` uses an `on: [col]` field. YAML 1.1 (still the default
spec for many parsers) treats bareword `on` as a boolean (= `True`),
which causes a "True was unexpected" pydantic error. Always quote it:

```yaml
"on": [customer_id]    # not  on: [customer_id]
```

Same applies to `off`, `yes`, `no` — quote them when used as field
names. The example.yaml in this demo demonstrates the safe form.

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Generate synthetic orders (400 rows, schema_type: orders) and synthetic
    customers (250 rows, schema_type: customers). Join them on customer_id
    (inner join). Then extract the month from order_date via a formula.
    Also coerce total to float. Then group by first_name, email, and month —
    computing sum of total (call it total_spend) and count of orders. Filter
    to customers whose total_spend across all months exceeds 1000. Write
    the result to /tmp/high_value_customers_by_month.csv.
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

Live-validated on gpt-4o-mini: **8/9 clean picks in 22s, ~$0.0065 total cost.** Outputs written: `/tmp/high_value_customers_by_month.csv`.


After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.

## See also

<!-- TODO: link related walkthroughs -->
