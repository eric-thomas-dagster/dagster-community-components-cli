# Titanic ETL — kitchen-sink pipeline (9 transforms)

A long-form data engineer's pipeline that walks raw Titanic CSV through
nine distinct transforms before writing a sample CSV. Touches the most
common cleanup, enrichment, and prep tasks in one chain — useful as a
"what does a real Dagster components pipeline look like?" example.

## Pipeline

```
csv_file_ingestion → type_coercer → data_cleansing → imputation
                   → tile_binning → field_mapper → arrange
                   → sample → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull Titanic CSV |
| 2 | `type_coercer` | transformation | Coerce `Age`/`Fare`→float, `Pclass`→int with `errors: coerce` |
| 3 | `data_cleansing` | transformation | Trim whitespace + fill string nulls with `"unknown"` (scoped to listed columns only) |
| 4 | `imputation` | transformation | Median-fill the now-numeric `Age` and `Fare` |
| 5 | `tile_binning` | transformation | Bin `Age` into named buckets: Child / Teen / Adult / Senior |
| 6 | `field_mapper` | transformation | Rename to snake_case (`PassengerId`→`passenger_id`, `Pclass`→`pclass`, …); `drop_unmapped: true` |
| 7 | `arrange` | transformation | Reorder columns: identity + outcome up front, narrative columns to the back |
| 8 | `sample` | transformation | 50-row reproducible (random_state=42) preview |
| 9 | `dataframe_to_csv` | sink | Write the preview |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_titanic_etl_demo.sh | bash
cd titanic-etl-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/titanic_preview.csv`:

```
passenger_id,survived,pclass,age_bracket,name,sex,age,siblings_spouses,...
710,1,3,Adult,"Moubarek, Master. Halim Gonios",male,28.0,1,...
440,0,2,Adult,"Kvillner, Mr. Johan Henrik Johannesson",male,31.0,0,...
```

Renamed to snake_case, age bucketed, identity columns first.

## What this demo shows

- **Realistic ETL chain.** Most pipelines aren't 3 components —
  they're 8-12. Each step does one thing.
- **`type_coercer` with `errors: coerce`** — turns "string-formatted
  numbers" into NaN rather than erroring. Combine with `imputation`
  downstream to fill the NaNs.
- **`data_cleansing` `null_handling: fill` is scoped.** Listing
  `columns: [Name, Sex, ...]` means only those columns get the
  string fill — numeric columns are untouched, so subsequent
  median-imputation on `Age` works correctly. (This was a recently-fixed
  bug — the old behavior corrupted numeric columns.)
- **`field_mapper` vs. `select_columns`.** `field_mapper` does
  rename + select + (optionally) drop unmapped — one component vs. the
  two-step rename-then-select pattern.
- **`arrange` is small but useful.** `move_to_front` / `move_to_back`
  reorders without retyping the entire column list.
- **`tile_binning` with `method: custom`** + named labels gives
  human-readable categorical output ready for downstream group-by.

## Extending

Drop `summarize` between `tile_binning` and `field_mapper` to compute
survival rate by `age_bracket` × `Sex` for an "executive summary" sink.
Or chain `count_records` after `data_cleansing` for a row-count check
asset alongside the transformed output.
