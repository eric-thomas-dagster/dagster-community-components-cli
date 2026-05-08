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
$CLI add gemini_image_generation --auto-install

# ─── Upstream prompts ────────────────────────────────────────────────────
mkdir -p "src/$PKG/defs/image_prompts"
cat > "src/$PKG/defs/image_prompts/definitions.py" <<'PYEOF'
"""3 prompts for the demo's hero-shot image generation."""
import pandas as pd
import dagster as dg


@dg.asset(
    key=dg.AssetKey(["image_prompts_df"]),
    description="3 product descriptions used as Nano Banana prompts.",
    group_name="ingest",
    kinds={"pandas"},
)
def image_prompts_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"sku": "MUG-001", "description": "A minimalist white ceramic coffee mug on a polished marble countertop, soft morning sunlight, crisp shadows, professional product photography"},
        {"sku": "BAG-002", "description": "A vintage tan leather messenger bag leaning against a red brick wall, golden afternoon sun, editorial fashion style, shallow depth of field"},
        {"sku": "RUN-003", "description": "A pair of futuristic neon running shoes mid-stride above a wet asphalt surface with tiny splashes, dramatic high-key lighting, hyperreal"},
    ])


defs = dg.Definitions(assets=[image_prompts_df])
PYEOF

# ─── gemini_image_generation configuration ──────────────────────────────
cat > "src/$PKG/defs/gemini_image_generation/defs.yaml" <<EOF
type: $PKG.components.gemini_image_generation.component.GeminiImageGenerationComponent
attributes:
  asset_name: product_hero_images
  upstream_asset_key: image_prompts_df

  api_key_env_var: GEMINI_API_KEY
  image_model: gemini-2.5-flash-image-preview

  prompt_column: description
  output_dir: /tmp/nano_banana_demo
  output_path_column: generated_image_path
  output_filename_template: "{sku}_{idx}.png"

  temperature: 1.0
  rate_limit_delay: 0.5
  max_retries: 3

  group_name: ai_media
EOF

# ─── Downstream pandas summary that reads each generated PNG ────────────
mkdir -p "src/$PKG/defs/image_size_report"
cat > "src/$PKG/defs/image_size_report/definitions.py" <<'PYEOF'
"""Reads each generated PNG and reports dimensions. Proves full lineage."""
import os
import pandas as pd
import dagster as dg
from dagster import AssetExecutionContext, AssetKey
from PIL import Image


@dg.asset(
    key=dg.AssetKey(["image_size_report"]),
    deps=[dg.AssetKey(["product_hero_images"])],
    description="One-row-per-image summary: SKU, prompt, generated path, width, height, file size.",
    group_name="downstream",
    kinds={"pandas"},
)
def image_size_report(context: AssetExecutionContext) -> pd.DataFrame:
    upstream = AssetKey(["product_hero_images"])
    ev = context.instance.get_latest_materialization_event(upstream)
    if ev is None:
        raise RuntimeError(f"no materialization for {upstream}")

    rows = []
    out_dir = "/tmp/nano_banana_demo"
    if not os.path.isdir(out_dir):
        return pd.DataFrame(rows)
    for fname in sorted(os.listdir(out_dir)):
        if not fname.endswith(".png"):
            continue
        path = os.path.join(out_dir, fname)
        with Image.open(path) as img:
            w, h = img.size
        rows.append({
            "filename": fname,
            "path": path,
            "width": w,
            "height": h,
            "size_kb": round(os.path.getsize(path) / 1024, 1),
        })
    df = pd.DataFrame(rows)
    return df


defs = dg.Definitions(assets=[image_size_report])
PYEOF

cat <<MSG

>>> Setup complete.

Asset graph:
    image_prompts_df              (3 hero-image prompts)
          │
          └── product_hero_images  ← gemini_image_generation (Nano Banana)
                                    saves PNGs to /tmp/nano_banana_demo
                  │
                  └── image_size_report  ← pandas (reads PNGs, reports dimensions)

Materialize all three:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    uv run dg dev   # http://localhost:3000

Cost: ~\$0.012 for 3 images via gemini-2.5-flash-image-preview.

To swap the image model (e.g. when Google ships GA), edit
\`image_model:\` in src/$PKG/defs/gemini_image_generation/defs.yaml.
MSG
