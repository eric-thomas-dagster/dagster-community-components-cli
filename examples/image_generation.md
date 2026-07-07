# Image Generation — LiteLLM row-wise images across many providers

**Component:** `litellm_image_generation` (+ `synthetic_data_generator` as upstream prompts)

**Script:** [`setup_image_generation_demo.sh`](./setup_image_generation_demo.sh)
**Cost:** ~$0.12 per run (3 × standard 1024×1024 DALL-E 3 images at ~$0.04 each)
**Validated path:** DALL-E 3 via OpenAI. Same YAML swaps to Stability, Imagen, Replicate, Bedrock, Nano Banana, etc. by changing `model:`.

## Why this exists

Product teams want AI-generated hero images, marketing variants, alt text, thumbnails — always **per-row** ("for each of these 200 products, generate an image"). The trick is the fan-out: it should be a Dagster asset (retries, partial re-runs, cost tracking, lineage to the product catalog), not a bespoke script.

`litellm_image_generation` is that asset. Point it at a DataFrame column of prompts, get a DataFrame column of image URLs. LiteLLM handles auth + provider quirks, so the same component works across every image-generation backend.

```
image_prompts_df       (3 product hero-image prompts from synthetic_data_generator)
        ↓
product_hero_images    ← litellm_image_generation (DALL-E 3 by default)
```

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `OPENAI_API_KEY` (for the default DALL-E 3 path — swap env var when swapping model)

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_image_generation_demo.sh -o setup_image_generation_demo.sh
chmod +x setup_image_generation_demo.sh
./setup_image_generation_demo.sh
```

## What the script does

1. Scaffolds a Dagster project + installs `pandas`, `litellm`, `openai`.
2. Installs two components via the CLI:
   - `synthetic_data_generator` — generates 3 rows in the `image_prompts` schema (each row has a `prompt` column).
   - `litellm_image_generation` — the image-generation asset.
3. Writes two `defs.yaml`:
   - `image_prompts_df` — synthetic prompts source
   - `product_hero_images` — `litellm_image_generation` reading `prompt` column, writing `image_url` column, using `dall-e-3` + `size: 1024x1024` + `quality: standard`
4. Ready to materialize with `uv run dg launch --assets '*'`.

## Swap to another provider (one YAML change)

Edit `defs/litellm_image_generation/defs.yaml` — the component is provider-agnostic, only `model:` and `api_key_env_var:` change.

### Nano Banana (Google Gemini 2.5 Flash Image)

```yaml
model: gemini/gemini-2.5-flash-image-preview
api_key_env_var: GEMINI_API_KEY
# size / quality are ignored by Gemini — LiteLLM passes only the prompt
```

### Stability (SDXL)

```yaml
model: stability/stable-diffusion-xl-1024-v1-0
api_key_env_var: STABILITY_API_KEY
```

### Imagen 3 on Vertex AI

```yaml
model: vertex_ai/imagen-3.0-generate-001
api_key_env_var: GOOGLE_APPLICATION_CREDENTIALS
```

### Replicate (FLUX, SDXL, etc.)

```yaml
model: replicate/black-forest-labs/flux-schnell
api_key_env_var: REPLICATE_API_KEY
```

## Extensions

- **Real product catalog upstream.** Swap `synthetic_data_generator` for a `snowflake_query_asset` / `bigquery_query_asset` / any DataFrame source reading your product table. Prompt column can be built in a `formula` component (e.g. `"studio photo of {name}, minimalist background, professional lighting"`).
- **Download + persist images.** The default `response_format: url` returns URLs that expire (usually 1 hour on OpenAI). Chain a downstream asset that downloads each URL and writes to S3 / GCS / ADLS.
- **Multi-provider fallback.** Wrap in a `vercel_ai_gateway_agent` for cross-provider routing (image-model equivalents exist across providers via LiteLLM).

## Related

- [LiteLLM Chat](./litellm_chat.md) — same LiteLLM abstraction, text-generation side.
- [Vercel AI Gateway Agent](./vercel_ai_gateway_agent.md) — one key routes across providers, with fallback chains.
