#!/usr/bin/env bash
# ISO 20022 Parser demo — synthetic pacs.008 + pacs.002 XML → flat transactions DataFrame.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   iso20022_messages       ← synthetic_data_generator (iso20022_payments, 10 msgs)
#         │
#         └── payments_flat   ← iso20022_payment_parser (per-transaction rows)
#
# Pure Python — no external services. Demonstrates the canonical
# treasury / payments ingest pattern.

set -euo pipefail
PROJECT_DIR="${1:-iso20022-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator    --auto-install 2>&1 | tail -2
$CLI add iso20022_payment_parser     --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import Iso20022PaymentParserComponent
__all__ = ["Iso20022PaymentParserComponent"]' > "src/$PKG/components/iso20022_payment_parser/__init__.py"

# 1) Synthetic ISO 20022 messages (alternating pacs.008 + pacs.002)
mkdir -p "src/$PKG/defs/iso20022_messages"
cat > "src/$PKG/defs/iso20022_messages/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: iso20022_messages
  schema_type: iso20022_payments
  row_count: 10
  random_state: 42
  group_name: ingest
EOF

# 2) Parse to flat per-transaction DataFrame
mkdir -p "src/$PKG/defs/payments_flat"
cat > "src/$PKG/defs/payments_flat/defs.yaml" <<EOF
type: $PKG.components.iso20022_payment_parser.component.Iso20022PaymentParserComponent
attributes:
  asset_name: payments_flat
  upstream_asset_key: iso20022_messages
  xml_column: xml
  group_name: payments
EOF

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    iso20022_messages       ← synthetic_data_generator (iso20022_payments, 10 msgs)
          │
          └── payments_flat   ← iso20022_payment_parser

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected: 5 pacs.008 (credit transfer) rows + 5 pacs.002 (status report) rows.
MSG
