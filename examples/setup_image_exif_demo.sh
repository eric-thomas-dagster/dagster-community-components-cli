#!/usr/bin/env bash
# Image EXIF demo — synthetic JPEGs with EXIF metadata → flat DataFrame.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   sample_images       ← synthetic_image_generator (3 JPEGs with injected EXIF)
#         │
#         └── image_metadata  ← image_exif_extractor (camera, GPS, capture settings)
#
# Demonstrates the canonical PII / compliance pattern: detect that
# user-uploaded photos carry GPS coordinates BEFORE publishing them.

set -euo pipefail
PROJECT_DIR="${1:-image-exif-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pillow piexif
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_image_generator  --auto-install 2>&1 | tail -2
$CLI add image_exif_extractor       --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticImageGeneratorComponent
__all__ = ["SyntheticImageGeneratorComponent"]' > "src/$PKG/components/synthetic_image_generator/__init__.py"
echo 'from .component import ImageExifExtractorComponent
__all__ = ["ImageExifExtractorComponent"]' > "src/$PKG/components/image_exif_extractor/__init__.py"

# 1) Synthetic JPEGs with injected EXIF (Make / Model / ISO / GPS)
mkdir -p "src/$PKG/defs/sample_images"
cat > "src/$PKG/defs/sample_images/defs.yaml" <<EOF
type: $PKG.components.synthetic_image_generator.component.SyntheticImageGeneratorComponent
attributes:
  asset_name: sample_images
  output_dir: /tmp/image_exif_demo_in
  samples: default
  width: 640
  height: 480
  inject_exif: true
  exif_make: DagsterCam
  exif_model: DG-1
  exif_gps_lat: 37.7749
  exif_gps_lon: -122.4194
  group_name: ingest
EOF

# 2) EXIF extractor
mkdir -p "src/$PKG/defs/image_metadata"
cat > "src/$PKG/defs/image_metadata/defs.yaml" <<EOF
type: $PKG.components.image_exif_extractor.component.ImageExifExtractorComponent
attributes:
  asset_name: image_metadata
  upstream_asset_key: sample_images
  image_path_column: file_path
  group_name: media
EOF

cat <<MSG

>>> Setup complete (100% components — no custom Python in defs/).

Asset graph:
    sample_images     ← synthetic_image_generator (3 JPEGs w/ EXIF: camera, GPS, ISO)
          │
          └── image_metadata  ← image_exif_extractor

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected: 3 rows w/ exif_make=DagsterCam, exif_model=DG-1, exif_iso=200,
exif_focal_length_mm=35.0, and GPS coordinates near San Francisco.
MSG
