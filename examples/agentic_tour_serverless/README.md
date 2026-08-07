# agentic_tour — 4 agentic pipelines, one code location, deployable to Dagster+ Serverless

**Everything meaningful is in one file: [`src/agentic_tour/definitions.py`](src/agentic_tour/definitions.py).** Four `AgenticPipelineComponent` instances (debate / route / critique_loop / synthesize) bundled into a single Dagster code location, deployable to Dagster+ Serverless with one command.

## What's inside

| Pipeline | Op showcased | Partitioned across | Typical output |
|---|---|---|---|
| `investment_memo_recommendation` | `debate` (3 proposers + arbitrator) | 3 tickers (NVDA / TSLA / META) | Portfolio committee recommendation + all 3 analyst proposals + arbitrator reasoning |
| `support_triage_routed` | `route` (router picks specialist) | 5 real-world ticket types | Router pick + specialist response + reasoning |
| `press_release_polished` | `critique_loop` (drafter/critic × 2) | 3 product launches | Final press release + iteration history |
| `framework_brief_briefing` | `synthesize` (4 fan-in) | 3 JS frameworks (react/vue/svelte) | Adoption briefing + 4 angle-specific analyses |

14 partitions total, ~35 LLM calls per full backfill, **~$0.02 total cost on gpt-4o-mini**.

Every step is a browsable Dagster asset with typed metadata (`cost_usd`, `latency_ms`, `tokens_total`, `arbitrator_reasoning`, `all_proposals`, `history`, `model_fingerprint`). Filter the catalog by kind (`route` / `debate` / `critique_loop` / `synthesize`) — every asset is tagged.

## Local run

```bash
# From this directory (agentic_tour_serverless/)
uv venv --python 3.12
uv pip install -e .
export OPENAI_API_KEY=sk-...
uv run dg dev
```

Open http://localhost:3000. You'll see all 4 pipelines' assets grouped by pipeline (`investment_committee`, `customer_support`, `marketing`, `platform_research`). Materialize any partition — full metadata lands per asset.

Or headless (all 14 partitions):

```bash
uv run dg launch --assets '*' --partition NVDA
uv run dg launch --assets '*' --partition login_issue
uv run dg launch --assets '*' --partition sso_launch
uv run dg launch --assets '*' --partition react
# ... (or use --partition-range for backfills)
```

## Dagster+ Serverless deploy

**Prereqs** (one-time):
1. A Dagster+ organization + a `prod` deployment.
2. A user API token exported as `DAGSTER_CLOUD_API_TOKEN`.
3. `~/.config/dagster_cloud/` configured with your org + deployment name (via `dagster-cloud config setup`).

**Deploy** (one command):

```bash
uvx --with pex --from dagster-cloud-cli dagster-cloud serverless \
    deploy-python-executable . \
    --location-name agentic_tour \
    --module-name agentic_tour.definitions \
    --python-version 3.12
```

**Set `OPENAI_API_KEY` as a location env var** in the Dagster+ UI (Deployment settings → Environment variables → add `OPENAI_API_KEY`).

That's the whole deploy. No YAML files besides `dagster_cloud.yaml` (4 lines), no Docker, no extra config. `pyproject.toml` declares deps; `deploy-python-executable` bundles them into a pex, uploads it, registers the location.

## What's in this repo

Just three things that matter:

```
agentic_tour_serverless/
├── src/agentic_tour/
│   ├── __init__.py               (empty)
│   └── definitions.py            <-- ALL THE PIPELINE LOGIC (~250 lines)
├── pyproject.toml                (deps + build config)
└── dagster_cloud.yaml            (4-line location manifest)
```

No `defs.yaml`, no components/ directory. The `AgenticPipelineComponent` is instantiated directly in Python — same object as what YAML loading produces, but skipping the YAML layer entirely. `dg.Definitions.merge(...)` combines the 4 pipelines' `.build_defs(...)` outputs into a single code location.

**Partitions are applied in Python** (not via `post_processing:` YAML). See `_apply_static_partitions` in `definitions.py` — takes a built `Definitions` and stamps a `StaticPartitionsDefinition` onto every asset. This is what YAML `post_processing:` does under the hood.

## What this proves

- The `AgenticPipelineComponent` is fully usable from Python — no forced YAML dependency.
- Multiple component instances compose cleanly via `Definitions.merge(...)` into a single code location.
- No boilerplate per-pipeline: each pipeline is one component instantiation.
- The whole thing deploys to Dagster+ Serverless with one command.
- Same YAML schema features (source kinds, ops, sinks) work identically from Python.

## Cost breakdown

| Pipeline | Partitions | LLM calls per partition | ~Cost per partition | Full backfill |
|---|---|---|---|---|
| debate | 3 | 4 (3 proposers + arbitrator) | $0.0005 | $0.0015 |
| route | 5 | 3 (passthrough + router + specialist) | $0.0008 | $0.0040 |
| critique_loop | 3 | 6 (passthrough + drafter + 2×(critic + revise)) | $0.0015 | $0.0045 |
| synthesize | 3 | 5 (4 fan-in + synthesize) | $0.0020 | $0.0060 |
| **Total** | **14** | **~35 calls** | | **~$0.016** |

All cost visible per-partition via `<step>__cost_usd` materialization metadata. Promote to a custom Insights metric via the Dagster+ UI to dashboard + alert on cost regressions.

## Notes on shape choices

- **All 4 pipelines use `gpt-4o-mini`** — cheap enough for a full backfill to cost <$0.02. Swap to `gpt-4o` per step for production quality (each op accepts a per-step `model:` field).
- **Static partition keys are embedded in the Python file** — for real prod you'd read from an external config (e.g. `os.environ["TICKERS"].split(",")`) or wire an upstream asset.
- **The `support_triage` and `press_release` pipelines use a "passthrough llm_call" first step** — an LLM that just looks up the per-partition text from an embedded Python dict — because `AgenticPipelineComponent`'s literal source doesn't support dict-lookup on `{partition_key}` natively. In production you'd source from an upstream asset (a database, a ticketing system export, etc.) instead of embedding the data.
