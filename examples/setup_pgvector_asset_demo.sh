#!/usr/bin/env bash
# pgvector_asset demo — Ollama + pgvector Docker, fully hermetic.
#
# Exercises BOTH embedding paths of the refactored pgvector_asset (v1.2.0+):
#
#   Path A — inline via LiteLLM (multi-provider):
#     synthetic_data_generator (100 products)
#       → pgvector_asset (embedding_model: ollama/nomic-embed-text,
#                          api_base_env_var: OLLAMA_HOST)
#         Embeds via LOCAL Ollama; no external API keys.
#
#   Path B — precomputed (compose with any upstream embedder):
#     synthetic_data_generator (100 products)
#       → litellm_embedding_batch (adds `embedding` column via same Ollama)
#         → pgvector_asset (precomputed_embedding_column: embedding)
#           Skips the embedder call, just upserts the DataFrame's vectors.
#
# Then pgvector_reader queries both target tables with the same literal
# vector to confirm the two paths produce identical results.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-pgvector-asset-demo}"
PG_NAME=dg-pgvector-asset-demo
PG_PORT=5434
PG_PASSWORD=demo
PG_URL="postgresql+psycopg2://postgres:${PG_PASSWORD}@localhost:${PG_PORT}/postgres"

OLLAMA_NAME=dg-ollama-demo
OLLAMA_PORT=11435
OLLAMA_HOST="http://localhost:${OLLAMA_PORT}"
EMBED_MODEL="nomic-embed-text"
EMBED_DIM=768   # nomic-embed-text output dim

echo ">>> 1/6  Starting Postgres 16 + pgvector on :$PG_PORT"
docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$PG_NAME" \
  -e POSTGRES_PASSWORD=$PG_PASSWORD \
  -p $PG_PORT:5432 \
  pgvector/pgvector:pg16 >/dev/null
for i in $(seq 1 30); do
  if docker exec "$PG_NAME" pg_isready -U postgres >/dev/null 2>&1; then
    echo "    Postgres up."
    break
  fi
  sleep 2
done
docker exec -i "$PG_NAME" psql -U postgres -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null

echo ">>> 2/6  Starting Ollama on :$OLLAMA_PORT (~700MB image on first pull)"
docker rm -f "$OLLAMA_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$OLLAMA_NAME" -p $OLLAMA_PORT:11434 ollama/ollama:latest >/dev/null
echo "    Waiting for Ollama to become ready..."
for i in $(seq 1 30); do
  if curl -fs "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
    echo "    Ollama up."
    break
  fi
  sleep 2
done

echo ">>> 3/6  Pulling embedding model '$EMBED_MODEL' (~274MB — one-time)"
docker exec "$OLLAMA_NAME" ollama pull "$EMBED_MODEL"

echo ">>> 4/6  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> 5/6  Adding deps"
uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'sqlalchemy>=2.0.0' 'psycopg2-binary>=2.9.0' 'pgvector>=0.2.0' \
          'pandas>=1.5.0' 'litellm>=1.30.0' faker

CLI="uvx --refresh --from dagster-community-components-cli dagster-component --refresh"

echo ">>> 6/6  Installing 3 components + writing defs.yaml"
$CLI add synthetic_data_generator --auto-install
$CLI add litellm_embedding_batch  --auto-install
$CLI add pgvector_asset           --auto-install

# `dg add pgvector_asset` scaffolds a defs/pgvector_asset/defs.yaml from
# example.yaml. We use pgvector_asset twice below (inline + precomputed)
# under different dir names, so remove the auto-generated single-instance
# defs to avoid a duplicate-asset conflict.
rm -rf "src/$PKG/defs/pgvector_asset"

# Upstream: 100 fake product rows with a description column
cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: raw_products
  schema_type: products
  row_count: 100
  random_state: 42
  description: 100 fake products for the pgvector_asset demo
  group_name: seeds
EOF

# Path A — pgvector_asset embeds inline via LiteLLM + Ollama
mkdir -p "src/$PKG/defs/pgvector_asset_inline"
cat > "src/$PKG/defs/pgvector_asset_inline/defs.yaml" <<EOF
type: $PKG.components.pgvector_asset.component.PgvectorAssetComponent
attributes:
  asset_name: product_embeddings_inline
  upstream_asset_key: raw_products
  database_url_env_var: DATABASE_URL
  id_column: product_id
  text_column: name
  target_table: product_embeddings_inline
  embedding_model: ollama/${EMBED_MODEL}
  api_base_env_var: OLLAMA_HOST
  dimensions: $EMBED_DIM
  batch_size: 20
  if_exists: replace
  group_name: vector_store_inline
EOF

# Path B — litellm_embedding_batch adds an 'embedding' column;
# pgvector_asset upserts vectors from that column (skips embedder).
cat > "src/$PKG/defs/litellm_embedding_batch/defs.yaml" <<EOF
type: $PKG.components.litellm_embedding_batch.component.LitellmEmbeddingBatchComponent
attributes:
  asset_name: product_embedded
  upstream_asset_key: raw_products
  text_column: name
  output_column: embedding
  model: ollama/${EMBED_MODEL}
  api_base_env_var: OLLAMA_HOST
  batch_size: 20
  group_name: vector_store_precomputed
EOF

mkdir -p "src/$PKG/defs/pgvector_asset_precomputed"
cat > "src/$PKG/defs/pgvector_asset_precomputed/defs.yaml" <<EOF
type: $PKG.components.pgvector_asset.component.PgvectorAssetComponent
attributes:
  asset_name: product_embeddings_precomputed
  upstream_asset_key: product_embedded
  database_url_env_var: DATABASE_URL
  id_column: product_id
  text_column: name
  precomputed_embedding_column: embedding
  target_table: product_embeddings_precomputed
  dimensions: $EMBED_DIM
  if_exists: replace
  group_name: vector_store_precomputed
EOF

cat <<MSG

>>> Setup complete.

Export env vars:

    export DATABASE_URL='$PG_URL'
    export OLLAMA_HOST='$OLLAMA_HOST'

Validate + materialize both paths:

    cd $PROJECT_DIR
    export DATABASE_URL='$PG_URL' OLLAMA_HOST='$OLLAMA_HOST'
    uv run dg check defs
    uv run dg launch --assets '*'

Verify both target tables landed with vector(${EMBED_DIM}) columns:

    docker exec -it $PG_NAME psql -U postgres -c \\
      "SELECT 'inline' AS path, COUNT(*) AS rows, MAX(vector_dims(embedding)) AS dim
         FROM product_embeddings_inline
        UNION ALL
       SELECT 'precomputed', COUNT(*), MAX(vector_dims(embedding))
         FROM product_embeddings_precomputed;"

Both paths should show 100 rows × 768 dims. To confirm the two paths produce
IDENTICAL embeddings (same model, same rows):

    docker exec -it $PG_NAME psql -U postgres -c \\
      "SELECT COUNT(*) FILTER (WHERE i.embedding = p.embedding) AS matching, COUNT(*) AS total
         FROM product_embeddings_inline i
         JOIN product_embeddings_precomputed p USING (id);"

Cleanup:

    docker rm -f $PG_NAME $OLLAMA_NAME
MSG
