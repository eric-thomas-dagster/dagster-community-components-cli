#!/usr/bin/env bash
# setup_generative_media_pipeline_demo.sh
#
# Raw video becomes searchable, structured, indexed content -- a real
# multi-modal pipeline, not just text. Chains VideoSceneSummarizerComponent
# (ffmpeg scene-change detection + vision-LLM summary per scene) with
# StructuredDocumentExtractorComponent (structure each summary into fields),
# EmbeddingsGeneratorComponent (local, free), and VectorStoreWriterComponent
# (ChromaDB) for searchable video content.
#
#   sample_videos → scene_summaries → scene_fields → scene_embeddings → scene_vector_index
#      (synth)     (ffmpeg+vision-LLM) (structure)   (local embed)      (ChromaDB)
#
# HONEST COST NOTE: scene detection (ffmpeg) is local and free; the scene
# SUMMARY and FIELD EXTRACTION steps are real vision-LLM calls and need a
# real OPENAI_API_KEY (or swap model/api_key_env_var for Anthropic/Gemini).
# This is NOT a $0 demo -- expect a few cents for the handful of short
# synthetic test-pattern clips generated below. The source video is a
# synthetically-generated test card + tone (via synthetic_video_generator),
# not real footage, so the vision-LLM's summaries will describe a generic
# test pattern rather than anything meaningful -- this proves the pipeline
# WIRING end-to-end; point video_path_column at your own real video files
# for summaries that are actually useful.
#
# What it demonstrates
#   • synthetic_video_generator — sample video clips (ffmpeg test pattern, local, free)
#   • video_scene_summarizer — ffmpeg scene-change detection + vision-LLM summary per scene
#   • structured_document_extractor — structures each summary into fields (input_type: text)
#   • embeddings_generator (sentence_transformers — local, free)
#   • vector_store_writer (chromadb) — searchable video content
#
# Cost: a few cents (OpenAI vision-LLM calls for scene summarization + field
# extraction) — everything else is local and free.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • ffmpeg in PATH
#   • OPENAI_API_KEY (or edit model/api_key_env_var for a different provider)
#
# Usage
#   OPENAI_API_KEY=sk-... ./setup_generative_media_pipeline_demo.sh
#   OPENAI_API_KEY=sk-... ./setup_generative_media_pipeline_demo.sh my_project

set -eo pipefail

PROJECT_NAME="${1:-generative_media_pipeline_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg not found -- install it (e.g. 'brew install ffmpeg')."
[ -z "${OPENAI_API_KEY:-}" ] && fail "OPENAI_API_KEY not set -- this demo makes real (cheap) vision-LLM calls."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

info "Installing deps (pandas, numpy, sentence-transformers, chromadb, litellm)…"
uv add -q pandas numpy sentence-transformers chromadb litellm
uv add --dev -q dagster-dg-cli dagster-webserver

info "Installing components (--refresh on first call busts the registry-manifest cache)"
uvx --from dagster-community-components-cli dagster-component --refresh add synthetic_video_generator --auto-install
uvx --from dagster-community-components-cli dagster-component add video_scene_summarizer --auto-install
uvx --from dagster-community-components-cli dagster-component add structured_document_extractor --auto-install
uvx --from dagster-community-components-cli dagster-component add embeddings_generator --auto-install
uvx --from dagster-community-components-cli dagster-component add vector_store_writer --auto-install

info "Writing defs.yaml (synth video → summarize → structure → embed → index)"

cat > "src/$PKG/defs/synthetic_video_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_video_generator.component.SyntheticVideoGeneratorComponent
attributes:
  asset_name: sample_videos
  output_dir: ./synthetic_videos
  samples: default
  group_name: ingest
EOF

cat > "src/$PKG/defs/video_scene_summarizer/defs.yaml" <<EOF
type: $PKG.components.video_scene_summarizer.component.VideoSceneSummarizerComponent
attributes:
  asset_name: scene_summaries
  upstream_asset_key: sample_videos
  video_path_column: file_path
  video_id_column: clip_id
  scene_threshold: 0.4
  max_scenes_per_video: 3
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: media
EOF

cat > "src/$PKG/defs/structured_document_extractor/defs.yaml" <<EOF
type: $PKG.components.structured_document_extractor.component.StructuredDocumentExtractorComponent
attributes:
  asset_name: scene_fields
  upstream_asset_key: scene_summaries
  document_type: custom
  output_fields: [setting, primary_subject, notable_action]
  input_column: summary
  input_type: text
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: media
EOF

cat > "src/$PKG/defs/embeddings_generator/defs.yaml" <<EOF
type: $PKG.components.embeddings_generator.component.EmbeddingsGeneratorComponent
attributes:
  asset_name: scene_embeddings
  upstream_asset_key: scene_fields
  provider: sentence_transformers
  model: all-MiniLM-L6-v2
  input_column: summary
  output_column: embedding
  group_name: media
EOF

cat > "src/$PKG/defs/vector_store_writer/defs.yaml" <<EOF
type: $PKG.components.vector_store_writer.component.VectorStoreWriterComponent
attributes:
  asset_name: scene_vector_index
  upstream_asset_key: scene_embeddings
  provider: chromadb
  collection_name: video_scenes
  embedding_column: embedding
  text_column: summary
  metadata_columns: [setting, primary_subject, notable_action]
  connection_string: ./chroma_db
  group_name: media
EOF

ok "Wrote 5 defs.yaml"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Materializing the full chain (downloads sentence-transformers model on first run; real vision-LLM calls, a few cents)"
uv run dg launch --assets '*' 2>&1 | tail -30

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Generated a handful of synthetic test-pattern video clips (ffmpeg, local)
  2. Detected scene changes in each clip and summarized each scene with a
     real vision-LLM call (scene_summaries)
  3. Extracted structured fields (setting, primary_subject, notable_action)
     from each summary with a second LLM call (scene_fields)
  4. Embedded each summary locally (sentence-transformers, no API key)
  5. Wrote everything to a local ChromaDB collection (scene_vector_index)

Inspect the results:
  cd $PROJECT_NAME
  uv run dg dev
    → click scene_fields → read what the vision-LLM actually saw in the
      synthetic test pattern (expect generic descriptions -- it's a
      colorbar + tone clip, not real footage)

Point at real footage:
  Swap synthetic_video_generator for a file_lister asset (or any asset
  producing a DataFrame of real video file paths) pointed at your own
  videos -- everything downstream is unchanged. Lower scene_threshold
  (e.g. 0.2) to catch subtler cuts like slow pans/dissolves.

Production variants:
  • Swap provider: openai (needs OPENAI_API_KEY) on embeddings_generator
    for higher-quality embeddings than the free local model
  • Swap vector_store_writer's provider to s3vectors for native AWS storage
    (see the S3 Vectors RAG demo) instead of ChromaDB
  • Tune summary_prompt on video_scene_summarizer to look for something
    specific (e.g. "identify the product shown and any visible price")
EOF
