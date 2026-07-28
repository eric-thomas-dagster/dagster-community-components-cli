#!/usr/bin/env bash
# RAG State Demo — RAG as tracked state, not as a pipeline.
#
# End-to-end story (all components, no custom Python):
#   1. document_corpus                — 5 markdown files → versioned corpus asset
#   2. vector_index_snapshot          — chunks + embeds → new dynamic partition per snapshot
#   3. rag_eval[snapshot_id]          — golden-set retrieval, asset-check on regression
#   4. Modify a doc → new snapshot → eval FAILS asset check → the regression is caught
#   5. Older snapshot still queryable via its partition — that's the rollback path
#
# Everything runs locally, no API keys. ChromaDB uses its bundled ONNX MiniLM
# embedder (~90 MB one-time download; cached after).

set -eo pipefail

PROJECT_DIR="${1:-rag-state-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi

# --- 1. Fresh project scaffold ---------------------------------------------
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- 2. Install deps -------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi

uv add -q "$DCC_SRC" pandas "chromadb>=0.5.0"

# --- 2b. Pin DAGSTER_HOME so materializations persist across CLI invocations ---
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

# --- 3. Seed corpus (5 markdown docs about Dagster concepts) --------------
mkdir -p docs snapshots
cat > docs/retry_policy.md <<'MD'
# Retry Policy

Configure a retry_policy on any asset by passing a RetryPolicy(max_retries=3,
delay=30, backoff=Backoff.EXPONENTIAL). The op will be re-executed up to
max_retries times with the given delay between attempts. The backoff strategy
controls whether the delay is linear or exponential.

Use retry_policy for transient errors: network glitches, rate limits, brief
warehouse locks. Do not use it to paper over deterministic failures.
MD

cat > docs/dynamic_partitions.md <<'MD'
# Dynamic Partitions

DynamicPartitionsDefinition lets you add partition keys at runtime. Register
new keys via context.instance.add_dynamic_partitions(partition_key=...). The
UI shows a partition_key selector so you can materialize a specific partition
on demand — great for per-tenant, per-file, or per-event work.
MD

cat > docs/asset_checks.md <<'MD'
# Asset Checks

Attach an @asset_check to any asset. Return AssetCheckResult(passed=True/False,
severity=AssetCheckSeverity.ERROR, metadata={...}). Failing checks with
severity ERROR block downstream materializations. Use asset checks for schema
drift, row-count sanity, cross-domain freshness gates, and quality regressions.
MD

cat > docs/freshness.md <<'MD'
# Freshness Policies

FreshnessPolicy(maximum_lag_minutes=60, cron_schedule=...) alerts you when an
asset gets stale. Combine with sensors so a downstream re-materialization
kicks off before the freshness deadline. The Dagster UI plots freshness over
time; on-call sees "asset X is 30m late" instead of "the pipeline broke."
MD

cat > docs/automation.md <<'MD'
# Automation Conditions

AutomationCondition.eager() materializes an asset as soon as any upstream
completes. AutomationCondition.on_cron("0 9 * * 1-5") ties materialization to
a schedule. Combine conditions with & (AND) and | (OR) to express: "materialize
when upstream is fresh AND it's a weekday morning."
MD

# --- 4. Write defs.yaml files (100% components) ---------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

mkdir -p "$DEFS/corpus" "$DEFS/snapshot" "$DEFS/eval"

cat > "$DEFS/corpus/defs.yaml" <<YAML
type: dagster_community_components.DocumentCorpusComponent
attributes:
  asset_name: docs_corpus
  source_dir: ${PROJECT_ABS}/docs
  file_glob: "**/*.md"
  min_doc_count: 3
  group_name: rag_state
YAML

cat > "$DEFS/snapshot/defs.yaml" <<YAML
type: dagster_community_components.VectorIndexSnapshotComponent
attributes:
  asset_name: docs_index
  upstream_asset_key: docs_corpus
  snapshot_root_dir: ${PROJECT_ABS}/snapshots
  chunk_size: 500
  chunk_overlap: 50
  embedder_provider: chromadb_default
  collection_name: dagster_docs
  dynamic_partition_name: rag_snapshot
  group_name: rag_state
YAML

