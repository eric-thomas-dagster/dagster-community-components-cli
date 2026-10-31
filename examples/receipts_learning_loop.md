# Receipts Closed Loop — a RAG pipeline that learns from real feedback
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Components:** `synthetic_data_generator`, `formula`, `embeddings_generator`, `vector_store_writer`, `context_receipt_store`, `rag_pipeline` (with its `receipts:` block), `receipt_outcome_ingestion` — all existing, composed via YAML.

**Script:** [`setup_receipts_learning_loop_demo.sh`](./setup_receipts_learning_loop_demo.sh)
**Cost:** $0 — everything local (ChromaDB + sentence-transformers + SQLite), no LLM call, no cloud account.
**Validated:** 2026-10-31 — RUN_SUCCESS on both retrieval rounds and the outcome-ingestion run; 10 real receipts written, 10 real outcome events ingested, 10 real per-chunk priors computed and read back by a second retrieval round.

## Why this exists

This is the "receipt store as a platform moat" idea made concrete: Dagster owning not just the RAG pipeline's execution, but the **audit trail** of what it retrieved (`rag_pipeline`'s `receipts:` block, backed by `resources/context_receipt_store`) and the **learning signal** from what actually worked (`receipt_outcome_ingestion` + `store.refresh_item_scores()`). No other demo in this catalog exercises the receipts loop end-to-end.

```
support_tickets_corpus (synth, 10 rows)
       │
       └── support_tickets_with_chunk_ids (formula: chunk_id = ticket_id)
                │
                ├── ticket_embeddings (sentence-transformers, local)
                │        │
                │        └── support_kb_index (vector_store_writer, chromadb)
                │
                └── support_answers (rag_pipeline: retrieve + receipts,
                      querying with each ticket's own text — self-retrieval)
                             │
         [outside Dagster] simulate thumbs up/down on this round's receipts
                             │
                      receipt_outcome_ingestion (real outcome events)
                             │
         [outside Dagster] store.refresh_item_scores() — real learned priors
                             │
                      support_answers (round 2 — rerank reads the priors)
```

## Two real gotchas this demo found

1. **`rag_pipeline`'s rerank needs a `chunk_id` metadata key on the indexed documents, literally named that.** `_doc_chunk_id()` looks for `metadata["chunk_id"]` specifically — without it, every retrieved doc gets a synthetic `__noid_N` id that's silently excluded from receipt item tracking. `vector_store_writer` doesn't stamp this on its own, so this demo inserts a `formula` step (`chunk_id: ticket_id`) before embedding/writing, and passes `metadata_columns: [chunk_id]`.
2. **`get_item_scores()` only *reads* the `context_item_scores` table — nothing computes it automatically.** `context_engineering_pipeline`'s own `receipts:` block calls `store.refresh_item_scores()` internally during its build step; since this demo uses the lighter `vector_store_writer`/`rag_pipeline` combo instead (not that heavier chunking component), it calls `refresh_item_scores()` directly in Python between the outcome-ingestion step and the second retrieval round.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `sqlite3` — preinstalled on macOS and most Linux distros.
- Nothing else — `sentence-transformers` downloads a small (~90MB) model on first run.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_receipts_learning_loop_demo.sh -o setup_receipts_learning_loop_demo.sh
chmod +x setup_receipts_learning_loop_demo.sh
./setup_receipts_learning_loop_demo.sh
```

## Validated run (2026-10-31)

```
Round 1: RUN_SUCCESS — 10 receipts written to receipts.sqlite3
Ingesting feedback: RUN_SUCCESS — 10 outcome events (7 thumbs_up, 3 thumbs_down)
Recomputing learned per-chunk priors from receipts + outcomes:
  T00001: retrievals=4, learned prior=0.143
  T00002: retrievals=3, learned prior=0.077
  T00003: retrievals=1, learned prior=-0.091
  ... (10 items total, real values computed by store.refresh_item_scores())
Round 2: RUN_SUCCESS — rerank active, reading the priors above
```

## Be honest about what this does and doesn't show

On this tiny 10-document, self-retrieval corpus, each query's #1 result is always its own exact text (cosine ≈ 1.0) — no learned prior at a sane `prior_weight` can displace a perfect match. **This demo proves the loop's mechanism is real end-to-end** (a receipt gets written, real feedback gets ingested against it, a real per-chunk prior gets computed from that feedback, and retrieval genuinely reads it back on the next round) — it is not staged to produce a dramatic ranking flip. Point this at a real corpus with genuinely competing documents (ambiguous queries where several chunks are plausible answers) to see reranking actually change what gets served.

For a fuller, more realistic version of this same loop — real chunking/dedup, chunk quarantine on sustained bad feedback, rewrite-candidate reporting, and a shadow-deployment A/B comparison — see `quickstarts/receipts_learning_loop` in [dagster-component-templates](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/quickstarts/receipts_learning_loop), which uses the heavier `context_engineering_pipeline` component for the build side instead of this demo's simpler `vector_store_writer`.

## Production variants

- Use a real embedding model (`provider: openai`, needs `OPENAI_API_KEY`) on both `embeddings_generator` and `rag_pipeline`'s `retriever.embedding`.
- Ship receipts to a warehouse with `context_receipt_bigquery_sync` — **not used in this demo**, since it requires a real `dagster_gcp.BigQueryResource` with no local/demo-mode alternative. The receipt store's native SQLite backing (used throughout here) needs no warehouse at all.
- Schedule `store.refresh_item_scores()` on a cron (e.g. nightly) rather than calling it ad hoc, so priors stay current as outcomes arrive.
- Swap in `context_engineering_pipeline` for the build side (see `quickstarts/receipts_learning_loop`) to get this same refresh running automatically, plus chunking, dedup, and quarantine of chunks that keep getting rated down.

## See also

- [`rag_pipeline` README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/rag_pipeline/README.md)
- [`context_receipt_store` SPEC](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/resources/context_receipt_store/README.md)
- [`receipt_outcome_ingestion` README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/receipt_outcome_ingestion/README.md)
- [Vector / RAG (mega-demo)](vector_rag.md) — the plain RAG stack this demo adds a learning loop on top of.
- Browse the [walkthrough index](README.md) for related demos across every component family.
