#!/usr/bin/env bash
# Speech-to-Text + Translation demo — transcribe audio, then translate to 4 languages.
#
# WHAT THIS DEMONSTRATES
#   Two GCP ML-API components chained end-to-end:
#     speech_to_text_asset    — transcribe public sample audio files
#     translation_api_asset   — translate the transcripts to es / fr / de / ja
#
# Asset graph:
#   audio_files                (DataFrame with public gs:// audio URIs)
#         │
#         └── transcripts                  ← speech_to_text_asset
#                  │
#                  └── transcripts_translated  ← translation_api_asset (es/fr/de/ja)
#                            │
#                            └── transcripts_csv  ← /tmp/speech_translate.csv
#
# REQUIRED ENV VAR
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON
#
# COST while running
#   ~\$0.001. Speech-to-Text is \$0.024/min beyond 60min/mo free; demo audio is
#   well under 1 min total. Translation: \$20/M chars; ~50 chars × 4 langs = $0.

set -euo pipefail
PROJECT_DIR="${1:-speech-translate-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-speech google-cloud-translate
uv add --dev -q dagster-dg-cli

echo ">>> Installing speech_to_text_asset + translation_api_asset + dataframe_to_csv"
uvx --from dagster-community-components-cli dagster-component add speech_to_text_asset --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add translation_api_asset --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add dataframe_to_csv --auto-install 2>&1 | tail -2

echo 'from .component import SpeechToTextAssetComponent
__all__ = ["SpeechToTextAssetComponent"]' > "src/$PKG/components/speech_to_text_asset/__init__.py"
echo 'from .component import TranslationApiAssetComponent
__all__ = ["TranslationApiAssetComponent"]' > "src/$PKG/components/translation_api_asset/__init__.py"

# 1) Audio source — public Google sample
mkdir -p "src/$PKG/defs/audio_files"
cat > "src/$PKG/defs/audio_files/definitions.py" <<'PYEOF'
"""Public sample audio files from Google's cloud-samples-data bucket."""
import pandas as pd
import dagster as dg

@dg.asset(
    key=dg.AssetKey(["audio_files"]),
    description="2 public Google Speech sample audio clips.",
    group_name="ingest",
    kinds={"pandas"},
)
def audio_files() -> pd.DataFrame:
    return pd.DataFrame([
        {"id": "brooklyn", "audio_uri": "gs://cloud-samples-data/speech/brooklyn_bridge.mp3", "expected": "how old is the Brooklyn Bridge"},
        {"id": "hello",    "audio_uri": "gs://cloud-samples-data/speech/hello.wav",           "expected": "hello"},
    ])

defs = dg.Definitions(assets=[audio_files])
PYEOF

# 2) Speech-to-Text — transcribe each audio
mkdir -p "src/$PKG/defs/speech_to_text_asset"
cat > "src/$PKG/defs/speech_to_text_asset/defs.yaml" <<EOF
type: $PKG.components.speech_to_text_asset.component.SpeechToTextAssetComponent
attributes:
  asset_name: transcripts
  upstream_asset_key: audio_files
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"

  audio_column: audio_uri
  output_column: transcript

  language_codes: [en-US]
  recognizer_model: latest_long
  enable_automatic_punctuation: true
  group_name: ai
EOF

# 3) Translation — translate transcripts to 4 langs
mkdir -p "src/$PKG/defs/translation_api_asset"
cat > "src/$PKG/defs/translation_api_asset/defs.yaml" <<EOF
type: $PKG.components.translation_api_asset.component.TranslationApiAssetComponent
attributes:
  asset_name: transcripts_translated
  upstream_asset_key: transcripts
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"

  text_column: transcript
  target_languages: [es, fr, de, ja]
  output_prefix: transcript_
  mime_type: text/plain
  group_name: i18n
EOF

# 4) CSV sink
mkdir -p "src/$PKG/defs/dataframe_to_csv"
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: transcripts_csv
  upstream_asset_key: transcripts_translated
  file_path: /tmp/speech_translate.csv
  include_index: false
  description: Transcripts plus translations.
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    audio_files                       (2 public Google sample gs:// audio URIs)
          │
          └── transcripts                ← speech_to_text_asset (en-US)
                    │
                    └── transcripts_translated  ← translation_api_asset (es / fr / de / ja)
                              │
                              └── transcripts_csv  ← /tmp/speech_translate.csv

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    cat /tmp/speech_translate.csv
MSG
