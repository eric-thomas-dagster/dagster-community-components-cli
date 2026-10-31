# Qwen3 Self-Hosted (ECS) vs. Bedrock Converse — the inference-infra choice as a Dagster graph
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Components:** `synthetic_data_generator`, `ecs_pipes_asset` (new), `bedrock_converse_asset` (new) — all existing or newly-added, composed via YAML.

**Script:** [`setup_qwen3_self_hosted_vs_bedrock_demo.sh`](./setup_qwen3_self_hosted_vs_bedrock_demo.sh)
**Cost:** $0 (both components run `demo_mode: true`)
**Validated:** 2026-10-31 — RUN_SUCCESS end-to-end; real Pipes protocol (local subprocess) completed, Bedrock Converse branch returned schema-valid JSON.

## Why this exists

The question every team self-hosting an open-weight model eventually asks: *would a managed API be cheaper/simpler for this workload?* This puts both inference paths for the same model family (Qwen3) side by side as real Dagster assets, so the comparison lives in the asset graph — not a spreadsheet someone has to maintain separately.

```
support_tickets_sample (synth)
       │
       └── ticket_triage_bedrock          — BedrockConverseAssetComponent
                                             one Converse call per row, JSON-schema-validated output

invoice_extraction_self_hosted            — EcsPipesAssetComponent
                                             GPU ECS task via Dagster Pipes (demo_mode:
                                             real local subprocess against a fake
                                             ECS/CloudWatch boundary)
```

They process different sample workloads **on purpose** — the ECS task simulates documents already staged for a GPU container (a real ECS task doesn't take a Dagster-asset input the way `bedrock_converse_asset` does); Bedrock processes a normal upstream DataFrame. The point isn't a literal diff of outputs — it's that both inference *architectures* are real, comparable Dagster assets with their own cost/latency/quality metadata, in one graph.

## `ecs_pipes_asset`'s demo mode, specifically

`demo_mode: true` does **not** swap `PipesECSClient` for a different transport. It builds the exact same `PipesECSClient` / `PipesCloudWatchMessageReader` classes used in production, injecting fake boto3 `ecs`/`logs` clients instead of real ones. The fake `run_task` call launches `local_script_path` as a real local subprocess; the fake `logs` client serves its stdout as paginated CloudWatch log events. Only the actual AWS API calls are faked — the real Pipes protocol code runs unmodified in both modes.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Nothing else — both components run `demo_mode: true`, no AWS account needed.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_qwen3_self_hosted_vs_bedrock_demo.sh -o setup_qwen3_self_hosted_vs_bedrock_demo.sh
chmod +x setup_qwen3_self_hosted_vs_bedrock_demo.sh
./setup_qwen3_self_hosted_vs_bedrock_demo.sh
```

## Validated run (2026-10-31)

```
[pipes] external process successfully opened dagster pipes.
self-hosted Qwen3 extraction: processing 3 invoices
[pipes] ECS task arn:aws:ecs:demo:000000000000:task/my-gpu-cluster/01c99dc4... completed
Asset check 'schema_valid_rate' on 'invoice_extraction_self_hosted' passed.
...
ASSET_MATERIALIZATION - Materialized value ticket_triage_bedrock.
RUN_SUCCESS - Finished execution of run for "__ASSET_JOB".
```

`invoice_extraction_self_hosted`'s reported metadata: `schema_valid_rate: 1.0`, `docs_failed: 0`, `rows_processed: 3`, `latency_ms_per_doc` (simulated GPU inference time). `ticket_triage_bedrock` adds `category`/`urgency` columns per row, JSON-schema validated against the configured `output_schema`.

## Production variants

- **ECS**: set `demo_mode: false`, point `cluster`/`task_definition`/`subnets` at a real GPU-backed capacity provider, and configure `metadata_checks` thresholds tuned to your own extraction's real error rate.
- **Bedrock**: set `demo_mode: false` and ensure the configured `aws_region` has `bedrock-runtime` access; set `min_valid_rate` on the `output_schema` check to gate on quality.
- **Automated pick**: chain a comparison asset that reads both assets' metadata via `context.instance.get_latest_materialization_event()` and picks a winner per batch based on cost/latency/schema_valid_rate.
- **Swap the source**: replace `synthetic_data_generator` with your real document ingestion for the Bedrock side.

## See also

- [`ecs_pipes_asset` README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/infrastructure/ecs_pipes_asset/README.md)
- [`bedrock_converse_asset` README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/bedrock_converse_asset/README.md)
- Browse the [walkthrough index](README.md) for related demos across every component family.
