# The Daily-Partitioned Knowledge Base

[The Closed Context-Receipt Loop](context_engineering.md) and its [MCP-served sibling](context_engineering_mcp.md)
both build the whole knowledge base in one run. A real corpus doesn't arrive all at once, though --
new support tickets, new docs, new transcripts show up a slice at a time. This demo runs the exact
same closed loop -- build, serve, real feedback, rebuild with the identical recipe, the chunks that
kept failing are gone -- but the build side is `partition_type: daily` instead of one all-or-nothing
run, and a rebuild only ever re-processes the ONE partition whose chunk picked up bad feedback.

## Architecture

```
support_kb_chunks ──▶ support_kb_classified ──▶ support_kb_embedded ──▶ support_kb ──▶ (chromadb index)
(ContextEngineeringPipelineComponent, per_step_ops: true -- one real asset per step,
 partition_type: daily, source: {kind: warehouse_query} against a local DuckDB table
 filtered by {partition_key}, receipts: {quarantine: {mode: exclude}})
                                                                            │
                                                                            ▼
                                                              context_receipts (ContextReceiptStoreComponent,
                                                                                 local SQLite)
                                                                            ▲
                                                                            │
support_questions ──▶ support_answers
(DataframeFromCsvComponent)     (RAGPipelineComponent: retrieve [+ generate, optional])
                                  receipts: {resource_key: context_receipts,
                                             context_name: support_kb}
```

Three builds, three different `partition_key`s, one shared index:

```
day 1 build (2026-01-01) ──▶ index: [billing_refund]
day 2 build (2026-01-02) ──▶ index: [billing_refund, shipping_delay]
day 3 build (2026-01-03) ──▶ index: [billing_refund, shipping_delay, export_broken]
```

Each day's run only ever sees its own row (`source.sql`'s `{partition_key}` placeholder is bound to
that run's partition and filters the DuckDB query), and `write_vector_store`'s default
`sync_mode: replace_sources` only deletes chunks belonging to source rows that were present in
**this run** -- a prior day's rows are never candidates for deletion because they never appear in
the current run's batch. `per_step_ops: true` gives the same 4-node look as the other two demos'
build side, just partitioned this time: `dg dev`'s UI shows a partition selector on `support_kb`
(and its 3 sibling step nodes), with each partition's own independent materialization history.

Free/local throughout: HuggingFace zero-shot classification, sentence-transformers embeddings, a
local ChromaDB index, a local DuckDB source table, a local SQLite receipt store. No API key
required for the core loop.

## The receipt loop is scoped per partition, not just per asset

Receipt feedback is looked up by **chunk id against the batch currently being built** -- so when
`support_kb`'s rebuild for partition `2026-01-01` runs, only that partition's own chunk ids are
candidates for quarantine. The `2026-01-02` and `2026-01-03` partitions were never served (this demo
only asks the question that matches day 1's ticket), so they carry no feedback and rebuilding only
`2026-01-01` is enough -- the other two partitions are correctly left untouched, with no need to
rebuild the whole table.

## What the script does

1. **Seed** a DuckDB table with three tickets, one dated each of three consecutive days, and one
   question that matches the day-1 ticket.
2. **Build partitions `2026-01-01` → `2026-01-03`** -- `dg launch --assets group:knowledge_base
   --partition <day>`, once per day. Each run reads and indexes only that day's new ticket; the index
   grows from 1 to 3 chunks across the three runs.
3. **Serve the question six times** via `support_answers` (`rag_pipeline`, `retrieve` against the
   shared index) -- each call **writes** a receipt through the `receipts:` block, no extra code.
4. **Record real negative feedback** -- each round also calls `ContextReceiptStore.record_outcome`
   directly (standing in for a real support UI's thumbs-down button) and rates that round's receipt
   `thumbs_down`.
5. **Rebuild ONLY partition `2026-01-01`** -- same `code_version`, only the accumulated feedback
   changed. `quarantine: {mode: exclude}` removes `billing_refund_0` (the chunk that kept getting
   rated down); `export_broken_0` / `shipping_delay_0`, from partitions that were never served, are
   confirmed untouched.
6. **Ask the same question again** -- served from a different chunk now.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_context_engineering_partitioned_demo.sh | bash
cd context-engineering-partitioned-demo
uv run dg dev
```

Browse the graph at `http://localhost:3000`:
- `knowledge_base` group: `support_kb_chunks → support_kb_classified → support_kb_embedded →
  support_kb`, each one a real asset node (`per_step_ops: true`), each partitioned daily. Pick a
  partition in the UI's partition selector and look at its own materialization history independent
  of the others. Click `support_kb`'s (the final node) second `2026-01-01` materialization to see the
  real `receipts_feedback` metadata (`quarantined_ids`, `scoped_to_recipe`, ...).
- `serving` group: `support_questions → support_answers`.

Re-run the loop yourself:

```bash
uv run dg launch --assets support_answers                                # ask again
uv run dg launch --assets group:knowledge_base --partition 2026-01-01    # rebuild just that day
```

Backfill further days yourself (after seeding more rows in `support_tickets.duckdb` for those dates):

```bash
uv run dg launch --assets group:knowledge_base --partition-range "2026-01-04...2026-01-10"
```

## Why `group:knowledge_base`, not `--assets support_kb`

`per_step_ops: true` makes `support_kb_chunks` / `support_kb_classified` / `support_kb_embedded` /
`support_kb` one non-subsettable multi-asset under the hood -- Dagster requires all of a
non-subsettable multi-asset's keys to be selected together. Selecting the group the component
already tags its output with (`group_name: knowledge_base`) selects all four at once; selecting
`support_kb` alone raises `DagsterInvalidSubsetError`.

## Related

- [The Closed Context-Receipt Loop](context_engineering.md) -- the same closed loop, same
  `per_step_ops: true` build side, but built all at once instead of partitioned daily. Read that one
  first if partitioning isn't the point yet; come here once the corpus arrives incrementally.
- [Served Over Real MCP](context_engineering_mcp.md) -- same receipt loop, served entirely outside
  Dagster by a FastMCP gateway + agent instead of `rag_pipeline`.
