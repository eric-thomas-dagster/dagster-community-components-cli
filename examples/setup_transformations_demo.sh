#!/usr/bin/env bash
# Transformations mega-demo — 37 pure-pandas transformation components.
# No API keys, no external services. Synthetic source data fans out to
# all 37 transformations in parallel.
#
# COST: $0 — purely local

set -euo pipefail
PROJECT_DIR="${1:-transformations-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy jsonschema rapidfuzz duckdb duckdb-engine sqlalchemy
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 37 transformation components"
for c in append_fields arrange auto_field count_records cross_tab \
         dataframe_transformer document_merger email_parser field_mapper \
         file_transformer find_replace fuzzy_match generate_rows \
         label_encoder make_columns make_group markdown_stripper \
         multi_field_binning multi_field_formula multi_row_formula \
         record_id sample schema_validator select_records sql_transform \
         text_to_columns train_test_splitter weighted_average \
         siem_event_normalizer scd_type_1 lookup surrogate_key hash \
         map_values cross_join audit_columns data_masking; do
  $CLI add $c --auto-install || echo "FAILED: $c"
done

echo ">>> Writing inline source data assets"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import os
import pandas as pd
import dagster as dg

# DuckDB SQLAlchemy URL for sql_transform component
os.environ.setdefault("SQL_DB_URL", "duckdb:////tmp/transformations_demo.duckdb")


@dg.asset(group_name="ingest")
def orders() -> pd.DataFrame:
    """Main source DataFrame — rich schema covering many transformation needs."""
    return pd.DataFrame({
        "order_id": list(range(1001, 1031)),
        "customer_id": [101, 102, 103, 101, 104, 102, 105, 106, 107, 101] * 3,
        "customer_name": ["Alice Smith", "Bob Jones", "Carol Davis", "Alice Smith",
                          "David Wilson", "Bob Jones", "Eve Martinez", "Frank Brown",
                          "Grace Lee", "Alice Smith"] * 3,
        "email": ["alice@acme.com", "bob.jones@globex.io", "carol.davis@initech.org",
                  "alice@acme.com", "dwilson@umbrella.co", "bob.jones@globex.io",
                  "eve@stark.com", "frank.brown@oscorp.net", "grace.lee@waynecorp.com",
                  "alice@acme.com"] * 3,
        "product": ["Widget A", "Widget B", "Gadget X", "Widget A", "Sprocket",
                    "Widget B", "Doohickey", "Thingamajig", "Whatzit", "Widget A"] * 3,
        "category": ["widgets", "widgets", "gadgets", "widgets", "sprockets",
                     "widgets", "misc", "misc", "misc", "widgets"] * 3,
        "quantity": [2, 1, 3, 2, 5, 1, 4, 2, 1, 3] * 3,
        "unit_price": [29.99, 39.99, 49.99, 29.99, 19.99, 39.99, 9.99, 14.99, 24.99, 29.99] * 3,
        "discount": [0.0, 0.1, 0.0, 0.05, 0.0, 0.0, 0.15, 0.0, 0.0, 0.1] * 3,
        "status": ["shipped", "pending", "shipped", "cancelled", "shipped",
                   "pending", "shipped", "shipped", "pending", "cancelled"] * 3,
        "description": ["**Heavy Duty** widget for *industrial* use",
                        "Lightweight option — see [docs](https://example.com)",
                        "Premium gadget with `metal` finish",
                        "**Heavy Duty** widget for *industrial* use",
                        "Standard sprocket, see specs",
                        "Lightweight option — see [docs](https://example.com)",
                        "Cheap doohickey", "Decent thingamajig",
                        "Whatzit description", "Heavy duty widget"] * 3,
        "raw_csv": ["red,large,cotton", "blue,medium,wool", "green,small,silk",
                    "red,large,cotton", "yellow,medium,linen", "blue,medium,wool",
                    "purple,small,nylon", "white,large,polyester", "black,medium,fleece",
                    "red,large,cotton"] * 3,
        "order_date": pd.date_range("2025-01-01", periods=30, freq="D"),
        "raw_event": [
            '{"timestamp": "2025-01-01T10:00:00Z", "src_ip": "192.168.1.1", "action": "login"}',
        ] * 30,
    })


@dg.asset(group_name="ingest")
def category_lookup() -> pd.DataFrame:
    """Lookup table for category enrichment."""
    return pd.DataFrame({
        "category": ["widgets", "gadgets", "sprockets", "misc"],
        "category_label": ["Widget Family", "Gadget Family", "Sprocket Line", "Miscellaneous"],
        "department": ["Hardware", "Hardware", "Hardware", "Other"],
    })


