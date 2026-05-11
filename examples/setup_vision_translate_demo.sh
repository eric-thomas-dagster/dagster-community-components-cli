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

echo ">>> Installing components"
uvx --from dagster-community-components-cli dagster-component add synthetic_image_generator    --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add vision_api_asset             --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add dataframe_extract_field      --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add translation_api_asset        --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add dataframe_to_csv             --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticImageGeneratorComponent
__all__ = ["SyntheticImageGeneratorComponent"]' > "src/$PKG/components/synthetic_image_generator/__init__.py"
echo 'from .component import VisionApiAssetComponent
__all__ = ["VisionApiAssetComponent"]' > "src/$PKG/components/vision_api_asset/__init__.py"
echo 'from .component import DataframeExtractFieldComponent
__all__ = ["DataframeExtractFieldComponent"]' > "src/$PKG/components/dataframe_extract_field/__init__.py"
echo 'from .component import TranslationApiAssetComponent
__all__ = ["TranslationApiAssetComponent"]' > "src/$PKG/components/translation_api_asset/__init__.py"

# 1) Synthetic image generator (component)
mkdir -p "src/$PKG/defs/sample_images"
cat > "src/$PKG/defs/sample_images/defs.yaml" <<EOF
type: $PKG.components.synthetic_image_generator.component.SyntheticImageGeneratorComponent
attributes:
  asset_name: sample_images
  output_dir: /tmp/vision_translate_imgs
  samples: default
  group_name: ingest
EOF

# 2) Vision API
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

# 3) Extract top vision label via dataframe_extract_field
mkdir -p "src/$PKG/defs/image_with_top_label"
cat > "src/$PKG/defs/image_with_top_label/defs.yaml" <<EOF
type: $PKG.components.dataframe_extract_field.component.DataframeExtractFieldComponent
attributes:
  asset_name: image_with_top_label
  upstream_asset_key: image_analysis
  source_column: vision_labels   # list[{description, score, ...}]
  target_column: top_label
  index: 0
  field: description
  group_name: vision
EOF

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
