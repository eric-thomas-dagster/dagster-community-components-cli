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
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas pillow anthropic openai
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing image_metadata_extractor + vision_model"
$CLI add synthetic_image_generator --auto-install
$CLI add image_metadata_extractor  --auto-install
$CLI add vision_model              --auto-install

echo 'from .component import SyntheticImageGeneratorComponent
__all__ = ["SyntheticImageGeneratorComponent"]' > "src/$PKG/components/synthetic_image_generator/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_image_generator" "src/$PKG/defs/image_metadata_extractor" "src/$PKG/defs/vision_model"

# ─── Synthetic image generator (component) ────────────────────────────────
mkdir -p "src/$PKG/defs/sample_images"
cat > "src/$PKG/defs/sample_images/defs.yaml" <<EOF
type: $PKG.components.synthetic_image_generator.component.SyntheticImageGeneratorComponent
attributes:
  asset_name: sample_images_df
  output_dir: out/vision_pipeline_images
  samples: default
  group_name: ingest
EOF

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
    sample_images_df          (3 synthetic PNGs in $PROJECT_ABS/out/vision_pipeline_images)
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
