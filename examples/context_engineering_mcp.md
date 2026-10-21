# The Closed Context-Receipt Loop, Served Over Real MCP

Same closed loop as [The Closed Context-Receipt Loop](context_engineering.md) -- build a knowledge base,
serve real queries, record real feedback, rebuild with the identical recipe, watch the index get better --
but with Dagster doing **only context engineering**. Serving, the agent, and recording feedback all run
outside Dagster entirely, as two plain long-running processes speaking real MCP.

If `context_engineering.md` answers "can one component build the index and another serve it," this demo
answers "does Dagster have to be the thing a live agent calls on every request." It doesn't.

## Architecture

```
support_kb_articles ──▶ support_kb_chunks ──▶ support_kb_classified ──▶ support_kb_embedded ──▶ support_kb
(DataframeFromCsv)      (ContextEngineeringPipelineComponent, per_step_ops: true --
                          chunk → classify → embed → write_vector_store, one real asset per step)
                                                                                      │
                                                                                      ▼
                                                                (chromadb index + sqlite receipt store)
                                                                                      │
                                                                                      │  Dagster's job ends here.
                                                                                      ▼
                                                                    context_gateway.py  (FastMCP server)
                                                                      search_context / record_feedback / explain
                                                                                      ▲
                                                                                      │  real MCP, stdio subprocess
                                                                                      │
                                                                                  agent.py  (standalone agent, no Dagster import)
```

`per_step_ops: true` means `support_kb` isn't one asset node here -- it's four: `support_kb_chunks`,
`support_kb_classified`, `support_kb_embedded`, and `support_kb` itself (the last step,
`write_vector_store`, keeps the plain name so the gateway's `source_asset_key` reference stays unchanged).
Each one has its own materialization history and its own metadata, and (via Dagster's native
re-execution-from-failure) can be retried independently -- same shape as
[`context_engineering.md`](context_engineering.md)'s own build side, just visible as separate nodes in the
asset graph instead of one.

`context_gateway.py` and `agent.py` are plain Python files sitting next to the Dagster project --
not in `defs/`, not components, not known to Dagster at all. They just happen to read the same
ChromaDB path and SQLite file `support_kb` wrote.

## What the script does

1. **Build 1** (Dagster) -- `support_kb_articles` → `support_kb_chunks` → `support_kb_classified` →
   `support_kb_embedded` → `support_kb`, `code_version: recipe-1`. Six support-KB articles get chunked,
   classified, embedded, and indexed -- each step its own asset node (`per_step_ops: true`). Nothing
   learned yet.
2. **Serve the same question six times** -- `agent.py` spawns `context_gateway.py` as a real subprocess
   over stdio (the same way Claude Desktop or Claude Code launches an MCP server) and calls its
   `search_context` tool. Each call **writes** a receipt (`ContextReceiptStore.record_retrieval`) with the
   same provenance `context_engineering_pipeline` stamped onto the chunks it retrieves.
3. **Record real negative feedback** -- each round also calls `record_feedback`, which **writes** a
   `thumbs_down` outcome onto that round's receipt (`ContextReceiptStore.record_outcome`). Still no Dagster
   involved -- this is the gateway, standing in for a real support UI's thumbs-down button.
4. **Build 2** (Dagster) -- same `defs.yaml`, same `code_version: recipe-1`. `support_kb`'s `receipts:`
   block reads the feedback the gateway wrote, and `quarantine: {mode: exclude}` removes the chunks that
   kept failing.
5. **Ask the same question again** -- via the agent, over MCP, same as every other round -- served from
   different chunks, because the old ones are gone from the index.

Free/local throughout: HuggingFace zero-shot classification, sentence-transformers embeddings, a local
ChromaDB index, a local SQLite receipt store. No API key required for the core loop. Set `OPENAI_API_KEY`
and `agent.py` also composes a real generated answer via `litellm` -- still with no Dagster component
anywhere in that call path.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_context_engineering_mcp_demo.sh \
  -o setup_context_engineering_mcp_demo.sh
bash setup_context_engineering_mcp_demo.sh
```

Takes a few minutes -- `sentence-transformers` and the HuggingFace zero-shot model download once and are
cached for the rest of the run, and each `agent.py` call spawns a fresh gateway subprocess (its own model
load) by design, matching how a real external client would connect.

Browse the Dagster side:

```bash
cd context-engineering-mcp-demo
uv run dg dev
# http://localhost:3000 -- support_kb_articles → support_kb_chunks →
# support_kb_classified → support_kb_embedded → support_kb: one real asset node
# per step. No rag_pipeline, no questions/answers assets: Dagster's whole job
# here is build + rebuild.
```

Talk to the gateway yourself after the script finishes:

```bash
python agent.py "How do I track my shipment?"
python agent.py "Why was I charged twice for my subscription?" --rate thumbs_up
```

Run the gateway standalone -- any MCP-compatible client can connect, not just `agent.py`:

```bash
python context_gateway.py          # stdio
# or: fastmcp run context_gateway.py
```

## Why build this as two processes instead of one Dagster asset

- **`context_engineering_pipeline`** is the only thing that needs Dagster: build/rebuild is a batch job with
  real inputs, real outputs, a code version, and (via `receipts:`) a reason to re-run. That's exactly the
  shape orchestration tools are for.
- **Serving a live query is not a batch job.** It's a synchronous request an agent (or a person) is
  waiting on. Routing that through Dagster would mean either a long-running sensor-driven asset or
  launching a run per question -- both fighting the grain of what an orchestrator is good at. A small
  FastMCP server that just reads an already-built index is the right shape for that instead.
- **The agent doesn't need to know Dagster exists.** `agent.py` never imports `dagster`. Any MCP client --
  Claude Desktop, Claude Code, a LangGraph agent, your own backend -- could stand in for it unchanged,
  because `context_gateway.py` only ever speaks MCP tool calls, never Dagster's APIs.

## Related

- [The Closed Context-Receipt Loop](context_engineering.md) -- the same loop built entirely with
  Dagster components (`rag_pipeline` as the serving layer instead of a FastMCP gateway). Start there if
  you'd rather have the query side show up as a Dagster asset with its own run history; come here once you
  need serving decoupled from the orchestrator.