@dg.asset(group_name="ingest")
def products_dim() -> pd.DataFrame:
    """Dimension table for joins."""
    return pd.DataFrame({
        "product": ["Widget A", "Widget B", "Gadget X", "Sprocket",
                    "Doohickey", "Thingamajig", "Whatzit"],
        "supplier_id": ["SUP001", "SUP002", "SUP003", "SUP001", "SUP004", "SUP005", "SUP006"],
        "weight_kg": [0.5, 0.7, 1.2, 0.3, 0.1, 0.2, 0.4],
    })


@dg.asset(group_name="ingest")
def orders_dim_existing() -> pd.DataFrame:
    """Existing dim table for SCD Type 1 demo."""
    return pd.DataFrame({
        "customer_id": [101, 102, 103],
        "customer_name": ["Alice S.", "Bob J.", "Carol D."],
        "email": ["alice.old@acme.com", "bob@globex.io", "carol@initech.org"],
    })


@dg.asset(group_name="ingest")
def orders_csv_file() -> str:
    """Write a CSV file to disk for file_transformer to read."""
    out = "/tmp/transformations_demo_orders.csv"
    pd.DataFrame({
        "order_id": [1, 2, 3, 4, 5],
        "customer": ["Alice", "Bob", "Carol", "Dave", "Eve"],
        "amount": [100.0, 200.0, 300.0, 400.0, 500.0],
    }).to_csv(out, index=False)
    return out


@dg.asset(group_name="ingest", deps=[dg.AssetKey("orders")])
def orders_in_duckdb(orders: pd.DataFrame) -> str:
    """Load orders into DuckDB so sql_transform can query it."""
    import sqlalchemy
    engine = sqlalchemy.create_engine(os.environ["SQL_DB_URL"])
    orders.to_sql("orders", engine, if_exists="replace", index=False)
    return os.environ["SQL_DB_URL"]


defs = dg.Definitions(assets=[orders, category_lookup, products_dim,
                              orders_dim_existing, orders_csv_file, orders_in_duckdb])
PYEOF

echo ">>> Writing 37 transformation defs.yaml"

write_yaml() {
  local d="$1"; local body="$2"
  mkdir -p "src/$PKG/defs/$d"
  echo -e "$body" > "src/$PKG/defs/$d/defs.yaml"
}

# 1. arrange (sort)
write_yaml "arrange" "type: $PKG.components.arrange.component.ArrangeComponent
attributes:
  asset_name: orders_arranged
  upstream_asset_key: orders
  move_to_front: [order_id, customer_id]
  rename:
    unit_price: price
  group_name: transforms"

# 2. auto_field (infer types)
write_yaml "auto_field" "type: $PKG.components.auto_field.component.AutoFieldComponent
attributes:
  asset_name: orders_typed
  upstream_asset_key: orders
  group_name: transforms"

# 3. count_records
write_yaml "count_records" "type: $PKG.components.count_records.component.CountRecordsComponent
attributes:
  asset_name: orders_count
  upstream_asset_key: orders
  group_name: transforms"

# 4. cross_tab
write_yaml "cross_tab" "type: $PKG.components.cross_tab.component.CrossTabComponent
attributes:
  asset_name: orders_crosstab
  upstream_asset_key: orders
  index_column: category
  pivot_column: status
  value_column: quantity
  agg_func: sum
  group_name: transforms"

# 5. dataframe_transformer (passthrough by default)
write_yaml "dataframe_transformer" "type: $PKG.components.dataframe_transformer.component.DataFrameTransformerComponent
attributes:
  asset_name: orders_passthru
  upstream_asset_key: orders
  group_name: transforms"

# 6. document_merger
write_yaml "document_merger" "type: $PKG.components.document_merger.component.DocumentMergerComponent
attributes:
  asset_name: orders_with_supplier
  left_asset_key: orders
  right_asset_key: products_dim
  \"on\": product
  how: left
  suffixes: [_l, _r]
  group_name: transforms"

# 7. email_parser
write_yaml "email_parser" "type: $PKG.components.email_parser.component.EmailParserComponent
attributes:
  asset_name: orders_email_parsed
  upstream_asset_key: orders
  column: email
  extract_fields: [user, domain, tld]
  group_name: transforms"

