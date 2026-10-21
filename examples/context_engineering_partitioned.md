# The Daily-Partitioned Knowledge Base

[The Closed Context-Receipt Loop](context_engineering.md) and its [MCP-served sibling](context_engineering_mcp.md)
both build the whole knowledge base in one run. A real corpus doesn't arrive all at once, though --
new support tickets, new docs, new transcripts show up a slice at a time. This demo shows
`context_engineering_pipeline` handling that for real: `partition_type: daily`, one build per day,
each run reading only that day's new rows -- never reprocessing, never wiping out, a prior day's
chunks.

## Architecture

```
support_kb_chunks ──▶ support_kb_classified ──▶ support_kb_embedded ──▶ support_kb
(ContextEngineeringPipelineComponent, per_step_ops: true -- one real asset per step,
 partition_type: daily, source: {kind: warehouse_query} against a local DuckDB table
 filtered by {partition_key})
                                                                            │
                                                                            ▼
                                                              (chromadb index, growing
                                                               one day's chunks at a time)
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
local ChromaDB index, a local DuckDB source table. No API key required.

## What the script does

1. **Seed** a DuckDB table with three tickets, one dated each of three consecutive days.
2. **Build partition `2026-01-01`** -- `dg launch --assets group:knowledge_base --partition 2026-01-01`.
   Only that day's ticket is chunked, classified, embedded, and indexed.
3. **Build partition `2026-01-02`** -- same command, next day's key. The index now holds both days'
   chunks; day 1's chunk is untouched.
4. **Build partition `2026-01-03`** -- same again. All three days' chunks are now in the index.
5. **Search** for the day-3 ticket's topic right after its own build -- confirms it's searchable
   immediately, without re-running anything for days 1 or 2.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_context_engineering_partitioned_demo.sh | bash
cd context-engineering-partitioned-demo
uv run dg dev
```

Browse the graph at `http://localhost:3000`: `support_kb_chunks → support_kb_classified →
support_kb_embedded → support_kb`, each one a real asset node (`per_step_ops: true`), each
partitioned daily. Pick a partition in the UI's partition selector and look at its own
materialization history independent of the others.

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

- [The Closed Context-Receipt Loop](context_engineering.md) -- the same `per_step_ops: true` build
  side, but builds the whole corpus in one run and closes the loop with `rag_pipeline` + real
  negative feedback. Read that one for the receipts/quarantine story; read this one for partitioning.
- [Served Over Real MCP](context_engineering_mcp.md) -- same receipt loop, served entirely outside
  Dagster by a FastMCP gateway + agent.
