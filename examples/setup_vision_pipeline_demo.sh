#!/usr/bin/env bash
# Vision pipeline demo — image_metadata_extractor + vision_model end-to-end.
#
# WHAT THIS DEMONSTRATES
#   Two unvalidated AI components running on the same image set:
#     - image_metadata_extractor:  pure PIL — width / height / format / EXIF.
#     - vision_model (Anthropic):  Claude haiku reads each image and
#                                  returns a one-sentence description.
#
#   The demo generates 3 synthetic images (hermetic, no network for the
#   image source) and runs both components on them. Materialization is
#   end-to-end through the asset graph.
#
# Asset graph:
#   sample_images_df (custom asset — generates 3 PNGs, returns DataFrame)
#         │
#         ├── image_metadata  ← image_metadata_extractor (pure PIL)
#         └── image_descriptions ← vision_model (Anthropic Claude haiku)
#
# REQUIRED ENV VAR
#   ANTHROPIC_API_KEY     Claude API key (sk-ant-...)
#
# COST while running
#   ~\$0.005 against claude-haiku-4-5-20251001 for 3 image descriptions.

set -euo pipefail
PROJECT_DIR="${1:-vision-pipeline-demo}"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: set ANTHROPIC_API_KEY (sk-ant-...)"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pillow anthropic openai
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing image_metadata_extractor + vision_model"
$CLI add image_metadata_extractor --auto-install
$CLI add vision_model              --auto-install

# ─── Synthetic image generator (custom asset, returns DataFrame) ──────────
mkdir -p "src/$PKG/defs/sample_images"
cat > "src/$PKG/defs/sample_images/definitions.py" <<'PYEOF'
"""Generate 3 synthetic PNG images and yield a DataFrame of file paths.

This sidesteps the need to pull from a public CDN and keeps the demo
hermetic. Each image is a solid colored rectangle with text — enough
shape for the metadata extractor and a plausibly-describable scene
for the vision model.
"""
import os
import pandas as pd
import dagster as dg
from PIL import Image, ImageDraw, ImageFont

_OUT_DIR = "/tmp/vision_pipeline_images"


def _make_image(path: str, color: tuple, label: str) -> None:
    img = Image.new("RGB", (320, 240), color=color)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=28)
    except Exception:
        font = ImageFont.load_default()
    draw.text((20, 100), label, fill="white", font=font)
    img.save(path, "PNG")


@dg.asset(
    key=dg.AssetKey(["sample_images_df"]),
    description="Generates 3 synthetic 320x240 PNG images and returns a DataFrame of paths.",
    group_name="ingest",
    kinds={"pillow"},
)
def sample_images_df() -> pd.DataFrame:
    os.makedirs(_OUT_DIR, exist_ok=True)
    images = [
        ("red_apple",  (200, 30, 30),  "Red Apple"),
        ("blue_car",   (40, 80, 200),  "Blue Car"),
        ("green_tree", (40, 160, 60),  "Green Tree"),
    ]
    rows = []
    for name, color, label in images:
        path = os.path.join(_OUT_DIR, f"{name}.png")
        _make_image(path, color, label)
        rows.append({"file_path": path, "name": name, "label": label})
    return pd.DataFrame(rows)


defs = dg.Definitions(assets=[sample_images_df])
PYEOF

# ─── image_metadata_extractor configuration ──────────────────────────────
cat > "src/$PKG/defs/image_metadata_extractor/defs.yaml" <<EOF
type: $PKG.components.image_metadata_extractor.component.ImageMetadataExtractorComponent
attributes:
  asset_name: image_metadata
  upstream_asset_key: sample_images_df

  image_column: file_path
  extract_exif: true
  extract_gps: false
  output_prefix: img_
  include_histogram: false

  group_name: vision
EOF

# ─── vision_model configuration (Anthropic Claude) ───────────────────────
cat > "src/$PKG/defs/vision_model/defs.yaml" <<EOF
type: $PKG.components.vision_model.component.VisionModelComponent
attributes:
  asset_name: image_descriptions
  upstream_asset_key: sample_images_df

  provider: anthropic
  model: claude-haiku-4-5-20251001
  api_key: \${ANTHROPIC_API_KEY}

  prompt: "Describe this image in one short sentence — what's in it and what color is it?"

  image_column: file_path
  image_type: path
  detail_level: high
  max_images_per_request: 1

  output_column: description

  temperature: 0.0
  max_tokens: 80

  batch_size: 1
  rate_limit_delay: 0.2
  max_retries: 2
  track_costs: true

  description: Claude haiku image descriptions for the 3 synthetic images.
  group_name: vision
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    sample_images_df          (3 synthetic PNGs in /tmp/vision_pipeline_images)
          │
          ├── image_metadata      ← image_metadata_extractor (PIL)
          └── image_descriptions  ← vision_model (Anthropic Claude haiku)

Materialize all three:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    uv run dg dev   # http://localhost:3000

Cost: ~\$0.005 for 3 images via claude-haiku-4-5-20251001.
MSG
