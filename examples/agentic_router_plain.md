# Agentic Router — Plain Dagster (No Components, No YAML)

The exact same asset graph shape as [agentic_router.md](agentic_router.md), but written in **raw Dagster Python** — no components, no YAML, no framework layer. For SE conversations where the question is "what does this look like without your framework?".

## The two demos, side by side

| | Components (`agentic_router.md`) | Plain Dagster (this walkthrough) |
|---|---|---|
| **Total code** | ~100 lines of YAML across 8 defs.yaml files | ~250 lines of Python in `defs/assets.py` |
| **The router** | 1 line `type: dagster_community_components.LlmMultiPathRouterComponent` + config | ~130 lines: `@graph_asset` + 5 `@op` + `@dg.op(build_task)` + `@dg.op(classify_and_register)` |
| **Human gate** | 1 line `type: dagster_community_components.HumanApprovalGateComponent` + config | ~30 lines: `@dg.asset` that reads a JSON token + emits `AssetCheckResult` |
| **Sensor** | 1 line `type: dagster_community_components.FilesystemMonitorSensorComponent` + config | ~15 lines: `@dg.sensor` polling a directory |
| **Sinks** | `type: dagster_community_components.DataframeToCsvComponent` per sink | Bespoke `@dg.asset` per sink |
| **Add a new agent** | New YAML file, no code | New Python function + register in `Definitions` |

Both projects produce the **same asset graph**, use the **same DuckDB baggage database**, and expose the **same sensor + gate + branch behavior**. If you run both projects side-by-side (different ports) and open the Dagster UI on each, the asset graphs are indistinguishable.

The difference is surface area, not capability.

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_router_plain_demo.sh \
  -o setup_agentic_router_plain_demo.sh
bash setup_agentic_router_plain_demo.sh
```

Requirements: `uv`, `OPENAI_API_KEY`. ~1 min first run.

## Project layout

```
agentic-router-plain-demo/
├── data/
│   ├── baggage.duckdb           ← real database, seeded by the script
│   └── baggage_reports.csv
├── approvals/                   ← human token drop directory (empty)
├── courier_bookings/            ← delivery_request → CSV per case
├── compensations/               ← voucher_issued → CSV per case
├── audit/
│   └── case_audit.duckdb        ← "legacy warehouse" audit table
└── src/agentic_router_plain_demo/
    ├── definitions.py           ← 12-line autoloader + resource glue
    └── defs/
        └── assets.py            ← EVERYTHING — 250 lines
```

`definitions.py`:

```python
from pathlib import Path
from dagster import Definitions, definitions, load_from_defs_folder
from dagster_duckdb import DuckDBResource
from .defs import assets as A

@definitions
def defs():
    return Definitions.merge(
        load_from_defs_folder(path_within_project=Path(__file__).parent),
        Definitions(resources={"baggage_db": DuckDBResource(database=A.BAGGAGE_DB)}),
    )
```

That's it — the autoloader picks up everything in `defs/`.

## What `defs/assets.py` contains

- **Partitioning setup** — `StaticPartitionsDefinition(["c1","c2","c3"])` for the router + `DynamicPartitionsDefinition("<branch>_cases")` for each branch.
- **Tool dispatcher** — `_query_baggage_db()` for real SQL; `TOOL_ROLEPLAY_PROMPTS` dict for the other 5 stub tools; `_call_tool()` picks the right one.
- **5 `@dg.op` step_ops** — one per ReAct iteration, all sharing the same body via a factory. Each takes `task_str` + `prior_step`, calls the planner LLM, dispatches the tool, returns a trajectory-updated dict. Short-circuits when `prior_step.done`.
- **`build_task` op** — reads the CSV, filters to the current partition's row, formats a task template.
- **`classify_and_register` op** — reads the trajectory, asks the classifier LLM which branches apply + fills in per-branch structured payloads, registers case_id on each picked branch's `DynamicPartitionsDefinition`.
- **`baggage_triage_agent` `@dg.graph_asset`** — wires build_task → 5 step_ops → classify_and_register into a graph. Each op is visible in the run UI.
- **3 branch `@dg.asset`** — each with its own `DynamicPartitionsDefinition`, loads the router's output for its `case_id` via `AllPartitionMapping`, returns the extracted payload as a single-row DataFrame.
- **`courier_booked` / `compensation_paid` `@dg.asset`** — dataframe → CSV sinks with `AutomationCondition.eager()`.
- **`human_review` `@dg.asset`** — reads `approvals/<case_id>.json`, emits Output + AssetCheckResult (WARN for pending, ERROR for rejected).
- **`escalation_audited` `@dg.asset`** — writes to the audit DuckDB with an append pattern.
- **`approve_and_process_job`** — `define_asset_job` over `[human_review, escalation_audited]`.
- **`approval_watcher` `@dg.sensor`** — polls the approvals directory, on new tokens yields `RunRequest(partition_key=<file stem>)`.

## Runtime behavior — identical to the components version

- c1 → agent runs → picks `delivery_request` → registers c1 on `delivery_request_cases` → delivery_request materializes → courier_booked CSV written.
- c2 → agent picks `voucher_issued` → registers c2 on `voucher_issued_cases` → compensation_paid CSV.
- Escalation cases → register on `escalation_cases` → human_review pends (WARN check) until a token drops → sensor sees the token, fires the job, cascade completes → audit trail.

## Why the components version is 4× shorter

Look at [`llm_multi_path_router` component source](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/ai/llm_multi_path_router/component.py) — it's ~1000 lines. All that logic lives in the component so YAML consumers don't have to. The user hands the component a `tools:` list + `outputs:` list, and the component handles:

- Building the graph_asset dynamically from `max_iterations` step ops
- Dispatching tool_type (sql / http / llm_roleplay)
- Constructing per-branch `DynamicPartitionsDefinition`
- Cross-partition mapping (static router → dynamic branches)
- Classifier prompt engineering + JSON schema
- Registering case_ids at emit time
- Branch asset factory (per-schema payload extraction)

In the plain-Dagster version, **you write all of that yourself, per project**. Adding a second agent (say, a fraud-triage router with different tools + different branches) means duplicating most of `assets.py`. In the components version, it's another 40 lines of YAML.

## When to reach for plain Dagster vs components

- **Plain Dagster**: you have exactly one router and don't need to reuse the pattern. Or you need custom behavior the component doesn't expose. Or you're evaluating the framework and want to see the raw shape first.
- **Components**: you need multiple router instances (per team, per case type, per environment). Or you're building a platform where analysts/domain experts author routers in YAML. Or you want the component-registry catalog of ready-made tools + shape variants.

Both produce the same runtime behavior, the same asset graph, the same UI experience. The choice is really about **who writes the router** — engineers (plain Dagster) or analysts + engineers via YAML (components).

## Compare in the UI

Stand up both:
```bash
bash setup_agentic_router_demo.sh agentic-router-yaml
bash setup_agentic_router_plain_demo.sh agentic-router-plain
```

Run each on a different port:
```bash
(cd agentic-router-yaml && uv run dg dev --port 3000) &
(cd agentic-router-plain && uv run dg dev --port 3100) &
```

Open both, click into `baggage_triage_agent` on each → the graphs are indistinguishable.
