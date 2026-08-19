#!/usr/bin/env bash
# local_ai_ab — the "should we go local" empirical answer as a Dagster
# pipeline. Same prompt through N LLM providers side-by-side; each
# response an asset with cost/latency/token metadata; an LLM-as-judge
# scores them comparatively; a cost report aggregates the whole thing
# into a single "here's what going local saves you" asset.
#
# ## What this scaffolds
#
#   InferenceProviderABTestComponent → 3 provider assets
#         │
#         ▼
#   ProviderABEvaluatorComponent → 1 scored asset (LLM-as-judge)
#         │
#         ▼
#   InferenceCostReportComponent → 1 report asset (recommendation + savings)
#
# ## Needed
#   - OPENAI_API_KEY (always — the judge uses gpt-4o)
#   - ANTHROPIC_API_KEY (optional — adds a Claude candidate)
#   - OLLAMA_URL (optional — adds a local Ollama candidate)
#   - uv
#
# ## Cost
#   ~$0.005 per full A/B+evaluator+report run (gpt-4o-mini candidate + gpt-4o judge).

set -eo pipefail

PROJECT_DIR="${1:-local-ai-ab-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — required for the judge + at least one candidate."
  echo "  export OPENAI_API_KEY=sk-... and re-run."
  exit 1
fi

info()  { echo "→ $*"; }
ok()    { echo "✓ $*"; }
fail()  { echo "✗ $*"; exit 1; }

# --- 1. fresh project ------------------------------------------------------
rm -rf "$PROJECT_DIR"
info "scaffolding Dagster project…"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -2
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(basename "$PROJECT_ABS" | tr '-' '_')"

# --- 2. deps + env ---------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  info "using local DCC checkout: $DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

info "installing deps…"
uv add -q "$DCC_SRC" 'litellm>=1.30.0' 2>&1 | tail -1

# --- 3. detect which providers to include ---------------------------------
PROVIDERS_YAML="    - alias: gpt_4o_mini
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY"

if [ -n "$ANTHROPIC_API_KEY" ]; then
  PROVIDERS_YAML="$PROVIDERS_YAML
    - alias: claude_haiku
      model: claude-3-5-haiku-latest
      api_key_env_var: ANTHROPIC_API_KEY"
  ok "detected ANTHROPIC_API_KEY — including Claude candidate"
else
  info "ANTHROPIC_API_KEY not set — skipping Claude candidate (still a valid demo w/ OpenAI + Ollama, or OpenAI alone)"
fi

if [ -n "$OLLAMA_URL" ]; then
  OLLAMA_MODEL="${OLLAMA_MODEL:-ollama/qwen2.5:14b}"
  PROVIDERS_YAML="$PROVIDERS_YAML
    - alias: qwen_local
      model: $OLLAMA_MODEL
      api_base_env_var: OLLAMA_URL
      cost_per_1k_tokens_override: 0.0"
  ok "detected OLLAMA_URL — including local Ollama ($OLLAMA_MODEL) candidate"
else
  info "OLLAMA_URL not set — skipping local Ollama candidate (start ollama + export OLLAMA_URL=http://localhost:11434 to include)"
fi

# --- 4. defs.yaml files ---------------------------------------------------
mkdir -p "src/$PKG/defs/triage_ab"
cat > "src/$PKG/defs/triage_ab/defs.yaml" <<EOF
type: dagster_community_components.InferenceProviderABTestComponent
attributes:
  asset_name_prefix: triage_ab
  group_name: local_ai_ab

  prompt:
    kind: literal
    text: |
      Triage this GitHub issue in ≤3 lines. Classify it as one of:
      defect | expected-behavior | needs-more-info | docs-gap | feature-request.
      One-line rationale citing SPECIFIC evidence. One-line concrete next action.

      Issue: "IO manager returns None when the upstream asset is a partitioned
      multi_asset with can_subset=False — I get 'NoneType has no attribute keys'
      at load_input time. Dagster 1.13.18, s3_parquet_io_manager,
      DailyPartitionsDefinition start_date 2026-01-01."

  providers:
$PROVIDERS_YAML

  system_prompt: |
    You are a rigorous, terse issue triage assistant. No preamble.
  temperature: 0.0
  max_tokens: 200
EOF
ok "wrote triage_ab/defs.yaml (InferenceProviderABTestComponent)"

# Build the evaluator's candidate list dynamically to match providers.
CANDIDATES_YAML="    - triage_ab_gpt_4o_mini"
if [ -n "$ANTHROPIC_API_KEY" ]; then
  CANDIDATES_YAML="$CANDIDATES_YAML
    - triage_ab_claude_haiku"
fi
if [ -n "$OLLAMA_URL" ]; then
  CANDIDATES_YAML="$CANDIDATES_YAML
    - triage_ab_qwen_local"
fi