# 8. field_mapper
write_yaml "field_mapper" "type: $PKG.components.field_mapper.component.FieldMapperComponent
attributes:
  asset_name: orders_renamed
  upstream_asset_key: orders
  mapping:
    customer_id: cust_id
    unit_price: price
    quantity: qty
  group_name: transforms"

# 9. file_transformer (skip — needs filesystem; just give it harmless config)
write_yaml "file_transformer" "type: $PKG.components.file_transformer.component.FileTransformerComponent
attributes:
  asset_name: orders_files
  upstream_asset_key: orders
  output_format: parquet
  output_directory: /tmp/transformations_demo_out
  group_name: transforms"

# 10. find_replace
write_yaml "find_replace" "type: $PKG.components.find_replace.component.FindReplace
attributes:
  asset_name: orders_replaced
  upstream_asset_key: orders
  lookup_asset_key: category_lookup
  lookup_key_column: category
  lookup_value_column: category_label
  target_column: category
  group_name: transforms"

# 11. fuzzy_match (against products_dim)
write_yaml "fuzzy_match" "type: $PKG.components.fuzzy_match.component.FuzzyMatch
attributes:
  asset_name: orders_fuzzy
  upstream_asset_key: orders
  column: product
  group_name: transforms"

# 12. generate_rows (passthrough?)
write_yaml "generate_rows" "type: $PKG.components.generate_rows.component.GenerateRowsComponent
attributes:
  asset_name: orders_generated
  upstream_asset_key: orders
  group_name: transforms"

# 13. label_encoder
write_yaml "label_encoder" "type: $PKG.components.label_encoder.component.LabelEncoderComponent
attributes:
  asset_name: orders_encoded
  upstream_asset_key: orders
  columns: [category, status]
  group_name: transforms"

# 14. make_columns
write_yaml "make_columns" "type: $PKG.components.make_columns.component.MakeColumnsComponent
attributes:
  asset_name: orders_pivot_cols
  upstream_asset_key: orders
  value_column: quantity
  pivot_column: status
  group_name: transforms"

# 15. make_group
write_yaml "make_group" "type: $PKG.components.make_group.component.MakeGroupComponent
attributes:
  asset_name: orders_grouped
  upstream_asset_key: orders
  key_columns: [customer_id, status]
  group_name: transforms"

# 16. markdown_stripper
write_yaml "markdown_stripper" "type: $PKG.components.markdown_stripper.component.MarkdownStripperComponent
attributes:
  asset_name: orders_stripped
  upstream_asset_key: orders
  columns: [description]
  group_name: transforms"

# 17. multi_field_binning
write_yaml "multi_field_binning" "type: $PKG.components.multi_field_binning.component.MultiFieldBinningComponent
attributes:
  asset_name: orders_binned
  upstream_asset_key: orders
  columns: [unit_price, quantity]
  n_bins: 5
  group_name: transforms"

# 18. multi_field_formula
write_yaml "multi_field_formula" "type: $PKG.components.multi_field_formula.component.MultiFieldFormulaComponent
attributes:
  asset_name: orders_formula
  upstream_asset_key: orders
  expression: '{col} * 1.1'
  columns: [unit_price]
  output_suffix: '_with_tax'
  group_name: transforms"

# 19. multi_row_formula
write_yaml "multi_row_formula" "type: $PKG.components.multi_row_formula.component.MultiRowFormulaComponent
attributes:
  asset_name: orders_row_formula
  upstream_asset_key: orders
  operations:
    - output_column: quantity_lag1
      column: quantity
      operation: lag
      periods: 1
    - output_column: rolling_qty_3
      column: quantity
      operation: rolling_mean
      window: 3
  group_name: transforms"

# 20. record_id
write_yaml "record_id" "type: $PKG.components.record_id.component.RecordIdComponent
attributes:
  asset_name: orders_with_id
  upstream_asset_key: orders
  group_name: transforms"

# 21. sample
write_yaml "sample" "type: $PKG.components.sample.component.SampleComponent
attributes:
  asset_name: orders_sampled
  upstream_asset_key: orders
  sample_size: 10
  group_name: transforms"

# 22. schema_validator
write_yaml "schema_validator" "type: $PKG.components.schema_validator.component.SchemaValidatorComponent
attributes:
  asset_name: orders_validated
  upstream_asset_key: orders
  json_schema:
    type: object
    properties:
      order_id: {type: integer}
      customer_id: {type: integer}
      unit_price: {type: number}
    required: [order_id, customer_id]
  on_invalid: tag
  tag_column: _validation
  group_name: transforms"

