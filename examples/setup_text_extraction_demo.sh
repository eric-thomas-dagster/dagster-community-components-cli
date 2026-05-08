#!/usr/bin/env bash
# Text extraction demo — 6 transforms that pull structured fields from
# semi-structured text columns.
#
# WHAT THIS DEMONSTRATES
#   The text-extraction transform family, all of which take a DataFrame
#   with a text/JSON/XML/HTML column and emit one or more new columns
#   with parsed values. Pure local; no external services.
#
# Asset graph:
#   raw_events (synthetic, mixed text-shaped columns)
#         │
#         ├── flattened_addresses     ← json_flatten
#         ├── extracted_user_fields   ← json_path_extractor
#         ├── extracted_address_parts ← nested_field_extractor
#         ├── parsed_product_xml      ← xml_parser
#         ├── parsed_html_content     ← html_parser
#         └── extracted_phone_parts   ← regex_parser
#
# COST: \$0 — fully local, pandas + stdlib only.

set -euo pipefail
PROJECT_DIR="${1:-text-extraction-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas beautifulsoup4 lxml
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 text-extraction transforms"
$CLI add json_flatten            --auto-install
$CLI add json_path_extractor     --auto-install
$CLI add nested_field_extractor  --auto-install
$CLI add xml_parser              --auto-install
$CLI add html_parser             --auto-install
$CLI add regex_parser            --auto-install

echo ">>> Writing inline source asset"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import json
import pandas as pd
import dagster as dg


@dg.asset(group_name="ingest", description="Synthetic events with mixed nested-text columns.")
def raw_events() -> pd.DataFrame:
    rows = []
    for i in range(20):
        addr = {
            "address": {
                "street": f"{100 + i} Main St",
                "city": ["Brooklyn", "SF", "Austin"][i % 3],
                "zip": f"{10000 + i}",
                "country": "US",
            }
        }
        rows.append({
            "user_id": 1000 + i,
            # JSON column for json_flatten — nested up to ~3 levels
            "address_json": json.dumps(addr),
            # JSON column for json_path_extractor — payload with $.user.id
            "payload": json.dumps({
                "user": {"id": 1000 + i, "email": f"user{i}@example.com"},
                "meta": {"tags": ["alpha", "beta"][i % 2 :]},
            }),
            # nested_field_extractor likes a dict-typed column (already-parsed)
            "customer_data": addr,
            # xml_parser
            "xml_data": f"<product><sku>SKU-{i:04d}</sku><name>Widget {i}</name><price>{19.99 + i}</price></product>",
            # html_parser
            "body": f"<p>Welcome {i}!</p><b>Bold</b> <i>italic</i>.",
            "description": f"<div>desc {i}</div>",
            # regex_parser
            "phone_number": f"{415 + i:03d}-555-{1000 + i:04d}",
        })
    return pd.DataFrame(rows)


defs = dg.Definitions(assets=[raw_events])
PYEOF

echo ">>> Writing 6 text-extraction defs.yaml"

cat > "src/$PKG/defs/json_flatten/defs.yaml" <<EOF
type: $PKG.components.json_flatten.component.JsonFlattenComponent
attributes:
  asset_name: flattened_addresses
  upstream_asset_key: raw_events
  column: address_json
  separator: "."
  max_depth: 3
  drop_original: true
  group_name: extracted
EOF

cat > "src/$PKG/defs/json_path_extractor/defs.yaml" <<EOF
type: $PKG.components.json_path_extractor.component.JsonPathExtractorComponent
attributes:
  asset_name: extracted_user_fields
  upstream_asset_key: raw_events
  source_column: payload
  extractions:
    user_id: "\$.user.id"
    user_email: "\$.user.email"
  drop_source: false
  group_name: extracted
EOF

cat > "src/$PKG/defs/nested_field_extractor/defs.yaml" <<EOF
type: $PKG.components.nested_field_extractor.component.NestedFieldExtractorComponent
attributes:
  asset_name: extracted_address_parts
  upstream_asset_key: raw_events
  source_column: customer_data
  extractions:
    city: "address.city"
    zip: "address.zip"
    country: "address.country"
  drop_source: false
  group_name: extracted
EOF

cat > "src/$PKG/defs/xml_parser/defs.yaml" <<EOF
type: $PKG.components.xml_parser.component.XmlParser
attributes:
  asset_name: parsed_product_xml
  upstream_asset_key: raw_events
  xml_column: xml_data
  xpath_expressions:
    product_name: ".//name/text()"
    price: ".//price/text()"
    sku: ".//sku/text()"
  group_name: extracted
EOF

cat > "src/$PKG/defs/html_parser/defs.yaml" <<EOF
type: $PKG.components.html_parser.component.HtmlParserComponent
attributes:
  asset_name: parsed_html_content
  upstream_asset_key: raw_events
  columns: [body, description]
  mode: strip_tags
  parser: html.parser
  new_column_suffix: _text
  group_name: extracted
EOF

cat > "src/$PKG/defs/regex_parser/defs.yaml" <<EOF
type: $PKG.components.regex_parser.component.RegexParser
attributes:
  asset_name: extracted_phone_parts
  upstream_asset_key: raw_events
  column: phone_number
  pattern: "^(\\\\d{3})-(\\\\d{3})-(\\\\d{4})\$"
  mode: extract
  output_columns: [area_code, exchange, subscriber]
  group_name: extracted
EOF

cat <<MSG

>>> Setup complete.

Materialize the source + 6 extractions:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Browse:
    uv run dg dev   # http://localhost:3000

Each extraction asset shows up in the 'extracted' group. The output
DataFrames have the parsed fields as new columns — usable downstream as
inputs to a sink, model, or further transformation.
MSG
