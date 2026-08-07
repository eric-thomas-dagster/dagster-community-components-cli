#!/usr/bin/env bash
# agentic_debate — investment memo with bull / bear / neutral analysts + arbitrator.
#
# Highlights the `debate` op on a realistic, business-visceral use case:
# three analysts propose different recommendations for the same ticker, an
# arbitrator (portfolio committee chair) picks the winner. Materialized once
# per ticker; every decision is a browsable per-partition asset with the
# arbitrator's reasoning + every losing proposal in the metadata.
#
# Partitioned across 3 tickers → 3 independent debates → 3 browsable per-
# partition materializations. Total cost: ~$0.005 for a full 3-partition
# backfill (all gpt-4o-mini, ~12 LLM calls).

set -eo pipefail

PROJECT_DIR="${1:-agentic-debate-demo}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "✗ OPENAI_API_KEY not set — get one at https://platform.openai.com/api-keys"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null 2>&1
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(ls src/ | head -1)"
mkdir -p out

echo ">>> Adding deps"
uv add -q litellm requests
uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing agentic_pipeline component"
$CLI add agentic_pipeline --auto-install >/dev/null 2>&1

cat > "src/$PKG/defs/agentic_pipeline/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: investment_memo
  group_name: investment_committee

  # {partition_key} = ticker. Same question shape per partition, different asset.
  source:
    kind: literal
    text: "Investment committee memo request. Ticker: {partition_key}. Should the portfolio buy, hold, or sell {partition_key} at current prices? Consider recent performance, competitive position, and material risks. Give a concrete recommendation."

  steps:
    - id: recommendation
      op: debate
      # 3 proposers = 3 analyst perspectives. All same model, different personas.
      proposers:
        - model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "You are a bull analyst. Argue for BUY. Cite growth vectors, competitive moats, and any recent positive catalysts. Be specific and quantitative. One paragraph."
          temperature: 0.8
          max_tokens: 500
        - model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "You are a bear analyst. Argue for SELL or aggressive underweight. Cite valuation risk, competitive threats, and any material headwinds. Be specific and quantitative. One paragraph."
          temperature: 0.8
          max_tokens: 500
        - model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "You are a neutral analyst. Argue for HOLD with a specific target price range. Present the balanced case — one bull argument, one bear argument, one wait-and-see catalyst. One paragraph."
          temperature: 0.8
          max_tokens: 500
      arbitrator:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY
        system_prompt: "You are the portfolio committee chair. Pick the recommendation best suited for a moderate-risk, long-horizon institutional portfolio. Focus on risk-adjusted return, not maximum upside. Explain why in 1-2 sentences."

  outputs:
    assets: [recommendation]
    json_sinks:
      - from: recommendation
        path: "out/{partition_key}/investment_memo.json"

post_processing:
  assets:
    - target: "*"
      attributes:
        partitions_def:
          type: static
          partition_keys:
            - NVDA
            - TSLA
            - META
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # UI at http://localhost:3000

Or backfill all 3 tickers headlessly (~\$0.005 total):

  uv run dg launch --assets '*' --partition NVDA
  uv run dg launch --assets '*' --partition TSLA
  uv run dg launch --assets '*' --partition META

In the UI:
  - Click investment_memo_recommendation → partitions strip shows NVDA / TSLA / META.
  - Click any ticker → metadata shows: winning recommendation text, winner_index
    (which analyst won), arbitrator_reasoning (why the committee chair picked
    that one), all_proposals (bull + bear + neutral cases in full), cost, latency.
  - This is the "audit every AI decision" story — no log-grepping to find why
    the committee picked what it picked on which day.

Per-ticker artifacts (full debate JSON, all proposals + arbitrator reasoning):

  $PROJECT_ABS/out/NVDA/investment_memo.json
  $PROJECT_ABS/out/TSLA/investment_memo.json
  $PROJECT_ABS/out/META/investment_memo.json
MSG
