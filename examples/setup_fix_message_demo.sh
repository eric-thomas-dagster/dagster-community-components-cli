#!/usr/bin/env bash
# FIX Message Parser demo — synthetic FIX 4.4 trading messages → flat DataFrame.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   fix_messages          ← synthetic_data_generator (fix_messages, 30 msgs)
#         │
#         └── fix_flat     ← fix_message_parser (one row per FIX message)
#
# Generates a mix of NewOrderSingle (D) and ExecutionReport (8) messages.
# Pure Python — no external services.

set -euo pipefail
PROJECT_DIR="${1:-fix-message-demo}"

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
$CLI add fix_message_parser          --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import FixMessageParserComponent
__all__ = ["FixMessageParserComponent"]' > "src/$PKG/components/fix_message_parser/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/fix_message_parser"

# 1) Synthetic FIX 4.4 messages (mix of NewOrderSingle + ExecutionReport)
mkdir -p "src/$PKG/defs/fix_messages"
cat > "src/$PKG/defs/fix_messages/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: fix_messages
  schema_type: fix_messages
  row_count: 30
  random_state: 42
  schema_options:
    delimiter: "|"   # pipe-delimited (canonical SOH x01 is hard to display in pandas)
  group_name: ingest
EOF

# 2) Parse to flat DataFrame — filter to orders + executions only
mkdir -p "src/$PKG/defs/fix_flat"
cat > "src/$PKG/defs/fix_flat/defs.yaml" <<EOF
type: $PKG.components.fix_message_parser.component.FixMessageParserComponent
attributes:
  asset_name: fix_flat
  upstream_asset_key: fix_messages
  message_column: message
  msg_type_filter: [D, 8]    # orders + executions only (8 coerced str)
  group_name: trading
EOF

cat <<MSG

>>> Setup complete (100% components).

Asset graph:
    fix_messages          ← synthetic_data_generator (fix_messages, 30 msgs)
          │
          └── fix_flat     ← fix_message_parser

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected: ~10 NewOrderSingle (D) + ~20 ExecutionReport (8) rows, each with
resolved columns: symbol, side, ord_type, time_in_force, order_qty, price,
last_px, cum_qty, leaves_qty, etc. Full tag dict preserved in tags_raw.
MSG
