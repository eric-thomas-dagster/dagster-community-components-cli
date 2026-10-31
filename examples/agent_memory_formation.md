# Agent Memory Formation (Mem0) — distinct from RAG, distinct from chat history
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Components:** `synthetic_data_generator`, `formula`, `agent_memory_store` (new) — all existing or newly-added, composed via YAML.

**Script:** [`setup_agent_memory_demo.sh`](./setup_agent_memory_demo.sh)
**Cost:** $0 (`demo_mode: true`, no API keys)
**Validated:** 2026-10-31 — RUN_SUCCESS end-to-end; 6 customer memories formed, `memory_search_smoke_check` asset check passed.

## Why this exists

"Agent memory" (Mem0 being the most-cited dedicated implementation) is a genuinely different thing from RAG: an LLM extracts durable facts from a conversation and consolidates them for later semantic recall, rather than retrieving from a fixed corpus. It's also different from this registry's existing `conversation_memory` component, which is just a sliding-window chat-history JSON file — no extraction, no consolidation, no semantic search.

```
support_chat_turns_raw (synth — no role column)
       │
       └── support_chat_turns (formula adds a constant role: "user" column)
                │
                └── customer_agent_memory — AgentMemoryStoreComponent
                                              groups by customer_id, calls
                                              Mem0's add() once per group,
                                              then a search() smoke check
```

## A real `dg` YAML-loading quirk

`pandas`'s `numexpr` eval engine can't parse a plain quoted string literal like `'user'` when the expression is loaded through a `dg` project's `defs.yaml` — reproduced this via direct Python construction of `FormulaComponent` (works fine, `Applied expression for 'role': 'user'`) vs. the identical expression through YAML (fails: `name 'user' is not defined`, as if the quote characters were stripped before reaching eval). Not a `FormulaComponent` bug — something in the YAML string-resolution path. The workaround: `chr(117)+chr(115)+chr(101)+chr(114)` avoids quote characters entirely and falls through to the component's Python-eval fallback path, which handles it correctly.

## Scope (deliberately limited)

`AgentMemoryStoreComponent` wraps Mem0's `add()` (write) and an optional `search()` smoke check only — `get_all()`/`update()`/`delete()` aren't implemented, since their exact signatures for Mem0's self-hosted `Memory` class weren't confirmed against primary docs (only the cloud REST API's shapes are documented for those). This demo only exercises the confirmed surface.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Nothing else — `demo_mode: true` means no Mem0/LLM/embedder calls at all.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agent_memory_demo.sh -o setup_agent_memory_demo.sh
chmod +x setup_agent_memory_demo.sh
./setup_agent_memory_demo.sh
```

## Validated run (2026-10-31)

```
customer_agent_memory — ASSET_MATERIALIZATION - Materialized value customer_agent_memory.
customer_agent_memory — ASSET_CHECK_EVALUATION - Asset check 'memory_search_smoke_check' on 'customer_agent_memory' passed.
RUN_SUCCESS - Finished execution of run for "__ASSET_JOB".
```

Sample formed memory (`customer_agent_memory_agent_memory_demo.json`, written in the project root):

```json
{
  "id": "mem_0",
  "memory": "Charge my card ending in 9630.",
  "user_id": "CUST3791",
  "agent_id": null,
  "run_id": null,
  "metadata": {},
  "categories": [],
  "created_at": "demo",
  "score": 1.0
}
```

## Production variants

- Set `demo_mode: false`. If `vector_store`/`llm`/`embedder` are all left unset, Mem0's own self-hosted default applies: on-disk Qdrant + OpenAI (`gpt-5-mini` extraction, `text-embedding-3-small` embeddings) — requires `OPENAI_API_KEY`.
- Set `run_id_column` to scope memories per-conversation rather than accumulating across a customer's entire history.
- Set `infer: false` to store raw messages verbatim with no extraction LLM call (cheaper, no consolidation — useful for a pure audit log of what was said, distinct from what should be remembered).
- Swap `synthetic_data_generator` for real chat/agent logs — any `litellm_agent` / `langgraph_agent` run table with `role`/`content`/`user_id` columns works directly.

## See also

- [`agent_memory_store` README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/agent_memory_store/README.md)
- [Vector / RAG (mega-demo)](vector_rag.md) — uses `conversation_memory` for the simpler sliding-window pattern this component is distinct from.
- Browse the [walkthrough index](README.md) for related demos across every component family.
