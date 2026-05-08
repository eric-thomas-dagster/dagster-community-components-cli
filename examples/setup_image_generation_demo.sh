#!/usr/bin/env bash
# Image generation demo — litellm_image_generation across multiple backends.
#
# WHAT THIS DEMONSTRATES
#   The litellm_image_generation component generating images per row of a
#   prompts DataFrame. Validated path: DALL-E 3 via OpenAI. The same
#   component supports any LiteLLM-routed image model — including
#   Stability AI, Vertex AI Imagen, Replicate, Bedrock, AWS Titan, and
#   Google's "Nano Banana" (gemini-2.5-flash-image-preview) — by
#   swapping the `model:` field. See the swap snippets at the bottom
#   of this script.
#
# Asset graph:
#   image_prompts_df  (custom asset — 3 product hero-image prompts)
#         │
#         └── product_hero_images  ← litellm_image_generation
#                                    (DALL-E 3 by default)
#
# REQUIRED ENV VAR
#   OPENAI_API_KEY     OpenAI API key (sk-...) for the default DALL-E 3 path.
#                      Swap this for GEMINI_API_KEY / STABILITY_API_KEY /
#                      REPLICATE_API_KEY etc. when changing the `model:`.
#
# COST while running
#   ~\$0.12 against dall-e-3 (\$0.04/standard 1024x1024 image × 3).
#   Switch to dall-e-2 for ~\$0.06 (\$0.02 × 3) if you want it cheaper.

set -euo pipefail
PROJECT_DIR="${1:-image-generation-demo}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "ERROR: set OPENAI_API_KEY (sk-...)"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas litellm openai
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing litellm_image_generation"
$CLI add litellm_image_generation --auto-install

# ─── Upstream prompts (custom asset) ─────────────────────────────────────
mkdir -p "src/$PKG/defs/image_prompts"
cat > "src/$PKG/defs/image_prompts/definitions.py" <<'PYEOF'
"""Three synthetic product hero-image prompts for the demo."""
import pandas as pd
import dagster as dg
from dagster import AssetExecutionContext


@dg.asset(
    key=dg.AssetKey(["image_prompts_df"]),
    description="3 product descriptions for hero-image generation.",
    group_name="ingest",
    kinds={"pandas"},
)
def image_prompts_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"sku": "SKU-001", "description": "A minimalist white ceramic coffee mug on a marble countertop, soft morning light, professional product photography"},
        {"sku": "SKU-002", "description": "A vintage leather messenger bag against a brick wall, warm afternoon sun, editorial style"},
        {"sku": "SKU-003", "description": "A pair of running shoes mid-stride above a wet asphalt surface with subtle splash effects, dramatic lighting"},
    ])


defs = dg.Definitions(assets=[image_prompts_df])
PYEOF

# ─── litellm_image_generation configuration ──────────────────────────────
cat > "src/$PKG/defs/litellm_image_generation/defs.yaml" <<EOF
type: $PKG.components.litellm_image_generation.component.LitellmImageGenerationComponent
attributes:
  asset_name: product_hero_images
  upstream_asset_key: image_prompts_df

  prompt_column: description
  output_column: image_url
  response_format: url

  model: dall-e-3
  size: 1024x1024
  quality: standard
  api_key_env_var: OPENAI_API_KEY

  group_name: ai_media
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    image_prompts_df              (3 product descriptions)
          │
          └── product_hero_images  ← litellm_image_generation (DALL-E 3)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect (image URLs land in the asset's DataFrame, viewable in the UI):
    uv run dg dev   # http://localhost:3000

Cost: ~\$0.12 for 3 standard 1024x1024 images via DALL-E 3.

#  ─── Swap to other backends (LiteLLM-routed) ───
#
# Edit src/$PKG/defs/litellm_image_generation/defs.yaml. The component
# is provider-agnostic — only model: and api_key_env_var: change.
#
# Nano Banana (Google Gemini 2.5 Flash Image):
#   model: gemini/gemini-2.5-flash-image-preview
#   api_key_env_var: GEMINI_API_KEY
#   # (size/quality are ignored by Gemini; LiteLLM passes only the prompt)
#
# Stable Diffusion XL via Stability:
#   model: stability/stable-diffusion-xl-1024-v1-0
#   api_key_env_var: STABILITY_API_KEY
#
# Imagen 3 on Vertex AI:
#   model: vertex_ai/imagen-3.0-generate-001
#   api_key_env_var: GOOGLE_APPLICATION_CREDENTIALS
#
# Replicate (FLUX, SDXL, etc.):
#   model: replicate/black-forest-labs/flux-schnell
#   api_key_env_var: REPLICATE_API_KEY
MSG
