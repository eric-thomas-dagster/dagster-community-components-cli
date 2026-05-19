#!/usr/bin/env bash
# X12 EDI Parser demo — synthetic ASC X12 envelopes → flat transaction DataFrame.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   x12_messages          ← synthetic_data_generator (x12_messages, 15 msgs)
#         │
#         └── x12_flat     ← x12_edi_parser (one row per ST/SE transaction)
#
# Covers five common transaction sets: 270, 271, 835, 837, 850.
# Pure Python — no external services.

set -euo pipefail
PROJECT_DIR="${1:-x12-edi-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy tabulate
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator    --auto-install 2>&1 | tail -2
$CLI add x12_edi_parser              --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import X12EdiParserComponent
__all__ = ["X12EdiParserComponent"]' > "src/$PKG/components/x12_edi_parser/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/x12_edi_parser"

# 1) Synthetic X12 EDI messages (rotates through 270/271/835/837/850)
mkdir -p "src/$PKG/defs/x12_messages"
cat > "src/$PKG/defs/x12_messages/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: x12_messages
  schema_type: x12_messages
  row_count: 15
  random_state: 42
  group_name: ingest
EOF

# 2) Parse to flat per-transaction DataFrame
mkdir -p "src/$PKG/defs/x12_flat"
cat > "src/$PKG/defs/x12_flat/defs.yaml" <<EOF
type: $PKG.components.x12_edi_parser.component.X12EdiParserComponent
attributes:
  asset_name: x12_flat
  upstream_asset_key: x12_messages
  message_column: message
  group_name: edi
EOF

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    x12_messages          ← synthetic_data_generator (x12_messages, 15 msgs)
          │
          └── x12_flat     ← x12_edi_parser

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected: 15 rows total — 3 each of 270 (eligibility inquiry),
271 (eligibility response), 835 (remittance), 837 (healthcare claim),
850 (purchase order). Each row has the ISA/GS envelope context
plus transaction-specific fields (claim_total_charge, payment_amount,
po_number, payer_name, subscriber_last_name, etc.).
MSG
