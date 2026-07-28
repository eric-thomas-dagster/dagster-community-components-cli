#!/usr/bin/env bash
# rag_supervisor — planner-picks-specialist pattern, contrast with retrieval RAG.
#
# Where rag_complete.md shows linear retrieval (chunk → embed → retrieve → rerank →
# answer), this walkthrough shows the *task-decomposition* shape: a planner LLM
# reads the task, picks specialist tools from a bounded YAML-declared set,
# each specialist runs as its own asset, and a synthesizer combines the
# results. Full Dagster lineage on every runtime decision.
#
# What runs:
#   docs_corpus              — same 5-doc corpus as rag_complete (source only,
#                              here for context — supervisor tools don't retrieve)
#   supervisor_plan          — planner LLM reads task, emits tool picks
#   <tool>_result (×3)       — one asset per specialist tool; runs only if the
#                              planner picked it, else empty
#   supervisor_final_answer  — synthesizer LLM combines invoked tool outputs
#
# Requires OPENAI_API_KEY (or edit defs.yaml to swap providers).

set -eo pipefail

PROJECT_DIR="${1:-rag-supervisor-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "✗ OPENAI_API_KEY not set. Export a key (or edit rag_answer/defs.yaml to swap providers)."
  exit 1
fi

# --- 1. Scaffold ----------------------------------------------------------
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi

export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

uv add -q "$DCC_SRC" pandas openai

# --- 2. Seed docs (context — 5 markdown files) ----------------------------
mkdir -p docs
cat > docs/retry_policy.md <<'MD'
# Retry Policy
Configure a retry_policy on any asset by passing RetryPolicy(max_retries=3,
delay=30, backoff=Backoff.EXPONENTIAL). max_retries controls attempts; delay
is seconds between attempts; backoff is linear or exponential.
MD
cat > docs/dynamic_partitions.md <<'MD'
# Dynamic Partitions
DynamicPartitionsDefinition + context.instance.add_dynamic_partitions register
partition keys at runtime. The UI shows a selector so you can materialize a
specific partition on demand — per-tenant, per-file, per-event.
MD
cat > docs/asset_checks.md <<'MD'
# Asset Checks
@asset_check functions return AssetCheckResult(passed, severity, metadata).
Failing checks with severity ERROR block downstream materializations. Use for
schema drift, freshness gates, quality regressions.
MD
cat > docs/freshness.md <<'MD'
# Freshness Policies
FreshnessPolicy(maximum_lag_minutes=60) alerts when an asset gets stale. The
UI plots freshness over time.
MD
cat > docs/automation.md <<'MD'
# Automation Conditions
AutomationCondition.eager() materializes an asset as soon as any upstream
completes. AutomationCondition.on_cron("...") ties to a schedule. Combine
with & (AND) and | (OR).
MD

# --- 3. Write defs.yaml files ---------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# docs_corpus (context — not directly consumed by supervisor in this demo,
# but a good landing spot for future extensions that pipe corpus content
# into a tool's system_message)
mkdir -p "$DEFS/docs_corpus"
cat > "$DEFS/docs_corpus/defs.yaml" <<YAML
type: dagster_community_components.DocumentCorpusComponent
attributes:
  asset_name: docs_corpus
  source_dir: ${PROJECT_ABS}/docs
  file_glob: "**/*.md"
  min_doc_count: 3
  group_name: source
YAML

