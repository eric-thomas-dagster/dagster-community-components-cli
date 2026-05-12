# ML feature engineering — 6 pre-modeling transforms

**Validated end-to-end** — RUN_SUCCESS in seconds. Synthetic 100-row
customer dataset (with intentional missing values + outliers) feeds
through 6 standard pre-modeling transforms.

```
raw_customers (100 rows: 5 numeric + 3 categorical, with NaNs and outliers)
       │
       ├── customers_imputed       ← imputation (fill NaNs with median)
       ├── customers_no_outliers   ← outlier_clipper (IQR ±1.5)
       ├── customers_label_encoded ← label_encoder (categorical → ordinal int)
       ├── customers_one_hot       ← one_hot_encoding (categorical → boolean cols)
       ├── customers_scaled        ← feature_scaler (standard z-score)
       └── customers_binned        ← tile_binning (annual_spend → quintile tiers)
```

## Components covered (6)

| Component | Strategy options |
|---|---|
| `imputation` | `mean`, `median`, `mode`, `forward_fill`, `backward_fill`, `constant` |
| `outlier_clipper` | `iqr`, `zscore`, `percentile`; `clip` or `drop` action |
| `label_encoder` | Ordering: `frequency`, `alphabetical`, `as_seen` |
| `one_hot_encoding` | `drop_first`, `dummy_na`, `max_categories` |
| `feature_scaler` | `standard` (z-score), `minmax`, `robust`, `maxabs` |
| `tile_binning` | `equal_freq`, `equal_width`, `kmeans`; configurable `labels` |

## Cost

**$0.** Pandas + scikit-learn, all local. No GPU, no API.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_ml_features_demo.sh | bash
cd ml-features-demo
uv run dg launch --assets '*'
uv run dg dev   # http://localhost:3000
```

## Why this matters

These 6 transforms cover the standard pre-training pipeline that data
scientists usually rewrite per project. Wired up as Dagster components,
each step:

- Has typed config validated at component load (no `KeyError` on
  missing field at run-time).
- Materializes as a versioned asset with row-count + preview metadata —
  visible alongside the rest of your pipeline in the catalog.
- Auto-infers column lineage (each output column maps to its input
  column) for downstream model components.
- Composes naturally — `imputation` → `outlier_clipper` →
  `feature_scaler` → `train_test_splitter` → `gradient_boosting_model`
  is a complete end-to-end ML pipeline using only registry components.
