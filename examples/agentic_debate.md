# Agentic debate — investment memo with 3 analysts + arbitrator (per-ticker partitions)

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is. No local file writes.

**Component:** `AgenticPipelineComponent` (`debate` op).
**Setup script:** [`setup_agentic_debate_demo.sh`](./setup_agentic_debate_demo.sh).
**Cost:** ~$0.001 per ticker (4 LLM calls at `gpt-4o-mini`).

The `debate` op in one YAML: N proposers each write a competing take, an
arbitrator LLM picks the winner. Every proposal, the arbitrator's
reasoning, and the winner are recorded as materialization metadata —
scroll the asset's history to audit any past decision.

## The scenario

An investment committee memo pipeline. For each ticker (NVDA / TSLA /
META), three analyst personas argue for BUY / SELL / HOLD, and the
committee chair (arbitrator) picks the recommendation best suited for a
"moderate-risk, long-horizon institutional portfolio."

## Architecture

```
            source: literal
            "Investment committee memo request. Ticker: {partition_key}.
             Should the portfolio buy, hold, or sell {partition_key}
             at current prices?"
                     │
                     ▼
        ┌──────────────────────────────────────────────────────┐
        │  investment_memo_recommendation   (op: debate)       │
        │                                                      │
        │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐  │
        │  │ bull analyst │ │ bear analyst │ │ neutral      │  │
        │  │ (proposer 1) │ │ (proposer 2) │ │ (proposer 3) │  │
        │  │ argues BUY   │ │ argues SELL  │ │ argues HOLD  │  │
        │  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘  │
        │         │                │                │          │
        │         └────────────────┼────────────────┘          │
        │                          ▼                           │
        │              ┌───────────────────────────┐           │
        │              │  arbitrator (committee    │           │
        │              │  chair) picks winner      │           │
        │              └───────────────────────────┘           │
        └──────────────────────────┬───────────────────────────┘
                                   │
                                   ▼
                        out/{partition_key}/investment_memo.json
                        (JSON sink — bull + bear + neutral cases,
                         arbitrator_reasoning, winner_index, all costs)
```

## What the demo shows

One `AgenticPipelineComponent` YAML. **`{partition_key}` in the source
text** substitutes the ticker at compute time — the same YAML materializes
one memo per ticker.

## What's in the emitted asset's metadata

Click `investment_memo_recommendation` in the Dagster UI → partitions
strip shows NVDA / TSLA / META. Click any ticker → materialization
metadata shows:

| Field | Contents |
|---|---|
| `text` | Markdown of the winning recommendation (rendered inline) |
| `winner_index` | Which proposer won (0=bull, 1=bear, 2=neutral) |
| `arbitrator_reasoning` | Free-text — WHY the committee chair picked that one |
| `proposals` | JSON with each proposer's full case (bull + bear + neutral) |
| `cost_usd` | This step's total cost (~$0.001) |
| `latency_ms` | End-to-end latency |
| `n_llm_calls` | 4 (3 proposers + 1 arbitrator) |
| `model_fingerprint` | `[gpt-4o-mini,gpt-4o-mini,gpt-4o-mini]→gpt-4o-mini` |

That's the "audit every AI decision" story — no log-grepping to find
why the committee picked HOLD instead of BUY on 2026-03-05.

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_debate_demo.sh \
  -o setup_agentic_debate_demo.sh
bash setup_agentic_debate_demo.sh
```

Requirements: `uv` + `OPENAI_API_KEY`. ~30s setup + ~10s per ticker to
materialize.

```bash
cd agentic-debate-demo
DAGSTER_HOME=$(pwd)/.dagster_home uv run dg dev
# UI at http://localhost:3000

# Or backfill all 3 tickers headlessly
DAGSTER_HOME=$(pwd)/.dagster_home uv run dg launch --assets '*' --partition NVDA
DAGSTER_HOME=$(pwd)/.dagster_home uv run dg launch --assets '*' --partition TSLA
DAGSTER_HOME=$(pwd)/.dagster_home uv run dg launch --assets '*' --partition META
```

Per-ticker JSON artifacts land at:

```
$PROJECT/out/NVDA/investment_memo.json
$PROJECT/out/TSLA/investment_memo.json
$PROJECT/out/META/investment_memo.json
```

Each has all 3 proposals + arbitrator reasoning + winner — full audit
trail for the committee record.

## Authored from this NL prompt (coding-agent path)

The debate YAML the setup script writes can be composed by Claude Code
(or Cursor / Copilot) from a plain-English prompt. In a bare directory:

```bash
uvx create-dagster@latest project investment-memo-demo --no-uv-sync
cd investment-memo-demo
uvx --from dagster-community-components-cli dagster-component init
```

Then tell the assistant:

> *Build an investment committee memo pipeline in one
> `AgenticPipelineComponent` using the `debate` op. Partition over
> tickers `[NVDA, TSLA, META]`. The source is a literal
> `"Investment committee memo request. Ticker: {partition_key}. Buy,
> hold, or sell?"`. Emit one asset — `investment_memo_recommendation`
> — with three proposers (bull argues BUY, bear argues SELL, neutral
> argues HOLD with a target price range) and a committee-chair
> arbitrator that picks the recommendation best for a moderate-risk,
> long-horizon institutional portfolio. All LLMs use `gpt-4o-mini`
> and `OPENAI_API_KEY`. JSON-sink to `out/{partition_key}/investment_memo.json`.
> Use `dagster-component schema agentic_pipeline` to verify the debate
> op's field names.*

The assistant runs `dagster-component add agentic_pipeline`, composes
the defs.yaml (with `post_processing:` for the static partitions), and
`dagster definitions validate` passes.

**When to reach for the NL prompt vs. the setup script:** the setup
script is a one-shot `curl | bash` that ships this exact demo end-to-end.
The NL prompt is what a customer would type into Claude Code to author
the same thing from scratch — useful for demoing the authoring flow,
and easy to tweak (change the tickers, swap analyst personas, change
the arbitrator's mandate). If you want the NL prompt in git rather than
the YAML, see the PCA-authored sibling:
[`pca_investment_memo.md`](./pca_investment_memo.md).

## Extending

- **More analysts.** Add a fourth persona to `proposers:` (e.g. a
  quantitative analyst focused on multiples). No code — one YAML block.
- **Different arbitrator model.** Swap the arbitrator's `model:` from
  `gpt-4o-mini` to `gpt-4o` (or `claude-haiku-4-5-20251001`, or
  `gemini/gemini-2.5-flash` — anything LiteLLM supports).
- **Real ticker data as source.** Replace `kind: literal` with
  `kind: url` fetching a yfinance / Alpha Vantage API per partition.
  Each partition now debates over live market data.
- **Chain into portfolio actions.** Add a downstream asset that reads
  `winner_index` from `investment_memo_recommendation`'s metadata and
  posts to a Slack channel / OMS webhook via a
  `slack_notification_sink` or `rest_api_sink`.

## Related patterns

- [**Agentic Pipeline (full 5-op tour)**](./agentic_pipeline.md) — same
  substrate, all five ops in one demo (`llm_call` + `route` +
  `critique_loop` + `debate` + `synthesize`).
- [**Typed named inputs**](./typed_named_inputs.md) — v2 wiring for
  multi-input joins across arbitrary prior steps.
- [**PCA-authored variant**](./pca_investment_memo.md) — same debate
  pipeline, authored by `PlannedCatalogAgentComponent` from a
  natural-language task instead of hand-written YAML.
- [**Maintainer investigation room**](./maintainer_investigation_room.md)
  — the full ceremony version with typed inputs, MCP, and human sign-off.
