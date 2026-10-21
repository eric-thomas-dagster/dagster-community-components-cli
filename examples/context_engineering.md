# The Closed Context-Receipt Loop

`context_engineering_pipeline` (build side) and `rag_pipeline` (query side) are
Dagster's own take on context engineering: chunk, embed, classify, and govern raw
text into a searchable, citeable knowledge base — and close the loop so every
rebuild learns from what was actually retrieved and how it turned out.

This demo runs that whole loop with real components, nothing mocked: build a support
knowledge base, serve the same question six times, record real negative feedback on
what got served, rebuild with the **identical recipe**, and watch the chunks people
kept rating badly disappear from the index — with no manual re-tuning.

## Architecture

```
support_kb_articles ──▶ support_kb_chunks ──▶ support_kb_classified ──▶ support_kb_embedded ──▶ support_kb ──▶ (chromadb index)
(DataframeFromCsv)      (ContextEngineeringPipelineComponent, per_step_ops: true --
                          chunk → classify → embed → write_vector_store, one real asset per step)
                          receipts: {resource_key: context_receipts,
                                     quarantine: {mode: exclude}}
                                                                                      │
                                                                                      ▼
                                                                    context_receipts (ContextReceiptStoreComponent,
                                                                                       local SQLite)
                                                                                      ▲
                                                                                      │
support_questions ──▶ support_answers
(DataframeFromCsv)     (RAGPipelineComponent: retrieve [+ generate, optional])
                        receipts: {resource_key: context_receipts,
                                   context_name: support_kb}
```

`per_step_ops: true` means `support_kb` isn't one asset node here -- it's four: `support_kb_chunks`,
`support_kb_classified`, `support_kb_embedded`, and `support_kb` itself (the last step, `write_vector_store`,
keeps the plain name, so `support_answers`' `context_name: support_kb` reference stays unchanged). Each one
has its own materialization history and its own metadata, and (via Dagster's native
re-execution-from-failure) can be retried independently.

Both components write to and read from the **same** `context_receipts` resource.
`support_kb` (the final step) stamps every chunk with provenance (`source_asset_key`, `chunk_id`,
`source_code_version`, ...) when it builds the index. `support_answers` reads that
provenance back off whatever chunks it retrieves and records one receipt per query —
no extra code, just the `receipts:` block on each component.

Free/local throughout: HuggingFace zero-shot classification, sentence-transformers
embeddings, a local ChromaDB index, a local SQLite receipt store. No API key
required for the core loop.

## What the script does

1. **Build 1** — `support_kb_articles` → `support_kb_chunks` → `support_kb_classified` →
   `support_kb_embedded` → `support_kb` with `code_version: recipe-1`. Six short support-KB
   articles (billing / shipping / other) get chunked, classified, embedded, and indexed —
   each step its own asset node (`per_step_ops: true`). Nothing has been learned yet.
2. **Serve the same question six times** — "Why was I charged twice for my
   subscription?" retrieves the same two chunks every time (identical query, same
   static index → identical ranking). Each materialization of `support_answers`
   records its own receipt via the `receipts:` block — no code for this part.
3. **Record real negative feedback** — recording an *outcome* ("did the answer help?")
   is deliberately not a Dagster asset: it's an external system's job (a support UI's
   thumbs-down button, in real life), so the script reads each new receipt id back
   from the store and calls `ContextReceiptStore.record_outcome(rid,
   signal="thumbs_down")` directly, the same way your own backend would.
4. **Build 2** — same `defs.yaml`, same `code_version: recipe-1` (nothing about the
   recipe changed). `support_kb`'s `receipts:` block reads the accumulated feedback,
   and `quarantine: {mode: exclude}` removes the two chunks that kept getting rated
   badly from the index.
5. **Ask the same question again** — served from different chunks, because the old
   ones are gone. The loop is closed.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_context_engineering_demo.sh \
  -o setup_context_engineering_demo.sh
bash setup_context_engineering_demo.sh
```

Takes a few minutes — `sentence-transformers` and the HuggingFace zero-shot model
download once and are cached for the rest of the run.

Browse the graph:

```bash
cd context-engineering-demo
uv run dg dev
# http://localhost:3000 -- support_kb_articles → support_kb_chunks →
# support_kb_classified → support_kb_embedded → support_kb: one real asset
# node per step (per_step_ops: true), each with its own materialization
# history and metadata.
```

Click `support_kb`'s (the final node) second materialization to see the real `receipts_feedback`
metadata (`quarantined_ids`, `scoped_to_recipe`, ...) Dagster recorded for that run.

## Try it yourself after the script finishes

```bash
uv run dg launch --assets support_answers   # ask again
uv run dg launch --assets support_kb        # rebuild, reading any new feedback
```

Add a real generated answer on top of retrieval:

```bash
export OPENAI_API_KEY=sk-...
bash setup_context_engineering_demo.sh   # re-run -- the 'generate' step is gated on the key
```

## Why this is two components, not one

- **`context_engineering_pipeline`** owns the build: chunking, classification,
  embedding, writing to the vector store, and reading receipts back to decide what
  stays in the index. One YAML, opt-in steps.
- **`rag_pipeline`** owns serving: retrieval (+ optional hybrid search, reranking,
  HyDE query expansion, generation), and recording what it served as a receipt.

Point `rag_pipeline`'s `retrievers:` at any index — not just one `context_engineering_pipeline`
built — and point several `context_engineering_pipeline` builds at the same
`context_receipts` resource to compare recipes (see the component's own README for
`receipts.shadow` — serving a fraction of live queries from a candidate recipe and
getting a statistically-grounded verdict before replacing the live one).

## Related

- [The Closed Context-Receipt Loop, Served Over Real MCP](context_engineering_mcp.md) — the
  same loop, but `rag_pipeline` is replaced with a standalone FastMCP gateway + agent, so
  serving runs entirely outside Dagster. Read that one once a real agent (not a Dagster run)
  needs to answer each query.
- [The Daily-Partitioned Knowledge Base](context_engineering_partitioned.md) — same build-side
  component, but partitioned daily instead of built all at once: each run reads only its own
  day's new rows. Read that one once your corpus arrives incrementally, not in one batch.
- [End-to-End RAG in Dagster](rag_complete.md) — the same RAG surface built from the
  **decomposed primitive** components (`document_chunker`, `embeddings_generator`,
  `vector_store_writer`, `vector_store_query`, `reranker`, `llm_prompt_executor`) one
  asset per step, instead of the two consolidated pipeline components here.
- [RAG as State, not as a Pipeline](rag_state.md) — versioned snapshots + retrieval-
  quality regression gating, a different axis from the receipt loop this demo covers.
