#!/usr/bin/env bash
# Text Codec Convert demo — sanitize multilingual ticket text to ASCII.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   support_tickets               ← synthetic_data_generator (multilingual tickets)
#         │
#         └── tickets_ascii_sanitized   ← text_codec_convert_asset (utf-8 → ascii)
#
# Same component handles ANY codec pair Python's stdlib supports:
#   - ASCII <-> EBCDIC (cp037, cp273, cp500, cp1047...)
#   - UTF-8 <-> UTF-16
#   - Windows-1252 -> UTF-8
#   - Latin-1 (ISO-8859-1) <-> UTF-8

set -euo pipefail
PROJECT_DIR="${1:-codec-convert-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator    --auto-install 2>&1 | tail -2
$CLI add text_codec_convert_asset    --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import TextCodecConvertAssetComponent
__all__ = ["TextCodecConvertAssetComponent"]' > "src/$PKG/components/text_codec_convert_asset/__init__.py"

# Remove auto-installed example defs (their upstream refs collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/text_codec_convert_asset"
# 1) Multilingual support tickets (has German Müller, Spanish é/í, em dashes)
mkdir -p "src/$PKG/defs/support_tickets"
cat > "src/$PKG/defs/support_tickets/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets
  row_count: 10
  random_state: 42
  group_name: ingest
EOF

# 2) Sanitize to ASCII (round-trip via codec; replace un-encodable chars with ?)
mkdir -p "src/$PKG/defs/tickets_ascii_sanitized"
cat > "src/$PKG/defs/tickets_ascii_sanitized/defs.yaml" <<EOF
type: $PKG.components.text_codec_convert_asset.component.TextCodecConvertAssetComponent
attributes:
  asset_name: tickets_ascii_sanitized
  upstream_asset_key: support_tickets
  mode: string
  source_column: ticket_text
  target_column: ticket_text_ascii
  from_codec: utf-8    # ignored in string mode (input is already Python str/Unicode)
  to_codec: ascii      # round-trip to ASCII → drops/replaces é, ñ, ü, em-dash, etc.
  errors: replace      # replace non-encodable chars with '?'
  group_name: warehouse
EOF

cat <<MSG

>>> Setup complete (100% components — no custom Python in defs/).

Asset graph:
    support_tickets               ← synthetic_data_generator (10 multilingual tickets)
          │
          └── tickets_ascii_sanitized   ← text_codec_convert_asset (utf-8 → ascii)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected: source has German 'Müller', Spanish 'caído', em-dashes '—';
output replaces them with '?'.

For EBCDIC (z/OS mainframe interop) use:
    from_codec: cp037   # IBM US EBCDIC
    to_codec:   utf-8
    mode:       file
    source_path_column: file_path
MSG
