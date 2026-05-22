# HuggingFace — full integration surface

End-to-end walkthrough wiring **5 community components** that cover the HuggingFace surface from one Dagster project:

| Component | Purpose |
|---|---|
| [`huggingface_pipeline`](https://dagster-component-ui.vercel.app/c/huggingface_pipeline) | Run any `transformers.pipeline()` task on a list of inputs. No DataFrame required. Local or Inference API. |
| [`huggingface_dataset_asset`](https://dagster-component-ui.vercel.app/c/huggingface_dataset_asset) | Surface a Hub dataset's metadata (downloads, likes, configs, license) as an `observable_source_asset` |
| [`huggingface_model_asset`](https://dagster-component-ui.vercel.app/c/huggingface_model_asset) | Surface a Hub model's metadata (downloads, pipeline tag, last_modified, license) as an `observable_source_asset` |
| [`huggingface_inference_endpoint`](https://dagster-component-ui.vercel.app/c/huggingface_inference_endpoint) | Call a paid dedicated Inference Endpoint (different from the shared public API) |
| [`huggingface_space_status_sensor`](https://dagster-component-ui.vercel.app/c/huggingface_space_status_sensor) | Fire a `RunRequest` when a HF Space reaches a target stage (e.g. RUNNING after a rebuild) |

## Demo

```bash
bash setup_huggingface_demo.sh hf-demo
cd hf-demo
# Optional — only needed for inference_api / dedicated endpoint / gated models
export HF_TOKEN=hf_xxxxxxxxxxxxxxxx
uv run dg dev
```

UI at `http://localhost:3000`. The compile-check works without a token; runtime materialization of the local `huggingface_pipeline` asset will download a small text-classification model on first run.

## Pick the right component for the job

```
Need to RUN HuggingFace inference?
├── Local compute, prototyping → huggingface_pipeline (mode: local)
├── Cheap shared inference     → huggingface_pipeline (mode: inference_api)
└── Production, dedicated      → huggingface_inference_endpoint

Need to TRACK HuggingFace artifacts in the catalog?
├── A Hub dataset → huggingface_dataset_asset
└── A Hub model  → huggingface_model_asset

Need to REACT to a HuggingFace event?
└── Space deploy / rebuild → huggingface_space_status_sensor
```

## Asset graph

The demo scaffolds this graph:

```
hf/datasets/imdb                  ← observable_source_asset (Hub dataset metadata)
hf/models/sentiment_model         ← observable_source_asset (Hub model metadata)
                                   │
                                   ↓
hf/sentiment                      ← huggingface_pipeline (local sentiment classification)
hf/quick_detect                   ← huggingface_pipeline (Inference API object-detection)
hf/endpoint/sentiment             ← huggingface_inference_endpoint (dedicated; needs your endpoint)

space_rebuilt sensor              ← fires on Space RUNNING → downstream_eval_job
```

## When NOT to use these

For **batch inference over a DataFrame column**, use the task-specific components that already exist in the registry — they wrap `transformers.pipeline()` and integrate with the rest of the DataFrame-centric registry:

| HF task | Task-specific component (DataFrame batch) |
|---|---|
| object-detection | [`image_object_detector`](https://dagster-component-ui.vercel.app/c/image_object_detector) |
| image-classification | [`image_classifier`](https://dagster-component-ui.vercel.app/c/image_classifier) |
| text-classification | [`text_classifier`](https://dagster-component-ui.vercel.app/c/text_classifier) / [`sentiment_analyzer`](https://dagster-component-ui.vercel.app/c/sentiment_analyzer) |
| zero-shot-classification | [`zero_shot_classifier`](https://dagster-component-ui.vercel.app/c/zero_shot_classifier) |
| automatic-speech-recognition | [`audio_transcriber`](https://dagster-component-ui.vercel.app/c/audio_transcriber) |

`huggingface_pipeline` exists specifically for the "no DataFrame, just give me an answer" path — the fastest way to demo HF in Dagster.

## What the demo proves

- All 5 HuggingFace components scaffold from the CLI in one shot
- All load in `dg dev` without a token
- The `huggingface_pipeline` asset materializes a small classification model end-to-end
- The two `observable_source_asset` components emit `ObserveResult` with real Hub metadata (downloads, likes, last_modified)
- The Space status sensor compiles cleanly; flip to `default_status: running` when you have a real Space to watch

## Costs

| Capability | Cost |
|---|---|
| `huggingface_pipeline` `mode: local` | $0 (downloads a model, runs locally) |
| `huggingface_pipeline` `mode: inference_api` | Free for many Hub models; some are pay-as-you-go (cents per call) |
| `huggingface_inference_endpoint` | **Paid** — billed per endpoint-hour by HuggingFace |
| `huggingface_dataset_asset` / `huggingface_model_asset` | $0 (read-only Hub API calls) |
| `huggingface_space_status_sensor` | $0 (read-only Hub API calls) |

## See also

- [HuggingFace Hub documentation](https://huggingface.co/docs/huggingface_hub/index)
- [HuggingFace Inference API](https://huggingface.co/docs/api-inference/index)
- [HuggingFace Inference Endpoints](https://huggingface.co/docs/inference-endpoints/index)
- [`multimodal_ai.md`](multimodal_ai.md) — DataFrame-batch HuggingFace components (image_object_detector, image_captioner, etc.)
