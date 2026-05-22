#!/usr/bin/env bash
# HuggingFace — full integration surface demo scaffold.
#
# WHAT THIS DEMONSTRATES
#   6 of 7 HuggingFace community components scaffolded by default; the
#   7th (huggingface_inference_endpoint) requires a customer-deployed
#   dedicated endpoint and is left out of the default scaffold.
#
#   Scaffolded by default:
#     - huggingface_pipeline (Inference API mode — needs HF_TOKEN)
#     - huggingface_chat_completion (OpenAI-compatible chat via HF router)
#     - huggingface_text_to_image (image generation w/ multi-provider routing)
#     - huggingface_dataset_asset (observable_source_asset for Hub datasets)
#     - huggingface_model_asset (observable_source_asset for Hub models)
#     - huggingface_space_status_sensor (sense Space restarts)
#
# Asset graph after `dg launch --assets '*'`:
#   hf/datasets/imdb              ← observable_source_asset (Hub dataset metadata, no token needed)
#   hf/models/sentiment_model     ← observable_source_asset (Hub model metadata, no token needed)
#   hf/quick_detect               ← pipeline asset (Inference API object-detection — needs HF_TOKEN)
#   hf/chat/photosynthesis        ← chat-completion asset (Kimi-K2 via router — needs HF_TOKEN)
#   hf/images/airship             ← text-to-image asset (FLUX.1 via wavespeed — needs HF_TOKEN)
#   space_rebuilt sensor          ← stopped by default; fires on Space RUNNING → downstream_eval_job
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
uv add -q huggingface-hub openai Pillow  # runtime deps for the assets scaffolded below
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 HuggingFace components (out of 7 in the family)"
echo ">>>   huggingface_inference_endpoint is omitted from the default scaffold —"
echo ">>>   it requires a real deployed Inference Endpoint name; install it manually"
echo ">>>   via 'dagster-component add huggingface_inference_endpoint' when you have one."
for c in huggingface_pipeline huggingface_chat_completion huggingface_text_to_image \
         huggingface_dataset_asset huggingface_model_asset \
         huggingface_space_status_sensor; do
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
# src/$PKG/defs/huggingface_pipeline/. Overwrite it with the Inference API
# variant (no heavy local deps) — this is the universally-runnable shape
# that just needs HF_TOKEN.
write_yaml "huggingface_pipeline" "type: $PKG.components.huggingface_pipeline.component.HuggingfacePipelineComponent
attributes:
  asset_key: hf/quick_detect
  task: object-detection
  model: facebook/detr-resnet-50
  mode: inference_api
  hf_token_env_var: HF_TOKEN
  inputs:
    - https://huggingface.co/datasets/mishig/sample_images/resolve/main/cats.jpg
  group_name: huggingface"

# Local-mode pipeline asset (transformers + torch) and dedicated-endpoint
# asset are NOT scaffolded by default — local pipeline requires ~500MB of
# torch + transformers, and the endpoint asset needs a real deployed
# Inference Endpoint name. Customers can copy the templates below into
# their defs.yaml when they're ready:
#
#   ## Local-mode pipeline (run transformers in-process):
#   # uv add transformers torch
#   # type: <PKG>.components.huggingface_pipeline.component.HuggingfacePipelineComponent
#   # attributes:
#   #   asset_key: hf/sentiment_local
#   #   task: text-classification
#   #   model: cardiffnlp/twitter-roberta-base-sentiment-latest
#   #   mode: local
#   #   inputs: ["I love this product!", "Terrible experience."]
#
#   ## Dedicated Inference Endpoint (paid; deploy first via HF):
#   # type: <PKG>.components.huggingface_inference_endpoint.component.HuggingfaceInferenceEndpointComponent
#   # attributes:
#   #   asset_key: hf/endpoint/sentiment
#   #   endpoint_name: <your-deployed-endpoint-name>
#   #   task: text-classification
#   #   inputs: ["Endpoint sanity check input."]
#   #   hf_token_env_var: HF_TOKEN

write_yaml "huggingface_chat_completion" "type: $PKG.components.huggingface_chat_completion.component.HuggingfaceChatCompletionComponent
attributes:
  asset_key: hf/chat/photosynthesis
  model: moonshotai/Kimi-K2-Instruct-0905
  prompt: 'Describe the process of photosynthesis in two short paragraphs.'
  max_tokens: 400
  hf_token_env_var: HF_TOKEN
  group_name: huggingface"

write_yaml "huggingface_text_to_image" "type: $PKG.components.huggingface_text_to_image.component.HuggingfaceTextToImageComponent
attributes:
  asset_key: hf/images/airship
  model: black-forest-labs/FLUX.1-dev
  provider: wavespeed
  prompts:
    - 'A steampunk airship in the clouds'
  output_dir: ./generated_images
  hf_token_env_var: HF_TOKEN
  group_name: huggingface"

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
echo "  export HF_TOKEN=hf_xxxxxxxxxxxxxxxx     # required for 3 of 5 default assets"
echo "  uv run dg dev                          # UI at http://localhost:3000"
echo ""
echo "What you'll see in dg dev:"
echo "  Assets group 'huggingface' (5 assets):"
echo "    - hf/datasets/imdb           (Observe — no token needed)"
echo "    - hf/models/sentiment_model  (Observe — no token needed)"
echo "    - hf/quick_detect            (Materialize — needs HF_TOKEN)"
echo "    - hf/chat/photosynthesis     (Materialize — needs HF_TOKEN, calls Kimi-K2 via router)"
echo "    - hf/images/airship          (Materialize — needs HF_TOKEN, generates a PNG via wavespeed)"
echo "  Sensor:"
echo "    - space_rebuilt              (stopped by default; edit space_id then flip on)"
echo "  Job:"
echo "    - downstream_eval_job        (no-op stub for the space sensor to trigger)"
echo ""
echo "To add the LOCAL pipeline asset (~500MB of model weights — transformers + torch):"
echo "    uv add transformers torch"
echo "    Copy the local-pipeline template from the setup script source into your defs.yaml"
echo ""
echo "To add the DEDICATED ENDPOINT asset (paid; needs a real HF Inference Endpoint):"
echo "    dagster-component add huggingface_inference_endpoint --auto-install"
echo "    Edit src/$PKG/defs/huggingface_inference_endpoint/defs.yaml — set endpoint_name"
echo ""
