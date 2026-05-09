#!/usr/bin/env bash
# Gemini LLM demo — gemini_llm native component end-to-end.
#
# WHAT THIS DEMONSTRATES
#   The new gemini_llm component (native google-genai, no LiteLLM)
#   running gemini-2.5-flash on a small set of synthetic support
#   tickets — same shape as the anthropic_llm and openai_llm demos
#   so you can compare providers side-by-side.
#
# Asset graph:
#   support_tickets   ← synthetic_data_generator (20 rows)
#         │
#         └── ticket_summaries  ← gemini_llm (gemini-2.5-flash)
#
# REQUIRED ENV VAR
#   GEMINI_API_KEY (or GOOGLE_API_KEY)
#   Get one at https://aistudio.google.com/app/apikey
#
# COST while running
#   Free-tier-friendly. Gemini 2.5 Flash has a generous free quota
#   (typically 15 RPM / 1500/day). 20 rows finish well inside that.

set -euo pipefail
PROJECT_DIR="${1:-gemini-llm-demo}"

if [ -z "${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}" ]; then
  echo "ERROR: set GEMINI_API_KEY (or GOOGLE_API_KEY)"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-genai
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing synthetic_data_generator + gemini_llm"
$CLI add synthetic_data_generator --auto-install
$CLI add gemini_llm               --auto-install

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets
  row_count: 20
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/gemini_llm/defs.yaml" <<EOF
type: $PKG.components.gemini_llm.component.GeminiLLMComponent
attributes:
  asset_name: ticket_summaries
  upstream_asset_key: support_tickets

  api_key_env_var: GEMINI_API_KEY
  text_model: gemini-2.5-flash

  system_prompt: "You are a customer-support analyst. Output a one-sentence summary of each ticket — under 20 words, no preamble."
  input_column: ticket_text
  output_column: summary

  max_output_tokens: 80
  temperature: 0.0
  rate_limit_delay: 0.2
  max_retries: 3

  group_name: ai
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    support_tickets               ← synthetic_data_generator (20 rows)
          │
          └── ticket_summaries    ← gemini_llm (gemini-2.5-flash)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    uv run dg dev   # http://localhost:3000

To swap models, edit src/$PKG/defs/gemini_llm/defs.yaml and change
\`text_model:\` to one of: gemini-2.5-flash (default, fastest/cheapest),
gemini-2.5-pro (most capable), gemini-2.0-flash-lite (cheapest).
MSG
