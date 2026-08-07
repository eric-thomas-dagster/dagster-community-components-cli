#!/usr/bin/env bash
# Video Pipeline demo — synthetic test videos → metadata + frames + audio.
#
# 100% components, no custom Python in defs/. All 4 video components in one chain.
#
# Asset graph:
#   sample_videos       ← synthetic_video_generator (2 MP4s w/ video+audio)
#         │
#         ├── video_meta    ← video_metadata_extractor (ffprobe)
#         ├── video_frames  ← video_frame_extract_asset (5 frames each)
#         └── video_audio   ← video_audio_extract_asset (16kHz mono WAV)
#
# Requires `ffmpeg` (ffprobe ships with it).

set -euo pipefail
PROJECT_DIR="${1:-video-pipeline-demo}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not in PATH. Install:"
  echo "  macOS:         brew install ffmpeg"
  echo "  Debian/Ubuntu: apt install ffmpeg"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_video_generator    --auto-install 2>&1 | tail -2
$CLI add video_metadata_extractor     --auto-install 2>&1 | tail -2
$CLI add video_frame_extract_asset    --auto-install 2>&1 | tail -2
$CLI add video_audio_extract_asset    --auto-install 2>&1 | tail -2

for pair in \
  "synthetic_video_generator:SyntheticVideoGeneratorComponent" \
  "video_metadata_extractor:VideoMetadataExtractorComponent" \
  "video_frame_extract_asset:VideoFrameExtractAssetComponent" \
  "video_audio_extract_asset:VideoAudioExtractAssetComponent"; do
  DIR="${pair%%:*}"; CLS="${pair#*:}"
  echo "from .component import $CLS
__all__ = [\"$CLS\"]" > "src/$PKG/components/$DIR/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_video_generator" "src/$PKG/defs/video_metadata_extractor" "src/$PKG/defs/video_frame_extract_asset" "src/$PKG/defs/video_audio_extract_asset"
done

# 1) Synthetic test videos
mkdir -p "src/$PKG/defs/sample_videos"
cat > "src/$PKG/defs/sample_videos/defs.yaml" <<EOF
type: $PKG.components.synthetic_video_generator.component.SyntheticVideoGeneratorComponent
attributes:
  asset_name: sample_videos
  output_dir: out/video_demo_in
  samples: default
  group_name: ingest
EOF

# 2) ffprobe metadata
mkdir -p "src/$PKG/defs/video_meta"
cat > "src/$PKG/defs/video_meta/defs.yaml" <<EOF
type: $PKG.components.video_metadata_extractor.component.VideoMetadataExtractorComponent
attributes:
  asset_name: video_meta
  upstream_asset_key: sample_videos
  video_path_column: file_path
  group_name: media
EOF

# 3) Frame extraction (5 evenly-spaced frames per video)
mkdir -p "src/$PKG/defs/video_frames"
cat > "src/$PKG/defs/video_frames/defs.yaml" <<EOF
type: $PKG.components.video_frame_extract_asset.component.VideoFrameExtractAssetComponent
attributes:
  asset_name: video_frames
  upstream_asset_key: sample_videos
  video_path_column: file_path
  video_id_column: clip_id
  output_dir: out/video_demo_frames
  mode: fixed_count
  fixed_count: 5
  image_format: jpg
  image_quality: 85
  group_name: media
EOF

# 4) Audio track extraction (16kHz mono WAV — Whisper-ready)
mkdir -p "src/$PKG/defs/video_audio"
cat > "src/$PKG/defs/video_audio/defs.yaml" <<EOF
type: $PKG.components.video_audio_extract_asset.component.VideoAudioExtractAssetComponent
attributes:
  asset_name: video_audio
  upstream_asset_key: sample_videos
  video_path_column: file_path
  output_dir: out/video_demo_audio
  target_format: wav
  sample_rate: 16000
  channels: 1
  group_name: media
EOF

cat <<MSG

>>> Setup complete (100% components — no custom Python in defs/).

Asset graph:
    sample_videos       ← synthetic_video_generator (2 MP4s, h264+aac)
          │
          ├── video_meta    ← video_metadata_extractor (ffprobe)
          ├── video_frames  ← video_frame_extract_asset (5 frames/video)
          └── video_audio   ← video_audio_extract_asset (16kHz mono WAV)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    ls -la $PROJECT_ABS/out/video_demo_in/      # source MP4s
    ls -la $PROJECT_ABS/out/video_demo_frames/  # 10 extracted JPEG frames
    ls -la $PROJECT_ABS/out/video_demo_audio/   # extracted WAV audio tracks
MSG
