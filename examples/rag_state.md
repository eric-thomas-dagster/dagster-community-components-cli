# RAG as State, not as a Pipeline
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

The three components in this walkthrough exist to turn a RAG stack from "a pipeline that occasionally writes tables" into "a set of stateful entities that evolve over time, each with a materialization history, a freshness policy, a quality check, and a rollback path."

Dagster's asset primitive is what makes that shape expressible: every artifact — the corpus, the vector index, the retrieval-quality score, the prompt — becomes a first-class thing with lineage, versions, and checks.

## Two ways to wire RAG in Dagster

Pick the one that matches your intent:

| Shape | Component(s) | When to reach for it |
|---|---|---|
| **Simple RAG** — one asset that does embed + retrieve + generate | [`RAGPipelineComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/rag_pipeline) | One YAML → answer. Fast on-ramp for a proof of concept. |
| **Stateful RAG** — corpus / snapshot / eval as separate assets | `document_corpus` + `vector_index_snapshot` + `rag_eval` | Production RAG: corpus provenance, immutable index snapshots, retrieval-quality gates, backfillable eval, partition-selectable rollback. Every stateful entity is a first-class asset. |

If you're prototyping, start with `RAGPipelineComponent`. If you're taking RAG to prod — where "which docs answered this question last Tuesday?" and "why did retrieval quality drop overnight?" become real questions — the three components below are the shape.

### Simple RAG — asset graph

One asset with everything inside. Suits demos and prototypes; no state to browse between runs.

```
                             ┌──────────────────────────┐
   user query  ──────────▶   │   rag_answer            │  ──────▶  answer + sources
   (config)                  │   (RAGPipelineComponent) │
                             │   • embed query          │
                             │   • search vector store  │
                             │   • LLM generation       │
                             └──────────────────────────┘
```

### Stateful RAG — asset graph

Every artifact is a named, versioned asset. `snapshot_id` is a partition key, so each downstream run picks a specific snapshot — that's the rollback edge.

```
    ┌───────────────────┐
    │ docs_corpus       │  metadata: doc_count, total_bytes, corpus_hash, ingested_at
    │ (DocumentCorpus)  │  asset_check: min_doc_count
    └────────┬──────────┘
             │  every materialization → new corpus_hash
             ▼
    ┌───────────────────────────────┐
    │ docs_index                    │  metadata: snapshot_id, snapshot_path, corpus_hash,
    │ (VectorIndexSnapshot)         │            chunk_count, dimension, embedder
    │                               │  side-effect: registers dynamic partition
    └────────┬──────────────────────┘            `rag_snapshot=<snapshot_id>`
             │  every materialization → new
             │  immutable snapshot dir on disk
             ▼                                    (partitions: <snap_v1>, <snap_v2>, ...)
    ┌───────────────────────────────┐
    │ docs_eval                     │  partitioned by rag_snapshot
    │ (RagEval)                     │  metadata: precision_at_k, n_queries, k
    │                               │  ┌───────────────────────────────────────────────┐
    │                               │  │ asset_check: retrieval_quality_check          │
    │                               │──│   FAILS if score < min OR regresses vs prior  │
    └───────────────────────────────┘  │   → downstream runs blocked                   │
                                       └───────────────────────────────────────────────┘

    Rollback:   dg launch --assets <downstream> --partition <older_snapshot_id>
    Backfill:   dagster asset backfill --assets docs_eval --partition-range v1...vN
```

## Components used

| Component | Role | Status tracked |
|---|---|---|
| [`document_corpus`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_corpus) | Corpus AS state — each materialization = one immutable snapshot of the source docs | `doc_count`, `total_bytes`, `corpus_hash`, `ingested_at` |
| [`vector_index_snapshot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_index_snapshot) | Chunk + embed → new ChromaDB snapshot dir per materialization. Registers a new dynamic partition. | `snapshot_id`, `snapshot_path`, `corpus_hash`, `chunk_count`, `dimension`, `embedder` |
| [`rag_eval`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/rag_eval) | Golden-set retrieval eval, partitioned by `snapshot_id`. Asset check FAILS on regression vs the prior materialization. | `precision_at_k`, `n_queries`, `k` |

## Run it locally

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_rag_state_demo.sh \
  -o setup_rag_state_demo.sh