mkdir -p "src/$PKG/defs/triage_ab_scored"
cat > "src/$PKG/defs/triage_ab_scored/defs.yaml" <<EOF
type: dagster_community_components.ProviderABEvaluatorComponent
attributes:
  asset_name: triage_ab_scored
  group_name: local_ai_ab

  candidates:
$CANDIDATES_YAML

  rubric:
    kind: literal
    text: |
      Score each candidate response on 0-100 across three dimensions
      (weights in parens — final score = sum):

      1. Classification accuracy (40 pts). Did the candidate correctly pick
         one of the five allowed classifications for what's clearly a
         defect (or needs-more-info)? Zero pts if missing or wrong.
      2. Rationale grounding (30 pts). Does the rationale cite SPECIFIC
         evidence from the issue (versions, symptoms, repro elements)?
         Generic reasoning = 0-10.
      3. Next-action concreteness (30 pts). Is the next action actionable
         and specific to this issue? Generic advice like "investigate
         further" = 0-5.

  judge:
    model: gpt-4o
    api_key_env_var: OPENAI_API_KEY
    temperature: 0.0
    max_tokens: 1500

  baseline_alias: gpt_4o_mini
  # Merge-gate: block promotion if winner's quality drops below 70/100.
  min_winner_score: 70
EOF
ok "wrote triage_ab_scored/defs.yaml (ProviderABEvaluatorComponent)"

mkdir -p "src/$PKG/defs/triage_ab_report"
cat > "src/$PKG/defs/triage_ab_report/defs.yaml" <<EOF
type: dagster_community_components.InferenceCostReportComponent
attributes:
  asset_name: triage_ab_report
  group_name: local_ai_ab

  candidates:
$CANDIDATES_YAML

  evaluator: triage_ab_scored
  baseline_alias: gpt_4o_mini

  # Back-of-envelope: at 10k triage calls/day, what does each alternative save?
  projected_daily_volume: 10000

  # 70% weight on quality vs cost in the value_score composite.
  quality_weight: 0.7
EOF
ok "wrote triage_ab_report/defs.yaml (InferenceCostReportComponent)"

# --- 5. validate ----------------------------------------------------------
info "dg check defs…"
uv run dagster definitions validate 2>&1 | tail -5 || fail "definitions failed to load"

# --- 6. materialize the whole pipeline ------------------------------------
DM="${PKG}.definitions"

info "running the A/B (candidates in parallel, each an asset)…"
CANDIDATE_SELECT="triage_ab_gpt_4o_mini"
if [ -n "$ANTHROPIC_API_KEY" ]; then
  CANDIDATE_SELECT="$CANDIDATE_SELECT,triage_ab_claude_haiku"
fi
if [ -n "$OLLAMA_URL" ]; then
  CANDIDATE_SELECT="$CANDIDATE_SELECT,triage_ab_qwen_local"
fi
uv run dagster asset materialize --select "$CANDIDATE_SELECT" -m "$DM" 2>&1 | tail -3 || fail "A/B failed"
ok "candidates materialized"

info "scoring candidates (LLM-as-judge — one call, all candidates)…"
uv run dagster asset materialize --select triage_ab_scored -m "$DM" 2>&1 | tail -3 || fail "evaluator failed"
ok "candidates scored"

info "generating cost report…"
uv run dagster asset materialize --select triage_ab_report -m "$DM" 2>&1 | tail -3 || fail "report failed"
ok "report generated"

# --- 7. summary -----------------------------------------------------------
echo
ok "Demo complete."
echo
cat <<EOF
Three-stage A/B pipeline ran end-to-end as first-class Dagster assets:

  1. triage_ab_<provider> (×N) — same prompt through each candidate,
     cost + latency + tokens in metadata
  2. triage_ab_scored — LLM-as-judge scored all candidates in one pass,
     winner picked, delta vs baseline computed
  3. triage_ab_report — aggregate: per-provider cost + quality + baseline
     deltas + projected daily savings at 10k volume + recommendation

Open the UI to browse the graph + metadata:
  cd $PROJECT_ABS
  DAGSTER_HOME=$DAGSTER_HOME uv run dg dev

The report asset (triage_ab_report) has a markdown comparison table in
its materialization metadata — that's your "should we go local" answer.

For scale: partition the pipeline on a dynamic partition keyed to your
real prompt corpus (say, one partition per triaged issue). Materialize
daily. In Dagster+ Insights the report becomes a time-series — cost
curves per provider, quality curves, projected savings — queryable +
PR-linkable.

The merge-gate pattern: ProviderABEvaluatorComponent emits an asset
check winner_meets_threshold that fails ERROR if the winner's score
drops below the threshold you set (min_winner_score: 70 here). Wire that
into branch-deploy CI (dagster asset materialize --select triage_ab_scored
in the PR runner) — the check FAILS ERROR when quality drops, blocking
the merge automatically.

Add candidates: export ANTHROPIC_API_KEY + OLLAMA_URL and re-run this
setup script.
EOF
