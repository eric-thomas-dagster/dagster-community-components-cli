#!/usr/bin/env bash
# ACORD Parser demo — synthetic ACORD insurance XML → flat per-entity DataFrame.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   acord_messages    ← synthetic_data_generator (acord_messages, 12 msgs)
#         │
#         └── acord_flat   ← acord_xml_parser (one row per Policy/Claim/Quote)
#
# Pure Python — no external services. Rotates through four ACORD envelopes:
#   InsurancePolicyAddRq, InsurancePolicyChangeRq, ClaimsNotificationRq,
#   InsurancePolicyQuoteInqRq.

set -euo pipefail
PROJECT_DIR="${1:-acord-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy tabulate
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator  --auto-install 2>&1 | tail -1
$CLI add acord_xml_parser          --auto-install 2>&1 | tail -1

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import AcordXmlParserComponent
__all__ = ["AcordXmlParserComponent"]' > "src/$PKG/components/acord_xml_parser/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/acord_xml_parser"

rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/acord_xml_parser"

mkdir -p "src/$PKG/defs/acord_messages"
cat > "src/$PKG/defs/acord_messages/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: acord_messages
  schema_type: acord_messages
  row_count: 12
  random_state: 42
  group_name: ingest
EOF

mkdir -p "src/$PKG/defs/acord_flat"
cat > "src/$PKG/defs/acord_flat/defs.yaml" <<EOF
type: $PKG.components.acord_xml_parser.component.AcordXmlParserComponent
attributes:
  asset_name: acord_flat
  upstream_asset_key: acord_messages
  xml_column: xml
  group_name: insurance
EOF

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    acord_messages    ← synthetic_data_generator (acord_messages, 12 msgs)
          │
          └── acord_flat   ← acord_xml_parser

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected: 12 rows total — 6 Policy + 3 Claim + 3 Quote, rotated across
four ACORD envelopes (InsurancePolicyAddRq, InsurancePolicyChangeRq,
ClaimsNotificationRq, InsurancePolicyQuoteInqRq).
MSG
