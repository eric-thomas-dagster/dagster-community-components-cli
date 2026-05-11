# Transformations mega-demo (34 components)

**Validated end-to-end** — 34 pure-pandas transformation components fan
out from 30 synthetic orders + 3 small lookup/dim tables. **Zero API
keys, zero external services, $0 cost.**

```
orders                        category_lookup       products_dim       orders_dim_existing
(30 rows, rich schema)        (4 rows)              (7 rows)           (3 rows)
       │                           │                    │                    │
       ├──→ 30+ pure-pandas fan-out ─────────────┴──────┴────────────────────┘
       │
       ├── orders_arranged          (arrange — column reordering / rename)
       ├── orders_typed             (auto_field — auto-detect dtypes)
       ├── orders_count             (count_records — group counts)
       ├── orders_crosstab          (cross_tab — pivot table)
       ├── orders_with_supplier     (document_merger — join with products_dim)
       ├── orders_email_parsed      (email_parser — extract user/domain/tld)
       ├── orders_renamed           (field_mapper — column rename)
       ├── orders_replaced          (find_replace — lookup replacement)
       ├── orders_fuzzy             (fuzzy_match — RapidFuzz string match)
       ├── orders_generated         (generate_rows — duplicate rows)
       ├── orders_encoded           (label_encoder — categorical → int codes)
       ├── orders_pivot_cols        (make_columns — wide-format reshape)
       ├── orders_grouped           (make_group — group key generation)
       ├── orders_stripped          (markdown_stripper — strip MD formatting)
       ├── orders_binned            (multi_field_binning — quantile binning)
       ├── orders_formula           (multi_field_formula — apply expr to N cols)
       ├── orders_row_formula       (multi_row_formula — lag / rolling_mean)
       ├── orders_with_id           (record_id — sequential ID column)
       ├── orders_sampled           (sample — random sample)
       ├── orders_validated         (schema_validator — JSON schema check)
       ├── orders_selected          (select_records — head/tail/range)
       ├── orders_split             (text_to_columns — split on delimiter)
       ├── orders_split_set_train   (train_test_splitter — train output)
       ├── orders_split_set_test    (train_test_splitter — test output)
       ├── orders_wavg              (weighted_average — group-wise wavg)
       ├── events_normalized        (siem_event_normalizer — OCSF schema)
       ├── orders_scd               (scd_type_1 — SCD Type 1 merge)
       ├── orders_lookup            (lookup — left-join enrichment)
       ├── orders_surrogate         (surrogate_key — sha256 surrogate ID)
       ├── orders_hashed            (hash — column-level hashing)
       ├── orders_mapped            (map_values — value remapping)
       ├── orders_crossed           (cross_join — Cartesian product)
       ├── orders_audit             (audit_columns — run_id / asset_key / time)
       ├── orders_masked            (data_masking — hash + redact PII)
       └── orders_appended          (append_fields — UNION-style row append)
```

## Validated end-to-end

All 34 components materialize successfully on first run. Total wall-clock
~10s with parallel multiprocess execution.

## Skipped (incompatible with this demo's pattern)

| Component | Why skipped |
|---|---|
| [`file_transformer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/file_transformer) | Reads raw files (CSV/JSON/Parquet) — not a DataFrame-in component |
| [`sql_transform`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/sql_transform) | Needs a SQLAlchemy connection URL env var (postgres://, etc.) |
| [`dataframe_transformer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/dataframe_transformer) | Component has a `retry_policy_max_retries` attribute bug at runtime |

## Bugs found + fixed during validation

- **`document_merger.on` and `lookup.on`**: YAML 1.1 parses `on:` as
  boolean `True`, not the string key the components expect. Fixed in
  the demo by quoting the key: `"on": product`.

- **[`dataframe_transformer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/dataframe_transformer)**: tries to read `self.retry_policy_max_retries`
  but the field doesn't exist on the model — runtime AttributeError.
  Excluded from this demo. Should be fixed in the component itself.

## Field-name reference (cheat sheet)

The transformation components don't follow a fully consistent naming
convention. Key gotchas surfaced during validation:

| Component | Fields differ from what you'd guess |
|---|---|
| [`arrange`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/arrange) | `move_to_front`, `move_to_back`, `column_order`, `rename` (not `sort_keys`) |
| [`make_columns`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/make_columns) | `n_columns`, `value_column`, `key_column` (no `pivot_column`) |
| [`multi_field_binning`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/multi_field_binning) | `n_bins`, not `num_bins` |
| [`multi_field_formula`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/multi_field_formula) | `expression: "{col} * 1.1"` — `{col}` is the placeholder |
| [`multi_row_formula`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/multi_row_formula) | `operations: [{output_column, column, operation: lag\|rolling_mean\|...}]` |
| [`text_to_columns`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/text_to_columns) | `delimiter`, not `separator` |
| [`train_test_splitter`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/train_test_splitter) | `seed`, not `random_state`; outputs `<asset>_train` + `<asset>_test` |
| [`sample`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/sample) | `n` or `frac`, not `sample_size` |
| [`select_records`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/select_records) | `mode: head\|tail\|range\|indices`, not `filter_expression` |
| [`generate_rows`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/generate_rows) | `mode: duplicate`, plus `n` |
| [`siem_event_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/siem_event_normalizer) | `schema: ocsf\|ecs`, no `event_column` |
| [`cross_join`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/cross_join) | `upstream_asset_key` + `upstream_right_key` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_transformations_demo.sh | bash
cd transformations-demo
uv run dg launch --assets '*'
```

## Cost

$0 — entirely local pandas / numpy / rapidfuzz / jsonschema.

## See also

- [`local_nlp.md`](./local_nlp.md) — 13 NLP / lightweight-AI components
- [`data_quality_checks.md`](./data_quality_checks.md) — 4 data quality
  components (companion to [`schema_validator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/schema_validator) and [`audit_columns`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/audit_columns) here)
