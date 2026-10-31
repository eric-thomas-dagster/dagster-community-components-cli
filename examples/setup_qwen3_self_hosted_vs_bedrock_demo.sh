#!/usr/bin/env bash
# setup_qwen3_self_hosted_vs_bedrock_demo.sh
#
# The question every team self-hosting an open-weight model eventually asks:
# "would a managed API be cheaper/simpler for this workload?" This demo puts
# BOTH inference paths for the same model family side by side as real Dagster
# assets, so the comparison lives in the asset graph, not a spreadsheet.
#
#   invoice_extraction_self_hosted   — EcsPipesAssetComponent: launches a
#     GPU-backed ECS task (Qwen3 fine-tuned on your document layout) via
#     Dagster Pipes. demo_mode runs the "task" as a real local subprocess
#     against a fake ECS/CloudWatch boundary -- the real PipesECSClient /
#     PipesCloudWatchMessageReader code runs unmodified.
#
#   ticket_triage_bedrock            — BedrockConverseAssetComponent: calls
#     AWS Bedrock's Converse API directly (boto3, not litellm) per row of an
#     upstream DataFrame, with JSON-schema-validated structured output.
#
# They process different sample workloads on purpose (the ECS task simulates
# documents already staged for a GPU container; Bedrock processes a normal
# upstream DataFrame) -- the point isn't a literal diff of outputs, it's that
# both inference ARCHITECTURES are real, comparable Dagster assets with their
# own cost/latency/quality metadata, side by side in one graph.
#
# What it demonstrates
#   • synthetic_data_generator  — sample support tickets for the Bedrock side
#   • ecs_pipes_asset           — self-hosted GPU batch extraction via Pipes
#   • bedrock_converse_asset    — managed per-row Bedrock Converse baseline
#
# Cost: $0 — both components run demo_mode: true. No AWS account needed.
# Point either one at a real account by setting demo_mode: false (ECS needs
# a real cluster/task_definition; Bedrock needs bedrock-runtime access in
# the configured aws_region).
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_qwen3_self_hosted_vs_bedrock_demo.sh                 # → qwen3_demo/
#   ./setup_qwen3_self_hosted_vs_bedrock_demo.sh my_project       # custom name

set -eo pipefail

PROJECT_NAME="${1:-qwen3_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

# Explicit `uv add` rather than relying on `dagster-component add --auto-install`
# alone -- auto-install's own pip step has been unreliable in this CLI version.
info "Installing deps (pandas, boto3, dagster-aws)…"
uv add -q pandas boto3 dagster-aws
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

info "Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add synthetic_data_generator --auto-install
$CLI add ecs_pipes_asset --auto-install
$CLI add bedrock_converse_asset --auto-install

info "Writing the self-hosted ECS task's inference script"
mkdir -p inference_task
cat > inference_task/run_batch.py <<'PY'
"""Stand-in for a self-hosted Qwen3 GPU batch-extraction job -- the same
contract a real ECS task's container would implement: open a Pipes context,
do the work, report metadata back.
"""
import json
import time

from dagster_pipes import open_dagster_pipes

DOCS = [
    {"invoice_number": "INV-1001", "carrier_name": "Freight Co A", "total_amount": 4210.55},
    {"invoice_number": "INV-1002", "carrier_name": "Freight Co B", "total_amount": 1899.00},
    {"invoice_number": "INV-1003", "carrier_name": "Freight Co A", "total_amount": 7320.10},
]

with open_dagster_pipes() as pipes:
    pipes.log.info(f"self-hosted Qwen3 extraction: processing {len(DOCS)} invoices")
    start = time.time()
    extracted = list(DOCS)  # a real task runs the local Qwen3 model here
    latency_ms = int((time.time() - start) * 1000) + 820  # + simulated GPU inference time

    pipes.report_asset_materialization(
        metadata={
            "schema_valid_rate": 1.0,
            "docs_failed": 0,
            "rows_processed": len(DOCS),
            "latency_ms_per_doc": latency_ms // len(DOCS),
            "sample_extraction": json.dumps(extracted[0]),
        },
    )
PY

info "Writing defs.yaml (source → self-hosted ECS path, source → Bedrock path)"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets_sample
  schema_type: support_tickets
  row_count: 5
  random_state: 42
  group_name: source
EOF

cat > "src/$PKG/defs/bedrock_converse_asset/defs.yaml" <<EOF
type: $PKG.components.bedrock_converse_asset.component.BedrockConverseAssetComponent
attributes:
  asset_name: ticket_triage_bedrock
  upstream_asset_key: support_tickets_sample
  input_column: ticket_text
  input_type: text

  aws_region: us-east-2
  model_id: qwen.qwen3-vl-235b-a22b
  prompt_template: >-
    Classify this support ticket. Return JSON with category
    (billing/technical/account/feature_request/other) and urgency (low/medium/high).

    Ticket: {input}

  output_schema:
    type: object
    properties:
      category: {type: string}
      urgency: {type: string}

  group_name: triage
  kinds: [bedrock, aws, llm]
  demo_mode: true
EOF

cat > "src/$PKG/defs/ecs_pipes_asset/defs.yaml" <<EOF
type: $PKG.components.ecs_pipes_asset.component.EcsPipesAssetComponent
attributes:
  asset_name: invoice_extraction_self_hosted

  task_definition: my-qwen3-extract:1
  container_name: inference
  cluster: my-gpu-cluster
  subnets: [subnet-0123456789abcdef0]
  aws_region: us-east-2
  capacity_provider: gpu-g6e

  log_group: /ecs/my-qwen3-extract
  metadata_checks:
    - {name: schema_valid_rate, metadata_key: schema_valid_rate, comparator: gte, threshold: 0.98, severity: ERROR}

  group_name: triage
  kinds: [ecs, pipes, aws, gpu]

  demo_mode: true
  local_script_path: inference_task/run_batch.py
EOF

ok "Wrote 3 defs.yaml"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Materializing both inference paths"
uv run dg launch --assets '*' 2>&1 | tail -20

echo
ok "Demo complete."
echo
cat <<EOF
Two inference architectures, same model family, side by side:

  invoice_extraction_self_hosted  — GPU ECS task via Dagster Pipes. Metadata:
    schema_valid_rate, docs_failed, rows_processed, latency_ms_per_doc.
  ticket_triage_bedrock            — Bedrock Converse, one call per upstream
    row. Output columns: category, urgency (JSON-schema validated).

Inspect the results:
  cd $PROJECT_NAME
  uv run dg dev
    → click invoice_extraction_self_hosted → Metadata tab for the GPU task's
      reported stats
    → click ticket_triage_bedrock → Asset Materializations for the row-level
      category/urgency output

Point either path at a real AWS account:
  ECS:      demo_mode: false + a real cluster/task_definition/subnets
  Bedrock:  demo_mode: false + bedrock-runtime access in aws_region

Production variants:
  • Swap synthetic_data_generator for your real document source
  • Add metadata_checks thresholds tuned to your own extraction's real
    error rate (ecs_pipes_asset) or min_valid_rate on bedrock_converse_asset's
    output_schema check
  • Chain a cost/latency comparison asset reading both assets' metadata via
    context.instance.get_latest_materialization_event() to pick a winner
    automatically per batch
EOF
