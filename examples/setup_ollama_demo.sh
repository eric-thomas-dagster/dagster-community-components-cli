#!/usr/bin/env bash
# Ollama demo — local LLM inference, zero API cost.
#
# WHAT THIS DEMONSTRATES
#   30 synthetic support tickets classified by a locally-running Ollama
#   model (llama3.2:3b — small, fast, free). No OpenAI key required.
#
# Pipeline:
#   support_tickets (synthetic_data_generator)
#         │
#         └── ollama_inference_asset → category per row (bug/feature/praise/question)
#
# REQUIREMENTS
#   1. Ollama running on http://localhost:11434
#      brew install ollama && ollama serve &
#   2. llama3.2:3b model pulled
#      ollama pull llama3.2:3b
#
# COST while running
#   \$0 — runs entirely on your machine.

set -euo pipefail
PROJECT_DIR="${1:-ollama-demo}"

# Pre-flight: confirm Ollama is reachable.
if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "ERROR: Ollama not reachable at http://localhost:11434"
  echo "Install + start Ollama:"
  echo "  brew install ollama"
  echo "  ollama serve &"
  echo "  ollama pull llama3.2:3b"
  exit 1
fi

# Pre-flight: confirm llama3.2:3b is pulled.
if ! curl -fsS http://localhost:11434/api/tags | grep -q "llama3.2"; then
  echo "ERROR: llama3.2 model not pulled. Run: ollama pull llama3.2:3b"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas requests
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator   --auto-install
$CLI add ollama_inference_asset     --auto-install

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets
  row_count: 30
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/ollama_inference_asset/defs.yaml" <<EOF
type: $PKG.components.ollama_inference_asset.component.OllamaInferenceAssetComponent
attributes:
  asset_name: ticket_categories
  upstream_asset_key: support_tickets
  model: llama3.2:3b
  prompt_template: "Classify the priority of this support ticket as exactly one word — low, medium, high, or urgent. Ticket: {ticket_text}"
  system_prompt: "You are a ticket triage assistant. Respond with only one word: low, medium, high, or urgent. No explanation, no punctuation."
  response_column: priority
  timeout_seconds: 120
  batch_size: 5
  group_name: ai
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

30 tickets × 1 LLM call = 30 calls against your local llama3.2:3b.
Wall-clock: ~1–3 minutes depending on hardware. \$0 cost.

Inspect:
    uv run dg dev   # http://localhost:3000
MSG
