# Vision pipeline — image metadata + vision-LLM description
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Two AI components on the same image set, end-to-end.** A custom
asset generates 3 synthetic PNGs; both downstream components consume
them in parallel:

```
sample_images_df          (3 synthetic 320×240 PNGs in /tmp/vision_pipeline_images)
        │
        ├── image_metadata        ← image_metadata_extractor (PIL — width / height / format / EXIF)
        └── image_descriptions    ← vision_model (Anthropic claude-haiku-4-5)
```

## Components used

| Component | What it does |
|---|---|
| `image_metadata_extractor` | Pure PIL. Extracts width, height, format, mode, EXIF, GPS, optional histogram from a column of file paths. |
| `vision_model` | Vision-capable LLM (OpenAI gpt-4o or Anthropic Claude). Reads each image (URL / path / base64) and returns a free-text description for each row. |

## Validation status

- **`image_metadata_extractor`: live** — RUN_SUCCESS in 4.16s on the
  `sample_images_df → image_metadata` chain. 3 images materialized
  with all expected `img_*` columns + asset check passed.
- **`vision_model`: code** — YAML loads under `dg check defs` and
  initializes against the Anthropic provider. End-to-end RUN_SUCCESS
  pending an `ANTHROPIC_API_KEY`. Update the validation level to
  `live` after running with a real key.

## Cost

**~$0.005** total against `claude-haiku-4-5-20251001` for 3 images
when the full pipeline runs. The image_metadata side is $0.

## Required env vars

```bash
ANTHROPIC_API_KEY=sk-ant-...   # only needed for vision_model
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_vision_pipeline_demo.sh | bash
cd vision-pipeline-demo

# Just the metadata side ($0, no key required):
uv run dg launch --assets sample_images_df,image_metadata

# Full pipeline including vision_model (needs ANTHROPIC_API_KEY):
uv run dg launch --assets '*'
```

Open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

## Why a synthetic image generator?

The `sample_images_df` custom asset writes 3 solid-color PNGs with
text labels straight to `/tmp/vision_pipeline_images/`. No CDN, no
network for the image source — the demo is hermetic on the
$0 path, and only `vision_model` reaches out to Anthropic when
materialized.

## Convention drift caught during validation

`vision_model`'s shipped `example.yaml` used `source_asset:` while
the Pydantic field is `upstream_asset_key:` (the canonical registry
field name). Fixed the example to match. The component code didn't
need to change — only the example.

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
