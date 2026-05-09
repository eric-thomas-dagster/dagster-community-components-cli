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

echo ">>> Installing vertex_ai_text_embeddings_asset + dataframe_to_csv"
uvx --from dagster-community-components-cli dagster-component add vertex_ai_text_embeddings_asset --auto-install 2>&1 | tail -2
uvx --from dagster-community-components-cli dagster-component add dataframe_to_csv --auto-install 2>&1 | tail -2

echo 'from .component import VertexAITextEmbeddingsAssetComponent
__all__ = ["VertexAITextEmbeddingsAssetComponent"]' > "src/$PKG/components/vertex_ai_text_embeddings_asset/__init__.py"

# 1) Custom upstream — 5 product descriptions
mkdir -p "src/$PKG/defs/sample_texts"
cat > "src/$PKG/defs/sample_texts/definitions.py" <<'PYEOF'
import pandas as pd
import dagster as dg

@dg.asset(
    key=dg.AssetKey(["sample_texts"]),
    description="5 short product descriptions to embed.",
    group_name="ingest",
    kinds={"pandas"},
)
def sample_texts() -> pd.DataFrame:
    return pd.DataFrame([
        {"sku": "MUG-001", "text": "A minimalist white ceramic coffee mug for everyday use."},
        {"sku": "BAG-002", "text": "A vintage tan leather messenger bag with brass buckles."},
        {"sku": "RUN-003", "text": "Lightweight neon running shoes for road and trail."},
        {"sku": "WTC-004", "text": "Stainless steel automatic dive watch with sapphire crystal."},
        {"sku": "BRN-005", "text": "Outdoor camping kettle, 1L, anodized aluminum."},
    ])

defs = dg.Definitions(assets=[sample_texts])
PYEOF

# 2) Vertex embeddings config
mkdir -p "src/$PKG/defs/vertex_ai_text_embeddings_asset"
cat > "src/$PKG/defs/vertex_ai_text_embeddings_asset/defs.yaml" <<EOF
type: $PKG.components.vertex_ai_text_embeddings_asset.component.VertexAITextEmbeddingsAssetComponent
attributes:
  asset_name: sample_text_embeddings
  upstream_asset_key: sample_texts
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"

  text_column: text
  output_column: embedding

  model_name: text-embedding-004
  task_type: RETRIEVAL_DOCUMENT
  location: us-central1
  batch_size: 5
  group_name: ai
EOF

# 3) Custom asset that flattens embedding to first 5 dims for CSV-friendly output
mkdir -p "src/$PKG/defs/embeddings_flat"
cat > "src/$PKG/defs/embeddings_flat/definitions.py" <<'PYEOF'
import pandas as pd
import dagster as dg
from dagster import AssetIn

@dg.asset(
    key=dg.AssetKey(["embeddings_flat"]),
    description="DataFrame with embedding[:5] flattened for CSV-readability.",
    group_name="ai",
    kinds={"pandas"},
    ins={"sample_text_embeddings": AssetIn(key=dg.AssetKey(["sample_text_embeddings"]))},
)
def embeddings_flat(sample_text_embeddings: pd.DataFrame) -> pd.DataFrame:
    df = sample_text_embeddings[["sku", "text"]].copy()
    embs = sample_text_embeddings["embedding"]
    df["embedding_dim"]   = embs.apply(lambda e: len(e) if isinstance(e, list) else 0)
    df["embedding_head5"] = embs.apply(
        lambda e: ", ".join(f"{v:.4f}" for v in e[:5]) if isinstance(e, list) else None
    )
    return df

defs = dg.Definitions(assets=[embeddings_flat])
PYEOF

# 4) CSV sink
mkdir -p "src/$PKG/defs/dataframe_to_csv"
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: embeddings_csv
  upstream_asset_key: embeddings_flat
  file_path: /tmp/vertex_embeddings.csv
  include_index: false
  description: SKU + text + embedding head + dim.
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
