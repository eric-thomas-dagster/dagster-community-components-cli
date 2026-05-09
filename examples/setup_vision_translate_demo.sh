#!/usr/bin/env bash
# Cloud Vision + Translation demo — image labels → translated to 4 languages → CSV.
#
# WHAT THIS DEMONSTRATES
#   Two new GCP ML-API components chained end-to-end:
#     vision_api_asset: detects labels + objects in 3 synthetic images
#     translation_api_asset: translates the labels to es/fr/de/ja
#     dataframe_to_csv: writes the result to /tmp/vision_translate.csv
#
# Asset graph:
#   sample_images       (3 synthetic PNGs in /tmp/vision_translate_imgs/)
#         │
#         └── image_analysis      ← vision_api_asset (LABEL + OBJECT + SAFE_SEARCH)
#                  │
#                  └── analysis_translated   ← translation_api_asset
#                                              (top label → es/fr/de/ja)
#                            │
#                            └── analysis_csv  ← dataframe_to_csv
#
# REQUIRED ENV VAR
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON
#
# COST while running
#   ~\$0.005. 3 images × 3 Vision features = 9 feature calls (\$0.0015 each
#   above the free 1000/mo); 3 strings × 4 langs = 12 translations
#   (\$20/M chars, here ~30 chars total).

set -euo pipefail
PROJECT_DIR="${1:-vision-translate-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pillow google-auth google-cloud-vision google-cloud-translate
uv add --dev -q dagster-dg-cli

echo ">>> Installing vision_api_asset + translation_api_asset + dataframe_to_csv"
uvx --from dagster-community-components-cli dagster-component add vision_api_asset --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add translation_api_asset --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add dataframe_to_csv --auto-install 2>&1 | tail -2

# Fix __init__ files (CLI doesn't always populate them)
echo 'from .component import VisionApiAssetComponent
__all__ = ["VisionApiAssetComponent"]' > "src/$PKG/components/vision_api_asset/__init__.py"
echo 'from .component import TranslationApiAssetComponent
__all__ = ["TranslationApiAssetComponent"]' > "src/$PKG/components/translation_api_asset/__init__.py"

# 1) Synthetic image generator
mkdir -p "src/$PKG/defs/sample_images"
cat > "src/$PKG/defs/sample_images/definitions.py" <<'PYEOF'
"""Generate 3 synthetic PNGs with shapes + colors that Vision can label."""
import os
import pandas as pd
import dagster as dg
from PIL import Image, ImageDraw

_OUT = "/tmp/vision_translate_imgs"


@dg.asset(
    key=dg.AssetKey(["sample_images"]),
    description="3 synthetic PNGs with simple shapes + text for Vision labeling.",
    group_name="ingest",
    kinds={"pillow"},
)
def sample_images() -> pd.DataFrame:
    os.makedirs(_OUT, exist_ok=True)
    images = []

    # Image 1: bright red apple-like circle
    img1 = Image.new("RGB", (320, 320), color=(255, 255, 255))
    d = ImageDraw.Draw(img1)
    d.ellipse([60, 60, 260, 260], fill=(220, 30, 30))
    d.rectangle([155, 30, 165, 70], fill=(80, 50, 30))  # stem
    p1 = os.path.join(_OUT, "apple.png"); img1.save(p1, "PNG")
    images.append({"sku": "FRUIT-1", "name": "apple", "file_path": p1})

    # Image 2: blue car-like rectangle
    img2 = Image.new("RGB", (320, 200), color=(240, 240, 240))
    d = ImageDraw.Draw(img2)
    d.rectangle([30, 80, 290, 150], fill=(40, 80, 200))
    d.ellipse([60, 130, 110, 180], fill=(30, 30, 30))
    d.ellipse([210, 130, 260, 180], fill=(30, 30, 30))
    p2 = os.path.join(_OUT, "car.png"); img2.save(p2, "PNG")
    images.append({"sku": "VEH-1", "name": "blue car", "file_path": p2})

    # Image 3: green leafy plant
    img3 = Image.new("RGB", (320, 320), color=(255, 255, 255))
    d = ImageDraw.Draw(img3)
    d.rectangle([150, 220, 170, 300], fill=(80, 50, 30))   # stem
    d.ellipse([100, 60, 220, 240], fill=(40, 160, 60))      # leaves
    p3 = os.path.join(_OUT, "plant.png"); img3.save(p3, "PNG")
    images.append({"sku": "PLANT-1", "name": "green plant", "file_path": p3})

    return pd.DataFrame(images)


defs = dg.Definitions(assets=[sample_images])
PYEOF

# 2) Vision API config
mkdir -p "src/$PKG/defs/vision_api_asset"
cat > "src/$PKG/defs/vision_api_asset/defs.yaml" <<EOF
type: $PKG.components.vision_api_asset.component.VisionApiAssetComponent
attributes:
  asset_name: image_analysis
  upstream_asset_key: sample_images
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  image_column: file_path
  image_source: path
  features: [LABEL_DETECTION, OBJECT_LOCALIZATION, IMAGE_PROPERTIES]
  max_results: 5
  output_prefix: vision_
  group_name: vision
EOF

# 3) Custom asset that picks the top vision label per row + sends to Translation
mkdir -p "src/$PKG/defs/top_label"
cat > "src/$PKG/defs/top_label/definitions.py" <<'PYEOF'
"""Pick the top Vision label per row and put it in a 'top_label' column."""
import pandas as pd
import dagster as dg
from dagster import AssetIn


@dg.asset(
    key=dg.AssetKey(["image_with_top_label"]),
    description="Adds a top_label column — the highest-confidence Vision label.",
    group_name="vision",
    kinds={"pandas"},
    ins={"image_analysis": AssetIn(key=dg.AssetKey(["image_analysis"]))},
)
def image_with_top_label(image_analysis: pd.DataFrame) -> pd.DataFrame:
    df = image_analysis.copy()
    def _top(labels):
        if not isinstance(labels, list) or not labels:
            return None
        return labels[0]["description"] if isinstance(labels[0], dict) else None
    df["top_label"] = df["vision_labels"].apply(_top)
    return df


defs = dg.Definitions(assets=[image_with_top_label])
PYEOF

# 4) Translation config — translates the top label
mkdir -p "src/$PKG/defs/translation_api_asset"
cat > "src/$PKG/defs/translation_api_asset/defs.yaml" <<EOF
type: $PKG.components.translation_api_asset.component.TranslationApiAssetComponent
attributes:
  asset_name: analysis_translated
  upstream_asset_key: image_with_top_label
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  text_column: top_label
  target_languages: [es, fr, de, ja]
  output_prefix: top_label_
  mime_type: text/plain
  batch_size: 32
  group_name: i18n
EOF

# 5) CSV sink
mkdir -p "src/$PKG/defs/dataframe_to_csv"
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: analysis_csv
  upstream_asset_key: analysis_translated
  file_path: /tmp/vision_translate.csv
  include_index: false
  description: Vision labels + Translation outputs combined.
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    sample_images                (3 synthetic PNGs)
          │
          └── image_analysis              ← vision_api_asset (labels + objects + props)
                    │
                    └── image_with_top_label  ← pandas (pick top label)
                              │
                              └── analysis_translated  ← translation_api_asset (4 languages)
                                        │
                                        └── analysis_csv  ← /tmp/vision_translate.csv

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    cat /tmp/vision_translate.csv
MSG
