#!/usr/bin/env bash
# agentic_pipeline — one YAML declares a whole 5-step agentic workflow,
# PARTITIONED across 3 distinct questions.
#
# Shows off:
#   - All 5 ops (llm_call, route, debate, critique_loop, synthesize) in one pipeline
#   - Per-step assets with rich typed metadata: cost_usd (Float), latency_ms (Int),
#     tokens_total (Int), model_fingerprint (Text), materialized_at (Timestamp), op (Text)
#   - Per-step Dagster kinds: route/debate/critique_loop/synthesize/llm_call
#   - text_sinks + json_sinks with {partition_key}-templated paths
#   - **Static partitions** — 3 distinct questions become 3 browsable per-partition
#     materializations. Time-travel to any decision is one click.
#
# The "Why Dagster" story this demo proves:
#   1. Every step's decision (router pick, arbitrator reasoning, critique history)
#      is a browsable asset materialization per partition — no log-grepping.
#   2. cost_usd + latency_ms + tokens_total are typed numeric metadata →
#      Dagster+ Insights turns them into dashboards + alerts for free.
#   3. `dagster/kind/<op>` on every asset means "show me every debate step"
#      is one catalog filter across the whole org.
#   4. Backfilling 3 partitions materializes 3 independent agent decisions,
#      each browsable via the partition selector.
#
# Total cost: ~$0.003 for a full 3-partition backfill (all gpt-4o-mini, ~30 LLM calls).

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

echo ">>> Installing components: synthetic_prompt_generator + agentic_pipeline"
$CLI add synthetic_prompt_generator --auto-install >/dev/null 2>&1
$CLI add agentic_pipeline             --auto-install >/dev/null 2>&1

# The CLI drops a copy of each component's example.yaml at
# src/$PKG/defs/<name>/defs.yaml. We're writing our own defs.yaml under
# defs/prompts/ (with topic-based partitioning), so remove the CLI's
# auto-installed defs so dg check doesn't scan two conflicting shapes.
rm -rf "src/$PKG/defs/synthetic_prompt_generator"

# Sinks land in project-relative `out/` — durable locally, ephemeral in
# Serverless (see defs.yaml comments below).
mkdir -p "$PROJECT_ABS/out"

echo ">>> Writing prompt-source defs.yaml (SyntheticPromptGeneratorComponent, Mode D)"
mkdir -p "src/$PKG/defs/prompts"
cat > "src/$PKG/defs/prompts/defs.yaml" <<EOF
# SyntheticPromptGeneratorComponent — Mode D (COMPOSED, no LLM).
#
# Emits a str-valued asset named research_prompts. Static partitions = the
# topics list. Each partition materializes to one prompt string composed
# deterministically from the levers below. Seed=42 → reproducible bit-for-bit.
#
# The downstream AgenticPipelineComponent picks this up via
# source: {kind: upstream_asset, upstream_asset_key: research_prompts}.
# Dagster stitches the partitioned dependency automatically.
type: $PKG.components.synthetic_prompt_generator.component.SyntheticPromptGeneratorComponent
attributes:
  asset_name: research_prompts
  group_name: prompts
  topics:
    - attention
    - transformer_vs_rnn
    - retrieval_augmented_generation
  # v1.5 levers — pure Python, no LLM cost:
  persona: engineer          # opener: "Cover attention for someone who ships production systems."
  style: technical           # word choice: "Include the actual technical detail..."
  length: medium             # ~250 words
  format_hint: paragraphs    # requested answer format
  depth: intermediate        # assumed reader background
  seed: 42                   # deterministic composition
  # count_per_topic: 3       # uncomment to emit 3 variants per topic (9 partitions total)
EOF

cat > "src/$PKG/defs/agentic_pipeline/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: research_bot
  group_name: agents

  # Source is now the upstream research_prompts asset — no on-disk question
  # files needed. Dagster wires the partitioned dependency automatically
  # (both this and research_prompts share the same 3 topic partitions).
  # Serverless-safe: no files to ship, no path issues.
  source:
    kind: upstream_asset
    upstream_asset_key: research_prompts

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
    # RELATIVE paths — same YAML works locally + Dagster+ Serverless.
    # (Absolute /tmp/... paths would bake local machine paths into the YAML
    # and fail on Serverless. Also: block style — not inline {} — because
    # flow-mapping {} conflicts with {partition_key} templates in values.)
    #
    # On Serverless, filesystem sinks are ephemeral (per-run container).
    # For durable outputs across runs, swap text/json sinks for table_sinks
    # into a warehouse — the interesting decision data is already in the
    # materialization metadata regardless.
    text_sinks:
      - from: final
        path: "out/{partition_key}/final_answer.txt"
    json_sinks:
      - from: debated
        path: "out/{partition_key}/debate_transcript.json"
      - from: refined
        path: "out/{partition_key}/critique_history.json"

# post_processing overrides make every pipeline-emitted asset partitioned
# on the SAME topics as the upstream research_prompts asset — Dagster then
# wires per-partition dependencies automatically.
post_processing:
  assets:
    - target: "*"
      attributes:
        partitions_def:
          type: static
          partition_keys:
            - attention
            - transformer_vs_rnn
            - retrieval_augmented_generation
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # open UI at http://localhost:3000

Or backfill all 3 partitions headlessly (~\$0.003 total):

  uv run dg launch --assets '*' --partition attention
  uv run dg launch --assets '*' --partition transformer_vs_rnn
  uv run dg launch --assets '*' --partition retrieval_augmented_generation

Then in the UI:
  - Click research_bot_debated → partitions strip shows all 3 topics,
    each with independent metadata (cost_usd, arbitrator_reasoning, all_proposals).
  - Click research_prompts (the upstream) → see the composed prompt for
    each topic. Iterate on prompts by editing the levers in
    prompts/defs.yaml (persona / style / length / format_hint / depth);
    no code, no on-disk question files.
  - Bump count_per_topic in prompts/defs.yaml to emit multiple variants
    per topic (partition keys become {topic}__v0 / {topic}__v1 / ...) —
    great for eval sweeps.

Per-partition sinks land at (relative to project dir):
  out/attention/final_answer.txt
  out/transformer_vs_rnn/final_answer.txt
  out/retrieval_augmented_generation/final_answer.txt
  (+ debate_transcript.json + critique_history.json in each partition dir)

Deploying to Dagster+ Serverless? The defs.yaml uses RELATIVE paths so
the same file works on Serverless — the prompt-generator component
composes prompts in-process (no files ship), text/json sinks land inside
the run container (ephemeral). For durable outputs across runs, replace
text_sinks / json_sinks with table_sinks targeting a warehouse. All
decision metadata is already in each asset's materialization metadata
regardless of sinks — that's what you actually want to browse
post-deploy anyway.
MSG