# The supervisor itself — one YAML declares planner + 3 tools + synthesizer.
mkdir -p "$DEFS/supervisor"
cat > "$DEFS/supervisor/defs.yaml" <<YAML
type: dagster_community_components.SupervisorAgentComponent
attributes:
  plan_asset_name: supervisor_plan
  synthesis_asset_name: supervisor_final_answer
  task: |
    A user asked: "We're seeing intermittent asset failures in a nightly job
    that syncs from a partitioned source. How should we harden this?"
    Diagnose which Dagster mechanisms are relevant, propose a concrete
    configuration, and note quality guardrails.
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.2
  tools:
    - name: retry_policy_expert
      description: "Advises on transient-failure handling via retry_policy, max_retries, delay, backoff."
      system_message: |
        You are a Dagster retry-policy specialist. When asked about
        transient failures, recommend a concrete RetryPolicy(max_retries=,
        delay=, backoff=) configuration and explain the tradeoffs
        between linear and exponential backoff. Keep it under 150 words.

    - name: partitions_expert
      description: "Advises on partitioning strategy — static, time-based, dynamic, per-tenant."
      system_message: |
        You are a Dagster partitions specialist. Recommend a partitioning
        strategy for the user's scenario. Static / daily / dynamic — say
        which and why. If dynamic, explain how partition keys get
        registered at runtime. Keep it under 150 words.

    - name: asset_check_expert
      description: "Advises on data-quality gates — asset checks, severity, blocking downstream materialization."
      system_message: |
        You are a Dagster asset-check specialist. Recommend which
        @asset_check(s) the user should attach: schema drift, row-count
        sanity, freshness. Explain how severity=ERROR blocks downstream
        vs severity=WARN just alerts. Keep it under 150 words.
  synthesis_system_message: |
    You are a Dagster staff engineer synthesizing specialist recommendations
    into a single actionable answer. Combine the specialists' inputs into a
    short 4-6 sentence recommendation for the user. Be concrete: name the
    specific classes/functions and the recommended values.
  group_name: supervisor
YAML

# --- 4. dg check + run ----------------------------------------------------
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

echo ""
echo ">>> Materializing docs_corpus (source context)"
uv run dg launch --assets docs_corpus 2>&1 | tail -3

echo ""
echo ">>> Materializing supervisor_plan (planner LLM picks specialists)"
uv run dg launch --assets supervisor_plan 2>&1 | tail -3

echo ""
echo ">>> Materializing all specialist tool assets (picked ones fill in, others go empty)"
# All 3 tool assets are declared at YAML load — this is the "static DAG shape"
# design. Runtime decisions land as named asset materializations; the tools
# the planner didn't pick materialize as empty DataFrames so the graph stays
# visually consistent across runs.
uv run dg launch --assets retry_policy_expert_result 2>&1 | tail -3
uv run dg launch --assets partitions_expert_result 2>&1 | tail -3
uv run dg launch --assets asset_check_expert_result 2>&1 | tail -3

echo ""
echo ">>> Materializing supervisor_final_answer (synthesizer combines invoked tools)"
uv run dg launch --assets supervisor_final_answer 2>&1 | tail -3

# --- 5. Print the final synthesized answer --------------------------------
echo ""
echo "─────── supervisor_final_answer ───────"
uv run python -c "
import pickle
with open('.dagster_home/storage/supervisor_final_answer', 'rb') as f:
    v = pickle.load(f)
if hasattr(v, 'to_string'):
    print(v.to_string()[:2000])
else:
    print(str(v)[:2000])
" 2>&1 | tail -30

# --- 6. Done --------------------------------------------------------------
cat <<DONE

✓ rag_supervisor demo done.

What just happened:
  1. supervisor_plan (planner LLM) read the task and emitted a DataFrame of
     tool picks: which specialists to invoke, and why. This asset IS the
     runtime decision — inspectable, replayable, gate-able.
  2. Three tool assets (retry_policy_expert_result, partitions_expert_result,
     asset_check_expert_result) materialized. Ones the planner picked ran the
     specialist LLM; ones it didn't pick emitted empty DataFrames so the
     asset graph stays visually consistent across runs.
  3. supervisor_final_answer (synthesizer LLM) read every tool's output and
     produced the final grounded answer.

Every runtime decision is a NAMED asset materialization. That's the pattern
contrast with rag_complete.md's linear retrieval pipeline — different
orchestration shape, same principle: state, not tasks.

Browse:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # Assets → group 'supervisor' → the plan, three tool_result assets, the
  # final synthesized answer, all connected by dependency edges.

Cleanup:
  rm -rf $PROJECT_ABS
DONE