cat > "$DEFS/eval/defs.yaml" <<YAML
type: dagster_community_components.RagEvalComponent
attributes:
  asset_name: docs_eval
  upstream_snapshot_asset_key: docs_index
  snapshot_root_dir: ${PROJECT_ABS}/snapshots
  collection_name: dagster_docs
  k: 3
  min_score_threshold: 0.5
  regression_pct_threshold: 10.0
  dynamic_partition_name: rag_snapshot
  golden_set:
    - query: "How do I configure retry policy?"
      expected_terms: ["retry_policy", "max_retries", "backoff"]
    - query: "What is a dynamic partition?"
      expected_terms: ["DynamicPartitionsDefinition", "partition_key"]
    - query: "How do I write an asset check?"
      expected_terms: ["AssetCheckResult", "passed", "severity"]
    - query: "How do I set freshness?"
      expected_terms: ["FreshnessPolicy", "maximum_lag_minutes"]
  group_name: rag_state
YAML

# --- 5. dg check defs ------------------------------------------------------
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

# --- 6. First round: materialize corpus + snapshot v1 + eval[v1] ----------
echo ""
echo ">>> Round 1 — original corpus"
uv run dg launch --assets docs_corpus 2>&1 | tail -3
uv run dg launch --assets docs_index 2>&1 | tail -3

# Read the snapshot id that just got registered (latest symlink → snapshot dir)
SNAP1="$(basename "$(readlink snapshots/latest 2>/dev/null || cat snapshots/latest.txt 2>/dev/null)")"
if [ -z "$SNAP1" ]; then echo "✗ Could not resolve snapshot v1 id"; exit 1; fi
echo "    snapshot v1 id: $SNAP1"

echo ">>> Materialize rag_eval against snapshot v1"
uv run dg launch --assets docs_eval --partition "$SNAP1" 2>&1 | tail -5

# --- 7. Regress the corpus: strip key terms from one doc ------------------
echo ""
echo ">>> Injecting a regression — stripping 'max_retries' and 'backoff' from retry_policy.md"
sed -i.bak \
  -e 's/max_retries=[0-9]*, //g' \
  -e 's/, backoff=Backoff\.EXPONENTIAL//g' \
  -e 's/backoff strategy/policy shape/g' \
  -e 's/exponential/one-shot/g' \
  -e 's/max_retries/attempts/g' \
  -e 's/backoff/timing/g' \
  docs/retry_policy.md
rm -f docs/retry_policy.md.bak

# --- 8. Second round: rematerialize corpus + snapshot v2 + eval[v2] -------
echo ""
echo ">>> Round 2 — regressed corpus"
uv run dg launch --assets docs_corpus 2>&1 | tail -3
uv run dg launch --assets docs_index 2>&1 | tail -3

SNAP2="$(basename "$(readlink snapshots/latest 2>/dev/null || cat snapshots/latest.txt 2>/dev/null)")"
if [ "$SNAP2" = "$SNAP1" ] || [ -z "$SNAP2" ]; then echo "✗ Snapshot v2 didn't advance"; exit 1; fi
echo "    snapshot v2 id: $SNAP2"

echo ">>> Materialize rag_eval against snapshot v2 (asset check should FAIL — regression)"
uv run dg launch --assets docs_eval --partition "$SNAP2" 2>&1 | tail -15 || true

# --- 9. Summary + rollback story ------------------------------------------
cat <<DONE

✓ RAG State Demo complete.

What just happened:
  1. docs_corpus materialized twice — corpus_hash changed between v1 and v2.
  2. docs_index registered TWO dynamic partitions (each a physical, immutable
     ChromaDB snapshot on disk):
        - v1: $SNAP1
        - v2: $SNAP2
  3. docs_eval materialized once per snapshot partition:
        - v1: full retrieval score
        - v2: retrieval regressed (retry_policy query lost its key terms)
             → asset check FAILS — Dagster catches the regression as data,
                not as a build error.

Rollback path (graph-native, no rebuild):
  - Older snapshot's ChromaDB dir is still on disk: snapshots/$SNAP1/
  - Its docs_eval materialization is still in Dagster's history.
  - Point downstream RAG queries at the older partition via
      dg launch --assets <your_rag_answer> --partition $SNAP1

Browse it all in the UI:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # - Assets tab: 3 assets in group 'rag_state'
  # - Runs tab: 6 runs (2× corpus, 2× snapshot, 2× eval)
  # - docs_eval → Partitions: both snapshot ids present; asset check status per partition
  # - Materialization history on docs_eval → precision@k trend

Contrast against Prefect / an imperative RAG flow:
  Prefect can run the same chain. It cannot natively:
    - Roll back queries to a past snapshot by partition selection.
    - Block downstream materialization on a retrieval-quality asset check.
    - Backfill the eval across all past snapshots to plot quality over time.

Cleanup:
  rm -rf $PROJECT_ABS
DONE
