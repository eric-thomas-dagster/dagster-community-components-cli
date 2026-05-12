#!/usr/bin/env bash
# Audio Transform demo — sine-tone WAVs → 16kHz/mono WAVs (Whisper-ready).
#
# 100% components, no custom Python in defs/.
#
# Asset graph:
#   tones           ← synthetic_audio_generator (3 sine tones at 44.1kHz)
#         │
#         └── tones_16k_mono  ← audio_transform_asset (ffmpeg resample to 16kHz mono)
#
# Requires `ffmpeg` in PATH (macOS: `brew install ffmpeg`; Linux: `apt install ffmpeg`).

set -euo pipefail
PROJECT_DIR="${1:-audio-transform-demo}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not in PATH. Install:"
  echo "  macOS:        brew install ffmpeg"
  echo "  Debian/Ubuntu: apt install ffmpeg"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_audio_generator --auto-install 2>&1 | tail -2
$CLI add audio_transform_asset     --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticAudioGeneratorComponent
__all__ = ["SyntheticAudioGeneratorComponent"]' > "src/$PKG/components/synthetic_audio_generator/__init__.py"
echo 'from .component import AudioTransformAssetComponent
__all__ = ["AudioTransformAssetComponent"]' > "src/$PKG/components/audio_transform_asset/__init__.py"

# Remove auto-installed example defs (their asset names + upstream refs collide with ours)
rm -rf "src/$PKG/defs/synthetic_audio_generator" "src/$PKG/defs/audio_transform_asset"
# 1) Synthetic sine-tone WAVs
mkdir -p "src/$PKG/defs/tones"
cat > "src/$PKG/defs/tones/defs.yaml" <<EOF
type: $PKG.components.synthetic_audio_generator.component.SyntheticAudioGeneratorComponent
attributes:
  asset_name: tones
  output_dir: /tmp/audio_transform_demo_in
  samples: default
  sample_rate: 44100
  group_name: ingest
EOF

# 2) Resample → 16kHz mono (Whisper / STT preset)
mkdir -p "src/$PKG/defs/tones_16k_mono"
cat > "src/$PKG/defs/tones_16k_mono/defs.yaml" <<EOF
type: $PKG.components.audio_transform_asset.component.AudioTransformAssetComponent
attributes:
  asset_name: tones_16k_mono
  upstream_asset_key: tones
  audio_path_column: file_path
  output_dir: /tmp/audio_transform_demo_out
  target_format: wav
  sample_rate: 16000
  channels: 1
  group_name: media
EOF

cat <<MSG

>>> Setup complete (100% components — no custom Python in defs/).

Asset graph:
    tones                 ← synthetic_audio_generator (3 sine tones, 44.1kHz/16-bit)
          │
          └── tones_16k_mono  ← audio_transform_asset (ffmpeg → 16kHz mono WAV)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    ls -la /tmp/audio_transform_demo_in/    # 44.1kHz source tones
    ls -la /tmp/audio_transform_demo_out/   # 16kHz mono outputs (~36% the size)
MSG
