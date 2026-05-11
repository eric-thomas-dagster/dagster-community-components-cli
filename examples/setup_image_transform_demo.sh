#!/usr/bin/env bash
# Image Transform demo — synthetic PNGs → resized WebP thumbnails.
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   sample_images   ← synthetic_image_generator (3 default PNGs, 640x640)
#         │
#         └── thumbnails  ← image_transform_asset (resize 128x128, → WebP, q=80)
#
# No external services. Pillow-only. Local files in /tmp.

set -euo pipefail
PROJECT_DIR="${1:-image-transform-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pillow
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_image_generator --auto-install 2>&1 | tail -2
$CLI add image_transform_asset     --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticImageGeneratorComponent
__all__ = ["SyntheticImageGeneratorComponent"]' > "src/$PKG/components/synthetic_image_generator/__init__.py"
echo 'from .component import ImageTransformAssetComponent
__all__ = ["ImageTransformAssetComponent"]' > "src/$PKG/components/image_transform_asset/__init__.py"

# 1) Synthetic PNGs
mkdir -p "src/$PKG/defs/sample_images"
cat > "src/$PKG/defs/sample_images/defs.yaml" <<EOF
type: $PKG.components.synthetic_image_generator.component.SyntheticImageGeneratorComponent
attributes:
  asset_name: sample_images
  output_dir: /tmp/image_transform_demo_in
  samples: default
  width: 640
  height: 640
  group_name: ingest
EOF

# 2) Resize → WebP thumbnails
mkdir -p "src/$PKG/defs/thumbnails"
cat > "src/$PKG/defs/thumbnails/defs.yaml" <<EOF
type: $PKG.components.image_transform_asset.component.ImageTransformAssetComponent
attributes:
  asset_name: thumbnails
  upstream_asset_key: sample_images
  image_path_column: file_path
  output_dir: /tmp/image_transform_demo_out
  resize_to: [128, 128]
  preserve_aspect_ratio: true
  convert_to: webp
  quality: 80
  group_name: media
EOF

cat <<MSG

>>> Setup complete (100% components — no custom Python in defs/).

Asset graph:
    sample_images       ← synthetic_image_generator (3 PNGs, 640x640)
          │
          └── thumbnails  ← image_transform_asset (→ 128px WebP, q=80)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    ls -la /tmp/image_transform_demo_in/   # 640x640 source PNGs
    ls -la /tmp/image_transform_demo_out/  # 128px WebP thumbnails
MSG