# 23. select_records
write_yaml "select_records" "type: $PKG.components.select_records.component.SelectRecordsComponent
attributes:
  asset_name: orders_selected
  upstream_asset_key: orders
  filter_expression: 'unit_price > 20'
  group_name: transforms"

# 24. sql_transform (DuckDB)
write_yaml "sql_transform" "type: $PKG.components.sql_transform.component.SqlTransformComponent
attributes:
  asset_name: orders_sql
  connection_url_env_var: SQL_DB_URL
  destination_table: orders_sql
  sql: 'SELECT category, SUM(quantity) AS total_qty FROM orders GROUP BY category'
  group_name: transforms"

# 25. text_to_columns
write_yaml "text_to_columns" "type: $PKG.components.text_to_columns.component.TextToColumns
attributes:
  asset_name: orders_split
  upstream_asset_key: orders
  column: raw_csv
  separator: ','
  output_columns: [color, size, fabric]
  group_name: transforms"

# 26. train_test_splitter
write_yaml "train_test_splitter" "type: $PKG.components.train_test_splitter.component.TrainTestSplitterComponent
attributes:
  asset_name: orders_split_set
  upstream_asset_key: orders
  test_size: 0.2
  random_state: 42
  group_name: transforms"

# 27. weighted_average
write_yaml "weighted_average" "type: $PKG.components.weighted_average.component.WeightedAverageComponent
attributes:
  asset_name: orders_wavg
  upstream_asset_key: orders
  value_column: unit_price
  weight_column: quantity
  group_by: [category]
  group_name: transforms"

# 28. siem_event_normalizer (operates on raw_event JSON)
write_yaml "siem_event_normalizer" "type: $PKG.components.siem_event_normalizer.component.SiemEventNormalizerComponent
attributes:
  asset_name: events_normalized
  upstream_asset_key: orders
  event_column: raw_event
  group_name: transforms"

# 29. scd_type_1
write_yaml "scd_type_1" "type: $PKG.components.scd_type_1.component.ScdType1Component
attributes:
  asset_name: orders_scd1
  upstream_asset_key: orders
  upstream_target_key: orders_dim_existing
  business_key_columns: [customer_id]
  group_name: transforms"

# 30. lookup
write_yaml "lookup" "type: $PKG.components.lookup.component.LookupComponent
attributes:
  asset_name: orders_lookup
  upstream_asset_key: orders
  upstream_lookup_key: category_lookup
  \"on\": [category]
  group_name: transforms"

# 31. surrogate_key
write_yaml "surrogate_key" "type: $PKG.components.surrogate_key.component.SurrogateKeyComponent
attributes:
  asset_name: orders_surrogate
  upstream_asset_key: orders
  business_key_columns: [order_id]
  group_name: transforms"

# 32. hash
write_yaml "hash" "type: $PKG.components.hash.component.HashComponent
attributes:
  asset_name: orders_hashed
  upstream_asset_key: orders
  columns: [email, customer_name]
  algorithm: sha256
  group_name: transforms"

# 33. map_values
write_yaml "map_values" "type: $PKG.components.map_values.component.MapValuesComponent
attributes:
  asset_name: orders_mapped
  upstream_asset_key: orders
  column: status
  mapping:
    pending: P
    shipped: S
    cancelled: X
  group_name: transforms"

# 34. cross_join
write_yaml "cross_join" "type: $PKG.components.cross_join.component.CrossJoinComponent
attributes:
  asset_name: orders_crossed
  upstream_asset_key: products_dim
  upstream_right_key: orders_dim_existing
  suffixes: [_l, _r]
  group_name: transforms"

# 35. audit_columns
write_yaml "audit_columns" "type: $PKG.components.audit_columns.component.AuditColumnsComponent
attributes:
  asset_name: orders_audit
  upstream_asset_key: orders
  group_name: transforms"

# 36. data_masking
write_yaml "data_masking" "type: $PKG.components.data_masking.component.DataMaskingComponent
attributes:
  asset_name: orders_masked
  upstream_asset_key: orders
  rules:
    - column: email
      method: hash
    - column: customer_name
      method: redact
  group_name: transforms"

# 37. append_fields
write_yaml "append_fields" "type: $PKG.components.append_fields.component.AppendFields
attributes:
  asset_name: orders_appended
  upstream_asset_key: orders
  source_asset_key: products_dim
  group_name: transforms"

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

37 transformations on 30 synthetic orders + 3 small lookup tables.
\$0 cost — all local.

Inspect:
    uv run dg dev   # http://localhost:3000
MSG
