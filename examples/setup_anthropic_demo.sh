#!/usr/bin/env bash
# Anthropic Claude demo — anthropic_llm component on synthetic tickets.
#
# WHAT THIS DEMONSTRATES
#   The anthropic_llm component running Claude on a small set of
#   synthetic support tickets. Showcases prompt-caching support, the
#   Anthropic-specific batching, and the same canonical input_column /
#   output_column shape used elsewhere in the AI family.
#
# Asset graph:
#   support_tickets (synthetic 20 rows: support requests + complaints)
#         │
#         └── ticket_summaries  ← anthropic_llm (claude-haiku-4-5-20251001)
#
# REQUIRED ENV VAR
#   ANTHROPIC_API_KEY     Claude API key (sk-ant-...)
#
# COST while running
#   ~\$0.01–\$0.05 against claude-haiku-4-5-20251001 for 20 rows.
#   For more advanced (and pricier) Claude models, edit `model:` in the
#   defs.yaml — claude-sonnet-4-6, claude-opus-4-7, etc.

set -euo pipefail
PROJECT_DIR="${1:-anthropic-demo}"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: set ANTHROPIC_API_KEY (sk-ant-...)"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas anthropic
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing anthropic_llm + synthetic_data_generator"
$CLI add synthetic_data_generator --auto-install
$CLI add anthropic_llm            --auto-install

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets
  row_count: 20
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/anthropic_llm/defs.yaml" <<EOF
type: $PKG.components.anthropic_llm.component.AnthropicLLMComponent
attributes:
  asset_name: ticket_summaries
  upstream_asset_key: support_tickets
  api_key: \${ANTHROPIC_API_KEY}
  model: claude-haiku-4-5-20251001
  system_prompt: "You are a customer-support analyst. Output a one-sentence summary of each ticket — under 20 words, no preamble."
  user_prompt_template: "Ticket: {ticket_text}"
  input_column: ticket_text
  output_column: summary
  max_tokens: 100
  temperature: 0.0
  batch_size: 5
  group_name: ai
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

20 ticket summaries via Claude Haiku 3.5 — should complete in 30s, ~\$0.01.

Inspect:
    uv run dg dev   # http://localhost:3000

To use a different Claude model, edit
    src/$PKG/defs/anthropic_llm/defs.yaml
and change \`model:\` to one of:
  - claude-haiku-4-5-20251001   (fastest, cheapest)
  - claude-sonnet-4-6  (default — balanced)
  - claude-opus-4-7      (most capable, priciest)
MSG
