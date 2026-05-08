#!/usr/bin/env bash
# Multi-modal AI demo — vision + embeddings via OpenAI APIs.
#
# WHAT THIS DEMONSTRATES
#   Three AI components that aren't covered by ai_with_llm or
#   llm_execution: vision-LLM extraction, vision-LLM captioning, and
#   embedding generation in batch via LiteLLM.
#
# Asset graph:
#   product_images (synthetic source: 5 image URLs from Unsplash)
#         │
#         ├── product_captions      ← image_captioner (gpt-4o-mini vision)
#         └── product_attributes    ← image_llm_extractor (5 fields per image)
#
#   product_descriptions (synthetic source: 10 product blurbs)
#         │
#         └── description_embeddings ← litellm_embedding_batch (text-embedding-3-small)
#
# REQUIRED ENV VARS
#   OPENAI_API_KEY      OpenAI API key (sk-...)
#
# COST
#   ~\$0.05 per run:
#   - 5 image_captioner calls × ~\$0.005 = \$0.025
#   - 5 image_llm_extractor calls × ~\$0.005 = \$0.025
#   - 10 embedding calls (negligible)

set -euo pipefail
PROJECT_DIR="${1:-multimodal-ai-demo}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "ERROR: set OPENAI_API_KEY"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas openai litellm
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 multi-modal components"
$CLI add image_captioner          --auto-install
$CLI add image_llm_extractor      --auto-install
$CLI add litellm_embedding_batch  --auto-install

echo ">>> Writing inline source assets"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg


# 5 small public-domain product images. Stable URLs that don't require auth.
PRODUCT_IMAGE_URLS = [
    "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=400",  # red sneaker
    "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=400",  # headphones
    "https://images.unsplash.com/photo-1546868871-7041f2a55e12?w=400",   # smartwatch
    "https://images.unsplash.com/photo-1572635196237-14b3f281503f?w=400", # sunglasses
    "https://images.unsplash.com/photo-1553062407-98eeb64c6a62?w=400",   # backpack
]


@dg.asset(group_name="ingest", description="5 public-domain product image URLs.")
def product_images() -> pd.DataFrame:
    return pd.DataFrame([
        {"image_id": i, "image_path": url, "source": "unsplash"}
        for i, url in enumerate(PRODUCT_IMAGE_URLS)
    ])


@dg.asset(group_name="ingest", description="10 short product description strings.")
def product_descriptions() -> pd.DataFrame:
    return pd.DataFrame([
        {"product_id": 1, "description": "Wireless noise-cancelling headphones with 30-hour battery life and active sound control."},
        {"product_id": 2, "description": "Lightweight running shoe with mesh upper and responsive foam midsole."},
        {"product_id": 3, "description": "Smart watch with heart-rate monitoring, GPS, and water resistance to 50 meters."},
        {"product_id": 4, "description": "Polarized aviator sunglasses with UV-400 protection and titanium frame."},
        {"product_id": 5, "description": "Waterproof hiking backpack, 35L capacity, with hydration bladder compartment."},
        {"product_id": 6, "description": "Mechanical keyboard with RGB backlight, tactile switches, and aluminum chassis."},
        {"product_id": 7, "description": "Espresso machine with built-in grinder, milk frother, and programmable shots."},
        {"product_id": 8, "description": "Yoga mat made from natural rubber, 6mm thick, non-slip both surfaces."},
        {"product_id": 9, "description": "Bluetooth portable speaker, IP67 waterproof, 24-hour playtime, deep bass."},
        {"product_id": 10, "description": "Cast iron skillet, 12-inch, pre-seasoned, oven-safe to 500°F."},
    ])


defs = dg.Definitions(assets=[product_images, product_descriptions])
PYEOF

echo ">>> Writing 3 multi-modal defs.yaml"

cat > "src/$PKG/defs/image_captioner/defs.yaml" <<EOF
type: $PKG.components.image_captioner.component.ImageCaptionerComponent
attributes:
  asset_name: product_captions
  upstream_asset_key: product_images
  image_path_column: image_path
  output_column: caption
  model: gpt-4o-mini
  prompt: "Describe this product image in one sentence — focus on what it is and key visible features."
  max_tokens: 120
  api_key_env_var: OPENAI_API_KEY
  group_name: vision
EOF

cat > "src/$PKG/defs/image_llm_extractor/defs.yaml" <<EOF
type: $PKG.components.image_llm_extractor.component.ImageLlmExtractorComponent
attributes:
  asset_name: product_attributes
  upstream_asset_key: product_images
  image_column: image_path
  input_type: url
  extraction_fields:
    product_category: "high-level product category (footwear, audio, accessory, etc.)"
    primary_color: "primary color of the product"
    visible_text: "any text or branding visible in the image (or 'none')"
    setting: "where the photo was taken (studio, outdoor, on-body, etc.)"
    estimated_price_range: "rough price tier — budget / mid-range / premium / luxury"
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: vision
EOF

cat > "src/$PKG/defs/litellm_embedding_batch/defs.yaml" <<EOF
type: $PKG.components.litellm_embedding_batch.component.LitellmEmbeddingBatchComponent
attributes:
  asset_name: description_embeddings
  upstream_asset_key: product_descriptions
  text_column: description
  output_column: embedding
  model: text-embedding-3-small
  batch_size: 100
  dimensions: 1536
  api_key_env_var: OPENAI_API_KEY
  group_name: embeddings
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Three calls happen:
  - 5 vision calls per image_captioner (~\$0.025)
  - 5 vision calls per image_llm_extractor (~\$0.025)
  - 1 batch call for 10 embeddings (negligible)
Total: ~\$0.05.

Browse:
    uv run dg dev   # http://localhost:3000
MSG
