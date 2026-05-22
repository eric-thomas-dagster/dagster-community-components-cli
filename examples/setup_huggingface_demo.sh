#!/usr/bin/env bash
# HuggingFace — full integration surface demo scaffold.
#
# WHAT THIS DEMONSTRATES
#   5 HuggingFace community components in one Dagster project:
#     - huggingface_pipeline (local + Inference API single-input runners)
#     - huggingface_dataset_asset (observable_source_asset for Hub datasets)
#     - huggingface_model_asset (observable_source_asset for Hub models)
#     - huggingface_inference_endpoint (paid dedicated endpoints)
#     - huggingface_space_status_sensor (sense Space restarts)
#
# Asset graph:
#   hf/datasets/imdb              ← observable_source_asset (Hub dataset metadata)
#   hf/models/sentiment_model     ← observable_source_asset (Hub model metadata)
#   hf/sentiment                  ← pipeline asset (local sentiment classification)
#   hf/quick_detect               ← pipeline asset (Inference API object-detection — needs HF_TOKEN)
#   hf/endpoint/sentiment         ← dedicated endpoint asset (needs your endpoint)
#   space_rebuilt sensor          ← fires on Space RUNNING → downstream_eval_job
#
# COST: $0 for compile-check + observation assets. The local
#       huggingface_pipeline run downloads a small (~500MB) model on
#       first materialization. Inference API + dedicated endpoint
#       require an HF_TOKEN.

set -euo pipefail
PROJECT_DIR="${1:-hf-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11+
uv add -q huggingface-hub
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 HuggingFace components"
for c in huggingface_pipeline huggingface_dataset_asset huggingface_model_asset \
         huggingface_inference_endpoint huggingface_space_status_sensor; do
  $CLI add $c --auto-install
done

echo ">>> Writing defs.yaml for each component"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "huggingface_dataset_asset" "type: $PKG.components.huggingface_dataset_asset.component.HuggingfaceDatasetAssetComponent
attributes:
  asset_key: hf/datasets/imdb
  dataset_id: imdb
  group_name: huggingface
  description: 'Live metadata for the IMDB dataset on the HuggingFace Hub.'"

write_yaml "huggingface_model_asset" "type: $PKG.components.huggingface_model_asset.component.HuggingfaceModelAssetComponent
attributes:
  asset_key: hf/models/sentiment_model
  model_id: cardiffnlp/twitter-roberta-base-sentiment-latest
  group_name: huggingface
  description: 'Live Hub metadata for the sentiment model used in the pipeline asset below.'"

# `dg add huggingface_pipeline` already installed a default defs.yaml at
# src/$PKG/defs/huggingface_pipeline/. Overwrite it with the LOCAL variant
# (asset_key hf/sentiment), and write the API variant into a SECOND
# directory with a DIFFERENT asset_key (hf/quick_detect).
write_yaml "huggingface_pipeline" "type: $PKG.components.huggingface_pipeline.component.HuggingfacePipelineComponent
attributes:
  asset_key: hf/sentiment
  task: text-classification
  model: cardiffnlp/twitter-roberta-base-sentiment-latest
  mode: local
  inputs:
    - 'I love this product!'
    - 'Terrible experience, would not recommend.'
    - 'It is fine. Nothing special.'
  group_name: huggingface
  deps:
    - hf/models/sentiment_model"

# Second instance of the same component, different asset_key + Inference API mode.
mkdir -p "src/$PKG/defs/huggingface_pipeline_api"
cat > "src/$PKG/defs/huggingface_pipeline_api/defs.yaml" <<EOF
type: $PKG.components.huggingface_pipeline.component.HuggingfacePipelineComponent
attributes:
  asset_key: hf/quick_detect
  task: object-detection
  model: facebook/detr-resnet-50
  mode: inference_api
  hf_token_env_var: HF_TOKEN
  inputs:
    - https://huggingface.co/datasets/mishig/sample_images/resolve/main/cats.jpg
  group_name: huggingface
EOF

write_yaml "huggingface_inference_endpoint" "type: $PKG.components.huggingface_inference_endpoint.component.HuggingfaceInferenceEndpointComponent
attributes:
  asset_key: hf/endpoint/sentiment
  endpoint_name: REPLACE-WITH-YOUR-ENDPOINT-NAME
  task: text-classification
  inputs:
    - 'Endpoint sanity check input.'
  hf_token_env_var: HF_TOKEN
  group_name: huggingface
  description: 'Placeholder — set endpoint_name to a real dedicated endpoint in your HF account.'"

write_yaml "huggingface_space_status_sensor" "type: $PKG.components.huggingface_space_status_sensor.component.HuggingfaceSpaceStatusSensorComponent
attributes:
  sensor_name: space_rebuilt
  space_id: REPLACE-WITH-YOUR-SPACE-ID
  target_stages: ['RUNNING']
  job_name: downstream_eval_job
  hf_token_env_var: HF_TOKEN
  minimum_interval_seconds: 60
  default_status: stopped"

echo ">>> Adding a downstream Dagster job for the space sensor to trigger"
mkdir -p "src/$PKG/defs/downstream_eval"
cat > "src/$PKG/defs/downstream_eval/definitions.py" <<EOF
"""No-op downstream job — the space sensor fires a RunRequest at this job
when the Space hits its target stage. Replace the op body with eval work."""
import dagster as dg


@dg.op
def receive_space_signal(context: dg.OpExecutionContext) -> None:
    config = context.op_config or {}
    context.log.info(
        f"Space {config.get('huggingface_space_id', '(unset)')} "
        f"reached stage {config.get('huggingface_space_stage', '(unset)')} at "
        f"{config.get('huggingface_space_last_modified', '(unset)')}."
    )
    context.log.info("Run downstream evaluation work here.")


@dg.job
def downstream_eval_job():
    receive_space_signal()


defs = dg.Definitions(jobs=[downstream_eval_job])
EOF

echo ""
echo "============================================================"
echo "HuggingFace demo scaffolded at $PROJECT_DIR"
echo "============================================================"
echo ""
echo "Next steps:"
echo ""
echo "  cd $PROJECT_DIR"
echo "  # Optional: only needed for inference_api / dedicated endpoint / gated models"
echo "  export HF_TOKEN=hf_xxxxxxxxxxxxxxxx"
echo "  uv run dg dev                          # UI at http://localhost:3000"
echo ""
echo "What you'll see in dg dev:"
echo "  Assets group 'huggingface' with:"
echo "    - hf/datasets/imdb           (click 'Observe' to fetch live metadata)"
echo "    - hf/models/sentiment_model  (click 'Observe' to fetch live metadata)"
echo "    - hf/sentiment               (click 'Materialize' — downloads model on first run)"
echo "    - hf/quick_detect            (needs HF_TOKEN; calls Inference API)"
echo "    - hf/endpoint/sentiment      (edit defs.yaml first — set endpoint_name)"
echo "  Sensor:"
echo "    - space_rebuilt              (stopped by default; edit space_id then flip on)"
echo "  Job:"
echo "    - downstream_eval_job        (no-op stub for the space sensor to trigger)"
echo ""
echo "First materialization of hf/sentiment downloads ~500MB of model weights."
echo "Subsequent runs are instant."
echo ""