bash setup_rag_state_demo.sh
```

Requirements: `uv`. No API keys — ChromaDB uses its bundled ONNX MiniLM embedder (~90 MB one-time download). Cost: $0. Time: ~3 min first run.

**What the script does end-to-end:**

1. Scaffolds a fresh Dagster project (`rag-state-demo/`).
2. Writes 5 markdown files about Dagster concepts.
3. Wires 3 defs.yaml files — `docs_corpus`, `docs_index`, `docs_eval`.
4. Runs `dg check defs`.
5. **Round 1** — materializes corpus → snapshot v1 → eval[v1]. The eval passes.
6. **Injects a regression** — strips key terms (`max_retries`, `backoff`) out of the retry-policy doc.
7. **Round 2** — re-materializes corpus (new `corpus_hash`) → snapshot v2 → eval[v2]. The retrieval quality drops on the retry-policy query. The **asset check on `docs_eval` fails** — Dagster records the regression as data, not as a build error.
8. Prints the rollback path: snapshot v1's ChromaDB directory is still on disk, its `docs_eval` materialization is still in history, and any downstream RAG-answer asset can be materialized against the v1 partition to serve queries from the old (good) index.

## What you see in `dg dev`

- **Assets tab** — 3 assets in the `rag_state` group. Lineage: `docs_corpus → docs_index → docs_eval`.
- **`docs_eval` → Partitions tab** — one row per snapshot_id. Both v1 (green) and v2 (red — check failed) present.
- **`docs_eval` → Materialization history** — `precision_at_k` plotted over time. The v2 drop is obvious.
- **`docs_eval` → Asset checks tab** — `docs_eval_retrieval_quality_check` — passed for v1, failed for v2, with the delta and threshold surfaced.

## The rollback move, spelled out

Rollback isn't restoring a file. It's *materializing your downstream against a past snapshot partition*.

Suppose you have a downstream `rag_answer` asset (which this walkthrough doesn't scaffold — it's up to you and your LLM provider) that's partitioned by the same `rag_snapshot` dynamic partition. To roll back to yesterday's index:

```bash
# Materialize rag_answer against yesterday's snapshot partition
dg launch --assets rag_answer --partition <yesterday_snapshot_id>
```

That resolves upstream to yesterday's `vector_index_snapshot` materialization, reads yesterday's ChromaDB directory from disk, and answers. No rebuild.

Same story for **backfills**: `dagster asset backfill --assets rag_answer --partition-range v1...v42` re-runs the answer asset against every snapshot in the range. Useful for regression investigations ("which snapshot introduced the drop?") or for repointing at a known-good partition after a rollout.

## What you get end-to-end

Beyond running embed → retrieve → generate, the stateful shape gives you:

- **"Corpus at time T" as a first-class entity** distinct from "the run that materialized it," carrying a stable `corpus_hash` that every downstream artifact traces back to.
- **Past index snapshots addressable as partition keys** in the graph. Rollback is a partition selector, not a filesystem restore or code change.
- **Downstream gated on the quality check.** Bad snapshots don't advance to answer generation — the regression is caught as data, not surfaced later from a customer complaint.
- **Backfill quality across every historical snapshot in one command** — `dagster asset backfill --assets docs_eval --partition-range v1...v42`. No bespoke metrics store.

## Extending

- **Per-tenant corpora**: give each tenant its own `document_corpus + vector_index_snapshot` pair. Dynamic-partition the snapshot by `<tenant_id>::<snapshot_id>` for tenant-scoped rollback.
- **Multiple embedder A/B**: two `vector_index_snapshot` assets side-by-side over the same corpus, different `embedder_provider`. `rag_eval` on each — compare precision@k across both partitions.
- **Automation**: attach `AutomationCondition.eager()` to `docs_index` so a new corpus materialization automatically triggers a new snapshot + eval. If the eval fails, downstream `rag_answer` remains bound to the last-good partition.
- **Freshness alerts**: set `freshness_max_lag_minutes` on `docs_corpus` so an operator gets paged if the corpus goes stale.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_rag_state_demo.sh \
  -o setup_rag_state_demo.sh
bash setup_rag_state_demo.sh
```

## See also

<!-- TODO: link related walkthroughs -->
