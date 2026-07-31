# Dagster Orchestrates + Prefect Executes — Each Doing What It's Best At

Not competitive — **complementary**. Dagster owns the graph, the partition (per-file / per-tenant / per-day), the asset catalog, the state tracking. Prefect owns the per-run work — including **durable execution** and **runtime-decided task graphs** (the exact tasks depend on what's inside each input).

For an SE audience that's evaluating Prefect vs Dagster: this walkthrough gives them "here's how you'd use BOTH, playing each to its strengths" instead of forcing a false choice.

## The story

An inbox of documents lands per hour. Each document could be a PDF, an HTML table, an email, a scanned image — you don't know until you look at it. The task graph for each document depends on what's inside it.

- **Dagster** partitions by file, kicks off one materialization per file, records the outcome in its asset catalog with per-file lineage.
- **Prefect** runs the actual document parse — a flow that inspects the file type at runtime and calls different tasks (`parse_pdf`, `parse_html`, `parse_email`) based on what it finds. Runtime-decided DAG; that's Prefect's sweet spot.

## The asset graph

```
inbox/                                     Prefect server (:4200) ──────────────────────────┐
├─ report.pdf.txt          ┌──►                                                              │
├─ prices.html             │       ┌───────┐    parse_document flow  (runtime task graph)   │
└─ support.eml             │       │       │       │                                          │
                            │       │       │       ├── parse_pdf(file)   ← if pdf/text       │
                            ▼       │       ▼       ├── parse_html(file)  ← if html            │
                    ┌─────────────┐ │     ┌───────┐ └── parse_email(file) ← if eml             │
                    │ parsed_documents ── ┘     │              │                                │
                    │ (Dagster asset,           │              ▼                                │
                    │  3 partitions,            │        write_output → parsed_output/*.json    │
                    │  triggers Prefect per file)                                              │
                    │                                                                          │
                    │  PrefectFlowRunAssetComponent                                            │
                    │  api_url = http://127.0.0.1:4200/api                                     │
                    │  parameters.file_path = "{partition_key}"                                │
                    └──────────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
              parsed_output/  ← per-file JSON with the runtime-decided
                                task graph's result. Downstream Dagster
                                assets read from here.
```

Each Dagster partition materialization records the Prefect `flow_run_id` + `state` in its metadata. Six weeks later "why did prices.html not extract table rows correctly?" is a click in Dagster (find the partition) → click through to Prefect's UI (see the runtime task graph for that specific run).

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dagster_orchestrates_prefect_demo.sh \
  -o setup_dagster_orchestrates_prefect_demo.sh
bash setup_dagster_orchestrates_prefect_demo.sh
```

Requirements: `uv`. **No API keys.** ~2 min first run (installs Prefect + starts a local Prefect server).

The setup script:
1. Starts a **local Prefect server** in background at `:4200` (no cloud account needed)
2. Deploys a `parse_document` Prefect flow via `.serve()` (background worker)
3. Scaffolds a Dagster project with 3 static partitions (one per demo file)
4. Materializes each partition — each triggers the Prefect flow, waits for completion, records the flow_run_id + state

## Watch both UIs side by side

- **Prefect UI**: http://127.0.0.1:4200 — Flow Runs tab shows the 3 flow runs. Click into one → see the **runtime task graph** for that specific file. PDF got `parse_pdf → write_output`. HTML got `parse_html → write_output`. Email got `parse_email → write_output`. The DAG differs per run — that's the Prefect story.
- **Dagster UI**: `cd $PROJECT_DIR && uv run dg dev` at http://localhost:3000 — Assets tab shows `parsed_documents` with 3 partitions (`report.pdf.txt`, `prices.html`, `support.eml`), each green. Click any partition → materialization metadata shows the Prefect `flow_run_id`, `state_name`, `state_type`, `parameters`, and `prefect_url`. Click the link → jump to the exact Prefect flow run.

## The three Prefect components used

- [`PrefectResourceComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/prefect_resource) — configures the Prefect Python SDK to point at a specific Prefect instance (local or cloud).
- [`PrefectFlowRunAssetComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/prefect_flow_run) — the one that does the actual triggering. This is the workhorse for "Dagster owns the orchestration, Prefect owns the run" patterns.
- [`PrefectFlowRunSensorComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/prefect_flow_run_sensor) — the other direction: Prefect owns some upstream work, Dagster reacts to completions. Not used in this demo but ready when Prefect kicks off before Dagster in your graph.

All three mirror the shape of the existing Temporal integration components (`temporal_workflow_trigger` / `temporal_workflow_sensor`), so if someone knows one they know both.

## Switch to Prefect Cloud

One YAML change in the trigger component:

```yaml
type: dagster_community_components.PrefectFlowRunAssetComponent
attributes:
  # ...
  api_url: https://api.prefect.cloud/api/accounts/<acct-id>/workspaces/<ws-id>
  api_key_env_var: PREFECT_API_KEY
```

Set `PREFECT_API_KEY` in your shell before `dg dev`. Everything else stays the same — the flow deployment lives on Prefect Cloud, Dagster's `parsed_documents` asset triggers it just the same.

## When to reach for this pattern

- **You already have a Prefect codebase** and want Dagster's asset catalog + lineage + scheduling around it. This bridge lets you keep Prefect's runtime task graph and layer Dagster on top.
- **The per-run work has a runtime-decided task graph** — content-type-driven document processing, per-record dynamic transforms, ML pipelines where the exact steps depend on the input. Prefect handles that shape cleanly; Dagster handles the outer orchestration.
- **You want durable execution semantics per-run** — Prefect's flow state is persisted to its DB; if a task fails mid-flow, Prefect knows where to resume. Dagster's per-partition retry gives you the outer layer.

## When NOT to reach for this pattern

- **The per-run work is a static DAG that doesn't need Prefect's runtime branching** — do it in Dagster directly. Simpler ops, one system.
- **You're greenfielding and don't already have Prefect flows** — Dagster's own primitives (partitions, dynamic outputs, asset checks) cover most of what Prefect brings for orchestration cases. The bridge shines when Prefect is already in the picture OR the per-run work genuinely needs runtime-decided DAGs.

## Files worth reading in the scaffolded project

- `prefect_worker/flow.py` — the Prefect flow. Note how the DAG isn't declared ahead of time; the flow decides at runtime which parse task to call.
- `src/dagster_orchestrates_prefect_demo/defs/parsed_documents/defs.yaml` — the `PrefectFlowRunAssetComponent` YAML. All the demo's Dagster→Prefect bridge is here.
- `src/dagster_orchestrates_prefect_demo/defs/prefect/defs.yaml` — the shared `PrefectResourceComponent`.
