# Nano Banana — `gemini_image_generation` end-to-end
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

A native (no-LiteLLM) Nano Banana demo: 3 product hero-shot prompts →
Gemini 2.5 Flash Image generates 3 PNGs → downstream pandas asset
reads each PNG and reports dimensions. Full lineage in the asset
graph.

```
image_prompts_df          (3 product hero-shot prompts)
        │
        └── product_hero_images  ← gemini_image_generation
                                  (gemini-2.5-flash-image-preview, a.k.a. Nano Banana)
                                  PNGs saved to /tmp/nano_banana_demo
                  │
                  └── image_size_report  ← pandas (reads each PNG, emits dims + size)
```

## Components used

| Component | What it does |
|---|---|
| `gemini_image_generation` | Native single-vendor wrapper for Google's Gemini 2.5 Flash Image. Goes directly through the `google-genai` SDK — no LiteLLM dependency. Supports text-to-image and image-to-image editing. |

## Validation status

- **`gemini_image_generation`: code** — `dg check defs` passes. Component
  YAML loads cleanly, asset graph wires up, schema is correct. Full
  RUN_SUCCESS pending a real `GEMINI_API_KEY` (run the demo as
  documented and the validation level bumps to `live`).

## Cost

**~\$0.012** for 3 images via `gemini-2.5-flash-image-preview`.

## Required env vars

```bash
GEMINI_API_KEY=...    # or GOOGLE_API_KEY (component falls back)
```

Get a key at <https://aistudio.google.com/app/apikey>.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_nano_banana_demo.sh | bash
cd nano-banana-demo
uv run dg launch --assets '*'
```

Open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

Inspect the generated PNGs:

```bash
ls -la /tmp/nano_banana_demo/
```

## Why a native (non-LiteLLM) component?

The registry already has `litellm_image_generation` which routes to
DALL-E, Stable Diffusion, Replicate, and (via `gemini/*` model ids)
Nano Banana itself. We ship `gemini_image_generation` alongside it
because:

- **Single-vendor shops** standardize on one provider and don't
  want the LiteLLM router as an extra dep.
- **Vendor-specific features** like Gemini's
  `response_modalities=["IMAGE", "TEXT"]` and image-to-image edit
  semantics are surfaced directly.
- **Drop-in field shape** — `upstream_asset_key`, `prompt_column`,
  `output_path_column`, etc. match `litellm_image_generation`, so
  swapping is just changing the `type:` line.

| Pick `gemini_image_generation` when... | Pick `litellm_image_generation` when... |
|---|---|
| Stack is Google-only | You multiplex across DALL-E, SD, Replicate, etc. |
| You need image-to-image editing | You only need text-to-image |
| You want native Gemini features (response_modalities) | You want a unified config across providers |

## Image-to-image editing

The native component supports edit mode: provide a column with
source-image file paths via `input_image_column:` and Gemini will
edit the source guided by the prompt.

```yaml
attributes:
  prompt_column: edit_instruction
  input_image_column: source_image_path
```

The component sends `[prompt, source_image_bytes]` as the request
contents — Gemini interprets the source as the canvas and the
prompt as the edit instructions.

## See also

<!-- TODO: link related walkthroughs -->
