# Cloud Vision + Translation — image labels translated to 4 languages
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end against real GCP**, full chain in <30s. Two new
GCP ML-API components running on a real image set, with the Translation
output as the final sink.

```
sample_images           (3 synthetic PNGs in /tmp/vision_translate_imgs/)
        │
        └── image_analysis        ← vision_api_asset
                                    (LABEL_DETECTION + OBJECT_LOCALIZATION
                                     + IMAGE_PROPERTIES, top 5 per feature)
                  │
                  └── image_with_top_label  ← pandas (pick top label)
                            │
                            └── analysis_translated  ← translation_api_asset
                                                       (es / fr / de / ja)
                                      │
                                      └── analysis_csv  ← /tmp/vision_translate.csv
```

## Components used

| Component | What it does |
|---|---|
| `vision_api_asset` | Cloud Vision per-row image analysis. 11 supported feature types (labels, objects, faces, landmarks, logos, OCR, NSFW safe-search, image properties, web detection, crop hints, document text). |
| `translation_api_asset` | Cloud Translation v3. Translate a column to N target languages — one new column per target. Per-row source-language auto-detect. |

## Validation status — both live

Real run output (from `/tmp/vision_translate.csv`):

| SKU | top_label | top_label_es | top_label_fr | top_label_de | top_label_ja | dominant RGB |
|---|---|---|---|---|---|---|
| FRUIT-1 | Red | Rojo | Rouge | Rot | 赤 | (220, 30, 30) |
| VEH-1 | Blue | Azul | Bleu | Blau | 青 | (40, 80, 200) |
| PLANT-1 | Clip art | Imágenes prediseñadas | Images clipart | Cliparts | クリップアート | (40, 160, 60) |

Note: Vision read these synthetic shapes primarily as colors — the strongest signal in flat solid-fill PNGs. Run on real photos for content labels.

## Cost

**~$0.005.** 3 images × 3 Vision features = 9 feature calls (~$0.0015 each above the free 1000/month tier; well within free for this demo). Translation: 3 strings × 4 langs = 12 translations totaling ~30 chars (~$0.0006 at $20/M chars).

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

Required SA roles: `roles/serviceusage.serviceUsageConsumer` + Vision API + Translation API enabled.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_vision_translate_demo.sh | bash
cd vision-translate-demo
uv run dg launch --assets '*'
cat /tmp/vision_translate.csv
```

## Drop-in extensions

Add OCR + translate text in scanned images:

```yaml
attributes:
  features: [TEXT_DETECTION]      # add OCR
  output_prefix: vision_

# downstream pandas: extract `vision_text` and feed to translation
```

Switch to image-from-GCS:

```yaml
attributes:
  image_column: gcs_uri          # column with `gs://bucket/path/image.jpg`
  image_source: gcs              # or `auto`
```

For document parsing (forms / tables / structured), use `document_ai_extractor` instead of Vision — Document AI parses field structure, Vision OCR returns flat text.

## See also

<!-- TODO: link related walkthroughs -->
