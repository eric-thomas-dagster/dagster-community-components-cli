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

## Components covered (7)

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

## Run it

```bash
./setup_data_combination_demo.sh
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
