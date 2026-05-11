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
PKG="$(ls src/ | head -1)"

uv add -q pandas litellm
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

# 5) Side-by-side join — only joins providers that ran
mkdir -p "src/$PKG/defs/multi_provider_compare"
cat > "src/$PKG/defs/multi_provider_compare/definitions.py" <<'PYEOF'
"""Joins the per-provider summary columns into one row per ticket."""
import pandas as pd
import dagster as dg
from dagster import AssetIn, AssetKey

# Build the ins dict dynamically based on which provider assets exist.
import os
_PKG_NAME = __name__.split(".")[0]
_DEFS_DIR = os.path.dirname(os.path.dirname(__file__))

_AVAILABLE = []
for prov in ("gemini", "openai", "anthropic"):
    if os.path.exists(os.path.join(_DEFS_DIR, f"classified_{prov}", "defs.yaml")):
        _AVAILABLE.append(prov)

_INS = {f"classified_{p}": AssetIn(key=AssetKey([f"classified_{p}"])) for p in _AVAILABLE}


@dg.asset(
    key=dg.AssetKey(["multi_provider_compare"]),
    description="Side-by-side comparison of LiteLLM summaries from each provider on the same tickets.",
    group_name="compare",
    kinds={"pandas"},
    ins=_INS,
)
def multi_provider_compare(**kwargs) -> pd.DataFrame:
    base = None
    for prov in _AVAILABLE:
        df = kwargs[f"classified_{prov}"]
        col = f"summary_{prov}"
        if base is None:
            base = df[["ticket_id", "ticket_text"] + ([col] if col in df.columns else [])].copy()
        else:
            if col in df.columns:
                base = base.merge(df[["ticket_id", col]], on="ticket_id", how="left")
    return base if base is not None else pd.DataFrame()


defs = dg.Definitions(assets=[multi_provider_compare])
PYEOF

# 6) CSV sink
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: compare_csv
  upstream_asset_key: multi_provider_compare
  file_path: /tmp/litellm_multi_provider.csv
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
                            └── compare_csv      ← /tmp/litellm_multi_provider.csv

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    cat /tmp/litellm_multi_provider.csv
MSG
