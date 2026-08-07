# Image Transform — synthetic PNGs → resized WebP thumbnails
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end** (local, no external services). 3 synthetic PNGs (640×640) → 3 WebP thumbnails (128px max-dimension, q=80) on disk.

```
sample_images       ← synthetic_image_generator (3 default PNGs, 640x640)
       │
       └── thumbnails  ← image_transform_asset (→ 128px WebP, q=80)
```

## Components used

| Component | What it does |
|---|---|
| `synthetic_image_generator` | Generates sample PNGs (built-in apple / blue car / green plant + optional custom set). Emits `(sku, name, kind, file_path)` DataFrame. |
| `image_transform_asset` | Pillow-based resize / crop / format-convert / grayscale. Reads a column of file paths, writes new files, adds `transformed_path` + `size_before` + `size_after` columns. |

## Live output

```
FRUIT-1_t.webp   574 bytes (from FRUIT-1.png, 640x640 → 128x128 WebP)
VEH-1_t.webp     406 bytes
PLANT-1_t.webp   406 bytes
```

## Pillow-only

No Cloud APIs, no extra binaries. Runs anywhere Python + Pillow does (`pip install pillow`).

## Typical extensions

| Goal | Setting |
|---|---|
| 224×224 model inputs | `resize_to: [224, 224]`, `preserve_aspect_ratio: false` |
| Center-crop to square 1:1 | `crop_to: [1024, 1024]` |
| Grayscale OCR preprocess | `grayscale: true`, `convert_to: png` |
| HEIC → JPEG | `convert_to: jpg` (needs `pillow-heif` for HEIC input) |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_image_transform_demo.sh | bash
cd image-transform-demo
uv run dg launch --assets '*'

ls -la /tmp/image_transform_demo_out/
```

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
