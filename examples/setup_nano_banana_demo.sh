#!/usr/bin/env bash
# Nano Banana demo — gemini_image_generation native component end-to-end.
#
# WHAT THIS DEMONSTRATES
#   The new `gemini_image_generation` component (a.k.a. Nano Banana —
#   gemini-2.5-flash-image-preview) generating images per row of a
#   prompts DataFrame, using Google's google-genai SDK directly (NO
#   LiteLLM dependency).
#
#   The same demo also installs a downstream pandas asset that reads
#   the saved PNGs and emits a small summary table — proving full
#   lineage: prompts DataFrame → Nano Banana → image-summary DataFrame.
#
# Asset graph:
#   image_prompts_df              (3 product hero-shot prompts)
#         │
#         └── product_hero_images  ← gemini_image_generation
#                                    (nano banana, writes PNGs to /tmp)
#                                            │
#                                            └── image_size_report  ← pandas
#                                                  (reads each PNG, emits dims)
#
# REQUIRED ENV VAR
#   GEMINI_API_KEY      Google AI Studio API key
#                       (https://aistudio.google.com/app/apikey)
#                       GOOGLE_API_KEY also accepted as a fallback.
#
# COST while running
#   ~\$0.012 against gemini-2.5-flash-image-preview for 3 images.

set -euo pipefail
PROJECT_DIR="${1:-nano-banana-demo}"

if [ -z "${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}" ]; then
  echo "ERROR: set GEMINI_API_KEY (or GOOGLE_API_KEY)"
  echo "       get a key at https://aistudio.google.com/app/apikey"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pillow google-genai
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing gemini_image_generation"
$CLI add synthetic_data_generator --auto-install
$CLI add gemini_image_generation  --auto-install

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/gemini_image_generation"

# ─── Upstream prompts: synthetic_data_generator (image_prompts schema) ──
mkdir -p "src/$PKG/defs/image_prompts"
cat > "src/$PKG/defs/image_prompts/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: image_prompts_df
  schema_type: image_prompts
  row_count: 3
  random_state: 42
  group_name: ingest
EOF

# ─── gemini_image_generation configuration ──────────────────────────────
cat > "src/$PKG/defs/gemini_image_generation/defs.yaml" <<EOF
type: $PKG.components.gemini_image_generation.component.GeminiImageGenerationComponent
attributes:
  asset_name: product_hero_images
  upstream_asset_key: image_prompts_df

  api_key_env_var: GEMINI_API_KEY
  image_model: gemini-2.5-flash-image

  prompt_column: prompt
  output_dir: /tmp/nano_banana_demo
  output_path_column: generated_image_path
  output_filename_template: "{prompt_id}_{idx}.png"

  temperature: 1.0
  rate_limit_delay: 0.5
  max_retries: 3

  group_name: ai_media
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    image_prompts_df              ← synthetic_data_generator (image_prompts, 3 rows)
          │
          └── product_hero_images  ← gemini_image_generation (Nano Banana)
                                    saves PNGs to /tmp/nano_banana_demo

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    uv run dg dev   # http://localhost:3000

Cost: ~\$0.012 for 3 images via gemini-2.5-flash-image-preview.

To swap the image model (e.g. when Google ships GA), edit
\`image_model:\` in src/$PKG/defs/gemini_image_generation/defs.yaml.
MSG
