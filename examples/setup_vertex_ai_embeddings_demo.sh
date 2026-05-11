#!/usr/bin/env bash
# Vertex AI Text Embeddings demo — text → 768-dim vectors via Vertex.
#
# WHAT THIS DEMONSTRATES
#   The vertex_ai_text_embeddings_asset component running text-embedding-004
#   on 5 synthetic product descriptions. Returns the upstream DataFrame
#   with an embedding column (list of floats) added. Useful for RAG,
#   semantic search, vector store loaders.
#
# Asset graph:
#   sample_texts             (5 product descriptions)
#         │
#         └── sample_text_embeddings   ← vertex_ai_text_embeddings_asset
#                                       (text-embedding-004, RETRIEVAL_DOCUMENT)
#                  │
#                  └── embeddings_csv  ← dataframe_to_csv (/tmp/vertex_embeddings.csv)
#
# REQUIRED ENV VAR
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON
#
# COST while running
#   ~\$0.0001. text-embedding-004 is \$0.025 / 1M input chars; 5 short rows
#   cost effectively nothing. First 100K chars/month are free.

set -euo pipefail
PROJECT_DIR="${1:-vertex-ai-embeddings-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-aiplatform
uv add --dev -q dagster-dg-cli

echo ">>> Installing components"
uvx --from dagster-community-components-cli dagster-component add synthetic_data_generator         --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add vertex_ai_text_embeddings_asset  --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add dataframe_to_gcs                 --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import VertexAITextEmbeddingsAssetComponent
__all__ = ["VertexAITextEmbeddingsAssetComponent"]' > "src/$PKG/components/vertex_ai_text_embeddings_asset/__init__.py"
echo 'from .component import DataframeToGcsComponent
__all__ = ["DataframeToGcsComponent"]' > "src/$PKG/components/dataframe_to_gcs/__init__.py"

# 1) Synthetic upstream — image-generation prompts work great as embedding source text
mkdir -p "src/$PKG/defs/sample_texts"
cat > "src/$PKG/defs/sample_texts/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: sample_texts
  schema_type: image_prompts
  row_count: 5
  random_state: 42
  group_name: ingest
EOF

# 2) Vertex embeddings — embed the `prompt` column from image_prompts schema
mkdir -p "src/$PKG/defs/vertex_ai_text_embeddings_asset"
cat > "src/$PKG/defs/vertex_ai_text_embeddings_asset/defs.yaml" <<EOF
type: $PKG.components.vertex_ai_text_embeddings_asset.component.VertexAITextEmbeddingsAssetComponent
attributes:
  asset_name: sample_text_embeddings
  upstream_asset_key: sample_texts
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  text_column: prompt
  output_column: embedding
  model_name: text-embedding-004
  task_type: RETRIEVAL_DOCUMENT
  location: us-central1
  batch_size: 5
  group_name: ai
EOF

# 3) Sink — parquet to GCS keeps the 768-dim array column as a native list
mkdir -p "src/$PKG/defs/dataframe_to_gcs"
cat > "src/$PKG/defs/dataframe_to_gcs/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_gcs.component.DataframeToGcsComponent
attributes:
  asset_name: embeddings_parquet
  upstream_asset_key: sample_text_embeddings
  bucket_env_var: GCS_BUCKET
  blob_path: embeddings/sample_text_embeddings.parquet
  format: parquet
  credentials_env_var: GOOGLE_APPLICATION_CREDENTIALS
  description: prompt_id + prompt + 768-dim Vertex embedding (parquet preserves the array natively).
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    sample_texts                    (5 short product descriptions)
          │
          └── sample_text_embeddings   ← vertex_ai_text_embeddings_asset
                                        (text-embedding-004, 768-dim)
                    │
                    └── embeddings_flat        ← pandas (embedding[:5] flat)
                              │
                              └── embeddings_csv  ← /tmp/vertex_embeddings.csv

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    cat /tmp/vertex_embeddings.csv
MSG
