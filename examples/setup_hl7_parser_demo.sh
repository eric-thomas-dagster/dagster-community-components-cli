#!/usr/bin/env bash
# HL7 v2 Parser demo — synthetic ADT^A01 + ORU^R01 + ORM^O01 → flat segment DataFrame.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   hl7_messages         ← synthetic_data_generator (hl7_messages, 12 messages)
#         │
#         └── hl7_segments  ← hl7_v2_parser (all 9 supported segments)
#
# Exercises every shipped extractor: MSH, PID, OBX, ORC, OBR, PV1, EVN, DG1, AL1.
# Pure Python — no external services.

set -euo pipefail
PROJECT_DIR="${1:-hl7-parser-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator --auto-install 2>&1 | tail -2
$CLI add hl7_v2_parser            --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import Hl7V2ParserComponent
__all__ = ["Hl7V2ParserComponent"]' > "src/$PKG/components/hl7_v2_parser/__init__.py"

# Remove auto-installed example defs (their asset_names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/hl7_v2_parser"

# 1) Synthetic HL7 messages (alternating ADT^A01 + ORU^R01)
mkdir -p "src/$PKG/defs/hl7_messages"
cat > "src/$PKG/defs/hl7_messages/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: hl7_messages
  schema_type: hl7_messages
  row_count: 12
  random_state: 42
  group_name: ingest
EOF

# 2) Parse into per-segment rows
mkdir -p "src/$PKG/defs/hl7_segments"
cat > "src/$PKG/defs/hl7_segments/defs.yaml" <<EOF
type: $PKG.components.hl7_v2_parser.component.Hl7V2ParserComponent
attributes:
  asset_name: hl7_segments
  upstream_asset_key: hl7_messages
  message_column: message
  keep_segments: [MSH, PID, OBX, ORC, OBR, PV1, EVN, DG1, AL1]
  group_name: healthcare
EOF

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    hl7_messages       ← synthetic_data_generator (hl7_messages, 12 messages)
          │
          └── hl7_segments  ← hl7_v2_parser (all 9 supported segments)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected (4 ADT^A01 + 4 ORU^R01 + 4 ORM^O01):
  MSH ×12 | PID ×12 | OBX ×8 | ORC ×8 | OBR ×8 | PV1 ×4 | EVN ×4 | DG1 ×4 | AL1 ×4
MSG
