#!/usr/bin/env bash
# agentic_pipeline — one YAML declares a whole 5-step agentic workflow.
#
# Shows off:
#   - All 5 ops (llm_call, route, debate, critique_loop, synthesize) in one pipeline
#   - Per-step assets with rich typed metadata: cost_usd (Float), latency_ms (Int),
#     tokens_total (Int), model_fingerprint (Text), materialized_at (Timestamp), op (Text)
#   - Per-step Dagster kinds: route/debate/critique_loop/synthesize/llm_call
#     — filterable in the asset catalog
#   - text_sinks + json_sinks writing per-partition-aware output files
#
# The "Why Dagster" story this demo proves:
#   1. Every step's decision (router pick, arbitrator reasoning, critique history)
#      is a browsable asset materialization — no log-grepping.
#   2. cost_usd + latency_ms + tokens_total are typed numeric metadata →
#      Dagster+ Insights turns them into dashboards + alerts for free.
#   3. `dagster/kind/<op>` on every asset means "show me every debate step"
#      is one catalog filter across the whole org.
#
# Total cost: ~$0.001 per full-pipeline run (all gpt-4o-mini, ~10 LLM calls).

set -eo pipefail

PROJECT_DIR="${1:-agentic-pipeline-demo}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "✗ OPENAI_API_KEY not set — the pipeline calls OpenAI and will error at materialize time."
  echo "  Get a key at: https://platform.openai.com/api-keys"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null 2>&1
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q litellm requests
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing agentic_pipeline component"
$CLI add agentic_pipeline --auto-install >/dev/null 2>&1

# All sink paths stay INSIDE $PROJECT_ABS — Windows-portable.
mkdir -p "$PROJECT_ABS/out"

cat > "src/$PKG/defs/agentic_pipeline/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: research_bot
  group_name: agents

  source:
    kind: literal
    text: "Explain in 3-4 sentences how attention works in transformer language models."

  steps:
    # 1) simplest op — one LLM call over source text
    - id: baseline
      op: llm_call
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      max_tokens: 400

    # 2) router picks best specialist LLM — router__reasoning surfaces in materialization metadata
    - id: routed
      op: route
      source: source
      router:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY
      specialists:
        - name: technical
          description: "Deep CS / ML / systems questions. Precise, formula-friendly."
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "You are a senior ML engineer. Concise, precise, technical answers."
          max_tokens: 400
        - name: general
          description: "General knowledge, everyday questions, non-technical topics."
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "You are a helpful assistant. Explain clearly for a general audience."
          max_tokens: 400
      fallback: general

    # 3) critique_loop — drafter revises against critic feedback; full history in metadata
    - id: refined
      op: critique_loop
      source: routed
      drafter:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY
        system_prompt: "You are a technical writer. Rewrite for clarity while preserving accuracy."
        max_tokens: 400
      critic:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY
        system_prompt: "Critique for clarity + correctness. Be specific."
        max_tokens: 300
      iterations: 1

    # 4) debate — two proposers, arbitrator picks the stronger version
    - id: debated
      op: debate
      source: refined
      proposers:
        - model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Rewrite to be more accessible to a beginner. One paragraph."
          temperature: 0.7
          max_tokens: 400
        - model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Rewrite to be more precise / rigorous. One paragraph."
          temperature: 0.7
          max_tokens: 400
      arbitrator:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY
        system_prompt: "Pick the proposal that best balances clarity + correctness for a technical reader."

    # 5) synthesize — merge all four artifacts into a polished final answer
    - id: final
      op: synthesize
      sources: [baseline, routed, refined, debated]
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: "Combine the labeled sections into a single polished response."
      max_tokens: 500

  outputs:
    assets: [baseline, routed, refined, debated, final]
    text_sinks:
      - {from: final, path: $PROJECT_ABS/out/final_answer.txt}
    json_sinks:
      - {from: debated, path: $PROJECT_ABS/out/debate_transcript.json}
      - {from: refined, path: $PROJECT_ABS/out/critique_history.json}
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev

Open http://localhost:3000 and:
  - Materialize the assets (research_bot_baseline / _routed / _refined / _debated / _final).
  - Click any asset → Materialization tab → see cost_usd, latency_ms, tokens_total,
    model_fingerprint, materialized_at, router_reasoning, arbitrator_reasoning, etc.
  - Rerun the assets → each new materialization is browsable with its own metadata.
    That is the "browse decision history" story you can't get from a job runner.

Sink files land at:
  $PROJECT_ABS/out/final_answer.txt
  $PROJECT_ABS/out/debate_transcript.json
  $PROJECT_ABS/out/critique_history.json
MSG
