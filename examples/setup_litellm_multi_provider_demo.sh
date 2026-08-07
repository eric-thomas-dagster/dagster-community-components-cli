#!/usr/bin/env bash
# LiteLLM multi-provider demo — same prompt, 3 providers side-by-side.
#
# WHAT THIS DEMONSTRATES
#   The litellm_inference_asset component running the same prompt
#   through THREE different vendors via one component:
#     - Google Gemini (gemini-2.5-flash) via GEMINI_API_KEY
#     - OpenAI (gpt-4o-mini)            via OPENAI_API_KEY
#     - Anthropic (claude-haiku-4-5)    via ANTHROPIC_API_KEY
#
#   Each provider's response lands in its own column on the same
#   DataFrame — perfect for cost/quality comparison and multi-vendor
#   resilience patterns.
#
# Asset graph:
#   support_tickets             ← synthetic_data_generator (10 rows)
#         │
#         ├── classified_gemini      ← litellm_inference_asset (gemini)
#         ├── classified_openai      ← litellm_inference_asset (gpt-4o-mini)
#         └── classified_anthropic   ← litellm_inference_asset (claude haiku)
#                                       │
#                  └── multi_provider_compare ← pandas (joins all 3) → CSV
#
# REQUIRED ENV VARS (provide as many as you have — others are skipped)
#   GEMINI_API_KEY     (gemini provider)
#   OPENAI_API_KEY     (openai provider)
#   ANTHROPIC_API_KEY  (anthropic provider)
#
# COST while running
#   ~\$0.005 total. 10 short tickets × 3 providers, all on cheapest tier.

set -euo pipefail
PROJECT_DIR="${1:-litellm-multi-provider-demo}"

# Need at least one provider key
if [ -z "${GEMINI_API_KEY:-}${OPENAI_API_KEY:-}${ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: provide at least one of GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas litellm
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing litellm_inference_asset + synthetic_data_generator + dataframe_to_csv"
$CLI add litellm_inference_asset    --auto-install 2>&1 | tail -2
$CLI add synthetic_data_generator   --auto-install 2>&1 | tail -2
$CLI add dataframe_to_csv           --auto-install 2>&1 | tail -2

echo 'from .component import LiteLLMInferenceAssetComponent
__all__ = ["LiteLLMInferenceAssetComponent"]' > "src/$PKG/components/litellm_inference_asset/__init__.py"

# 1) Synthetic upstream — 3 support tickets (enough for side-by-side compare,
#    and stays under Gemini's 20-rpm free-tier when up to 3 providers run)
cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets
  row_count: 3
  random_state: 42
  group_name: ingest
EOF

# Common prompt config
SYS_PROMPT='You are a support-ticket classifier. Output a one-sentence summary under 18 words. No preamble.'
PROMPT_TPL='Ticket: {ticket_text}'

# 2-4) One litellm asset PER provider that has a key in env at scaffold time
mkdir -p "src/$PKG/defs/classified_gemini" "src/$PKG/defs/classified_openai" "src/$PKG/defs/classified_anthropic"

if [ -n "${GEMINI_API_KEY:-}" ]; then
cat > "src/$PKG/defs/classified_gemini/defs.yaml" <<EOF
type: $PKG.components.litellm_inference_asset.component.LiteLLMInferenceAssetComponent
attributes:
  asset_name: classified_gemini
  upstream_asset_key: support_tickets
  model: gemini/gemini-2.5-flash
  api_key_env_var: GEMINI_API_KEY
  system_prompt: "$SYS_PROMPT"
  prompt_template: "$PROMPT_TPL"
  response_column: summary_gemini
  group_name: llm
EOF
else
  rm -rf "src/$PKG/defs/classified_gemini"
fi

if [ -n "${OPENAI_API_KEY:-}" ]; then
cat > "src/$PKG/defs/classified_openai/defs.yaml" <<EOF
type: $PKG.components.litellm_inference_asset.component.LiteLLMInferenceAssetComponent
attributes:
  asset_name: classified_openai
  upstream_asset_key: support_tickets
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  system_prompt: "$SYS_PROMPT"
  prompt_template: "$PROMPT_TPL"
  response_column: summary_openai
  group_name: llm
EOF
else
  rm -rf "src/$PKG/defs/classified_openai"
fi

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
cat > "src/$PKG/defs/classified_anthropic/defs.yaml" <<EOF
type: $PKG.components.litellm_inference_asset.component.LiteLLMInferenceAssetComponent
attributes:
  asset_name: classified_anthropic
  upstream_asset_key: support_tickets
  model: claude-haiku-4-5-20251001
  api_key_env_var: ANTHROPIC_API_KEY
  system_prompt: "$SYS_PROMPT"
  prompt_template: "$PROMPT_TPL"
  response_column: summary_anthropic
  group_name: llm
EOF
else
  rm -rf "src/$PKG/defs/classified_anthropic"
fi

# 5) Side-by-side join via dataframe_join (N-way) — only joins providers that ran.
# Build the upstream_asset_keys list from the providers that have a defs.yaml on disk.
$CLI add dataframe_join --auto-install 2>&1 | tail -2
echo 'from .component import DataframeJoin
__all__ = ["DataframeJoin"]' > "src/$PKG/components/dataframe_join/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/litellm_inference_asset" "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/dataframe_to_csv" "src/$PKG/defs/dataframe_join"

PROVIDERS=()
for prov in gemini openai anthropic; do
  [ -f "src/$PKG/defs/classified_$prov/defs.yaml" ] && PROVIDERS+=("classified_$prov")
done

if [ "${#PROVIDERS[@]}" -ge 2 ]; then
  LEFT="${PROVIDERS[0]}"
  RIGHT="${PROVIDERS[1]}"
  EXTRA_KEYS=""
  for k in "${PROVIDERS[@]:2}"; do EXTRA_KEYS+="    - $k"$'\n'; done

  mkdir -p "src/$PKG/defs/multi_provider_compare"
  cat > "src/$PKG/defs/multi_provider_compare/defs.yaml" <<EOF
type: $PKG.components.dataframe_join.component.DataframeJoin
attributes:
  asset_name: multi_provider_compare
  left_asset_key: $LEFT
  right_asset_key: $RIGHT
$( if [ -n "$EXTRA_KEYS" ]; then echo "  additional_asset_keys:"; echo "$EXTRA_KEYS"; fi )
  how: outer
  on: [ticket_id]
  suffixes: ["", "_dup"]
  group_name: compare
EOF
elif [ "${#PROVIDERS[@]}" -eq 1 ]; then
  echo ">>> Only one provider key set — skipping multi_provider_compare (single-input join is a no-op)."
fi

# 6) CSV sink
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: compare_csv
  upstream_asset_key: multi_provider_compare
  file_path: out/litellm_multi_provider.csv
  include_index: false
  description: Per-ticket summaries from each LiteLLM-routed provider, side by side.
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Asset graph (whichever providers had keys):
    support_tickets                 (10 synthetic tickets)
          │
          ├── classified_gemini       ← litellm_inference_asset (gemini-2.5-flash)
          ├── classified_openai       ← litellm_inference_asset (gpt-4o-mini)
          └── classified_anthropic    ← litellm_inference_asset (claude-haiku-4-5)
                  │
                  └── multi_provider_compare    ← pandas (side-by-side join)
                            │
                            └── compare_csv      ← $PROJECT_ABS/out/litellm_multi_provider.csv

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    cat $PROJECT_ABS/out/litellm_multi_provider.csv
MSG
