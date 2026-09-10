#!/usr/bin/env bash
# pgvector_reader demo — Docker Postgres+pgvector, no external API keys.
#
# The reader takes EITHER a query_text (embedded via OpenAI — needs API
# key) OR a pre-computed query_embedding literal vector. We use the
# literal path so the demo is fully hermetic.
#
# Shape (1 component):
#   pgvector_reader  →  ANN search against a pgvector-indexed Postgres table
#
# Setup:
#   1. `pgvector/pgvector:pg16` docker container (Postgres 16 + pgvector 0.7+)
#   2. Seeded `documents` table with 8 rows of (id, title, embedding vector(4))
#      — deliberately 4-dim so the query vector literal fits on one line.
#      Each row's embedding is hand-tuned so cosine-similar results are
#      deterministic and easy to reason about.
#   3. IVFFLAT index for realistic ANN behavior.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-pgvector-reader-demo}"
PG_NAME=dg-pgvector-demo
PG_PORT=5433
PG_PASSWORD=demo
PG_URL="postgresql+psycopg2://postgres:${PG_PASSWORD}@localhost:${PG_PORT}/postgres"

echo ">>> 1/5  Starting Postgres 16 + pgvector on :$PG_PORT"
docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$PG_NAME" \
  -e POSTGRES_PASSWORD=$PG_PASSWORD \
  -p $PG_PORT:5432 \
  pgvector/pgvector:pg16 >/dev/null

echo "    Waiting for Postgres to become ready..."
for i in $(seq 1 30); do
  if docker exec "$PG_NAME" pg_isready -U postgres >/dev/null 2>&1; then
    echo "    Postgres up."
    break
  fi
  sleep 2
done

echo ">>> 2/5  Enabling pgvector extension + seeding documents"
docker exec -i "$PG_NAME" psql -U postgres <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
DROP TABLE IF EXISTS documents;
CREATE TABLE documents (
    id        INTEGER PRIMARY KEY,
    title     TEXT NOT NULL,
    embedding vector(4) NOT NULL
);
-- 8 documents on a small conceptual grid. Query vector [1.0, 0.0, 0.0, 0.0]
-- will match "ml basics" and "ml advanced" most closely (both aligned to
-- axis 0), whereas "cooking pasta" (axis 3) will be least similar.
INSERT INTO documents VALUES
    (1, 'ml basics',       '[1.0, 0.1, 0.0, 0.0]'),
    (2, 'ml advanced',     '[0.9, 0.2, 0.1, 0.0]'),
    (3, 'ml frameworks',   '[0.8, 0.3, 0.0, 0.1]'),
    (4, 'databases 101',   '[0.1, 1.0, 0.0, 0.0]'),
    (5, 'sql tuning',      '[0.0, 0.9, 0.1, 0.0]'),
    (6, 'devops handbook', '[0.0, 0.1, 1.0, 0.0]'),
    (7, 'k8s in prod',     '[0.0, 0.0, 0.9, 0.1]'),
    (8, 'cooking pasta',   '[0.0, 0.0, 0.0, 1.0]');
-- pgvector IVFFLAT index — realistic ANN shape (~lists=√rows).
CREATE INDEX ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 3);
ANALYZE documents;
SQL
echo "    seeded 8 documents + ivfflat cosine index"

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> 4/5  Adding deps"
uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'sqlalchemy>=2.0.0' 'psycopg2-binary>=2.9.0' 'pgvector>=0.2.0' 'pandas>=1.5.0'

CLI="uvx --refresh --from dagster-community-components-cli dagster-component --refresh"

echo ">>> 5/5  Installing pgvector_reader"
$CLI add pgvector_reader --auto-install

cat > "src/$PKG/defs/pgvector_reader/defs.yaml" <<EOF
type: $PKG.components.pgvector_reader.component.PgvectorReaderComponent
attributes:
  asset_name: similar_docs
  database_url_env_var: DATABASE_URL
  table_name: documents
  embedding_column: embedding
  # Literal 4-dim vector — bypasses the OpenAI query_text embedding
  # path entirely (no API key needed).
  query_embedding: [1.0, 0.0, 0.0, 0.0]
  n_results: 5
  distance_metric: cosine
  group_name: search
EOF

cat <<MSG

>>> Setup complete.

Export DATABASE_URL:

    export DATABASE_URL='$PG_URL'

Validate + materialize:

    cd $PROJECT_DIR
    export DATABASE_URL='$PG_URL'
    uv run dg check defs
    uv run dg launch --assets similar_docs

Expected: 5 rows ranked by cosine similarity to [1.0, 0.0, 0.0, 0.0]:
    ml basics (closest), ml advanced, ml frameworks, ...

Verify directly from Postgres:

    docker exec -it $PG_NAME psql -U postgres -c \\
      "SELECT id, title, 1 - (embedding <=> '[1.0, 0.0, 0.0, 0.0]') AS similarity FROM documents ORDER BY embedding <=> '[1.0, 0.0, 0.0, 0.0]' LIMIT 5;"

Cleanup:

    docker rm -f $PG_NAME
MSG
