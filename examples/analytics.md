# Analytics mega-demo (40 components)
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end** — 40 sklearn / scipy / statsmodels analytics
components running on synthetic ML / customer / event / campaign data.
**$0 cost, no API keys.**

```
ml_dataset (200 rows × 6 cols)         geo_dataset (5 cities)
       │                                       │
       ├── linear_regression_model              ├── coordinate_transformer
       ├── gradient_boosting_model              ├── point_in_polygon
       ├── naive_bayes_model                    ├── geocoder
       ├── neural_network_model                 └── reverse_geocoder
       ├── gamma_regression
       ├── count_regression                customer_dataset
       ├── spline_model                          │
       ├── svm                                   ├── customer_360
       ├── k_centroids_diagnostics               ├── customer_health_score
       ├── append_cluster                        ├── lead_scoring
       ├── model_coefficients                    ├── product_recommendations
       ├── vif                                   ├── propensity_scoring
       ├── model_comparison                      └── (subscription_metrics — skip)
       ├── model_score
       ├── lift_chart                       event_dataset
       ├── stepwise                              │
       ├── multidimensional_scaling              ├── product_usage_analytics
       ├── oversample_field                      ├── funnel_analysis
       ├── simulation_sampling                   ├── event_data_standardizer
       ├── test_of_means                         └── product_analytics_standardizer
       └── optimization
                                            campaign / crm / ecom / support / ad_spend
                                                  │
                                                  ├── campaign_performance
                                                  ├── crm_data_standardizer
                                                  ├── ecommerce_standardizer
                                                  ├── marketing_data_standardizer
                                                  ├── support_ticket_standardizer
                                                  └── ad_spend_standardizer
```

## Components used

- `ad_spend_standardizer`
- `append_cluster`
- `campaign_performance`
- `coordinate_transformer`
- `count_regression`
- `crm_data_standardizer`
- `customer_360`
- `customer_health_score`
- `ecommerce_standardizer`
- `event_data_standardizer`
- `funnel_analysis`
- `gamma_regression`
- `geocoder`
- `gradient_boosting_model`
- `k_centroids_diagnostics`
- `lead_scoring`
- `lift_chart`
- `linear_regression_model`
- `marketing_data_standardizer`
- `model_coefficients`
- `model_comparison`
- `model_score`
- `multidimensional_scaling`
- `naive_bayes_model`
- `neural_network_model`
- `optimization`
- `oversample_field`
- `point_in_polygon`
- `priority_scorer`
- `product_analytics_standardizer`
- `product_recommendations`
- `product_usage_analytics`
- `propensity_scoring`
- `reverse_geocoder`
- `simulation_sampling`
- `spline_model`
- `stepwise`
- `subscription_metrics`
- `support_ticket_standardizer`
- `svm`
- `test_of_means`
- `vif`

## Components used (40)

### ML models (8) — all sklearn, share `target_column` + `feature_columns`

`linear_regression_model`, `gradient_boosting_model`, `naive_bayes_model`,
`neural_network_model`, `gamma_regression`, `count_regression`,
`spline_model`, `svm`. Note: `gradient_boosting_model` and
`neural_network_model` default to `task_type: classification` so set
`target_column` to a discrete y.

### Diagnostics + utility (7)

`k_centroids_diagnostics` (silhouette + elbow), `append_cluster`,
`model_coefficients`, `vif` (variance inflation), `model_comparison`,
`model_score` (load + score a saved sklearn pickle), `lift_chart`.

### Stats + sampling (6)

`stepwise` (forward feature selection), `multidimensional_scaling`,
`oversample_field` (SMOTE-style), `simulation_sampling`,
`test_of_means` (t-test), `optimization`.

### Geo (4)

`coordinate_transformer`, `point_in_polygon` (needs a GeoJSON file),
`geocoder` (geopy/Nominatim), `reverse_geocoder`.

### Business analytics (6)

`customer_360`, `customer_health_score`, `lead_scoring`,
`product_recommendations`, `product_usage_analytics`,
`propensity_scoring`. Each uses **specific** asset_key fields like
`crm_data_asset_key`, `event_data_asset_key`, etc. — not a generic
`upstream_asset_key`.

### Marketing/event (3)

`funnel_analysis`, `campaign_performance`, `event_data_standardizer`.

### SaaS data standardizers (6)

`ad_spend_standardizer`, `crm_data_standardizer`,
`ecommerce_standardizer`, `marketing_data_standardizer`,
`product_analytics_standardizer`, `support_ticket_standardizer`. All
take a `platform: <enum>` field that pins the source schema.

## Skipped (2 / 42)

| Component | Why |
|---|---|
| `priority_scorer` | Declares `**kwargs` ins= without binding any AssetIn → raises `Priority Scorer requires an upstream DataFrame` regardless of YAML |
| `subscription_metrics` | Silent subprocess crash on synthetic data — likely needs Stripe-shaped subscription history |

## Field-name reference (cheat sheet)

The analytics category has very inconsistent input-asset patterns. Key gotchas:

| Component | Input asset field |
|---|---|
| Most ML models | `upstream_asset_key` |
| `customer_health_score` | `customer_data_asset_key` (+ optional `subscription_*`, `product_*`, `support_*`) |
| `customer_360` | `crm_data_asset_key` (+ optional `stripe_*`, `ga4_*`, `marketing_*`) |
| `lead_scoring` | `lead_data_asset_key` (+ optional `behavioral_*`, `company_*`) |
| `product_usage_analytics` | `event_data_asset_key` |
| `subscription_metrics` | `stripe_data_asset_key` |
| `funnel_analysis` | `event_data_asset_key` |
| `ad_spend_standardizer` | `google_ads_asset` / `facebook_ads_asset` / `other_ad_platform_asset` |
| `priority_scorer`, `optimization`, `test_of_means` | various — see component README |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_analytics_demo.sh | bash
cd analytics-demo
uv run dg launch --assets '*'
```

## Cost

$0 — entirely local sklearn / scipy / statsmodels / shapely.
The geocoder uses Nominatim (free public service) with a low rate limit.

## See also

<!-- TODO: link related walkthroughs -->
