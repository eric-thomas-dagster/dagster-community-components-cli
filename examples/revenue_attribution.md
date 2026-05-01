# Revenue attribution — split conversions across marketing channels

A 4-component pipeline that fans in two synthetic CSVs (5 marketing
campaigns + 120 Stripe charge events), runs `revenue_attribution` with
linear attribution, writes per-campaign metrics (spend, impressions,
clicks, conversions, ROI, ROAS, CAC).

## Pipeline

```
csv_file_ingestion (marketing) ┐
                                ├─→ revenue_attribution → dataframe_to_csv
csv_file_ingestion (revenue)   ┘
```

| # | Component | Category | Role |
|---|---|---|---|
| 1a | `csv_file_ingestion` (marketing) | ingestion | Load synthetic campaigns (Spring_Sale, Brand_Awareness, Retargeting, Black_Friday, Newsletter) |
| 1b | `csv_file_ingestion` (revenue) | ingestion | Load synthetic Stripe-shaped charges |
| 2 | `revenue_attribution` | analytics | Linear attribution model; aggregates spend + computes ROI / ROAS / CAC |
| 3 | `dataframe_to_csv` | sink | Per-campaign report |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_revenue_attribution_demo.sh | bash
cd revenue-attribution-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/campaign_attribution.csv` — sample run:

```
campaign_name,    spend,  impressions, clicks, conversions, attributed_revenue, roi,  roas, cac
Black_Friday,     15000,  200000,      8000,   580,         0,                  -1.0, 0.0,
Brand_Awareness,  12000,  300000,      3000,   80,          0,                  -1.0, 0.0,
Spring_Sale,      8000,   150000,      4500,   320,         0,                  -1.0, 0.0,
Retargeting,      4500,   80000,       6500,   410,         0,                  -1.0, 0.0,
Newsletter,       1500,   25000,       1200,   210,         0,                  -1.0, 0.0,
```

The `attributed_revenue` is 0 for synthetic data because the join key
(`customer_id`) doesn't match between the marketing CSV (which doesn't
have customer-level data) and the Stripe charges. With real
customer-keyed marketing events (e.g. UTM-tagged signups linked to
Stripe customer IDs), the linear attribution distributes revenue across
each customer's touchpoints.

## What this demo shows

- **First fan-in to a multi-source analytics component.**
  `revenue_attribution` declares both `marketing_data_asset` and
  `revenue_data_asset` — dg's autoloader wires both into the asset's
  inputs. Same pattern as the SpaceX join demo, but with two separate
  ingest assets each owning its own `defs.yaml`.
- **Same component installed twice via `--target-dir`.**
  `csv_file_ingestion` lives at `defs/csv_file_ingestion/` (marketing)
  and `defs/csv_revenue/` (revenue) — each with its own attributes.
- **Component-level computed metrics.** ROI / ROAS / CAC are computed
  internally from `spend` / `attributed_revenue` / `attributed_customers`;
  the column lineage records that.

## Extending

Real Stripe exports include `customer_id` per charge — link them with
UTM-tagged or session-tracked marketing events keyed on the same
`customer_id` to get non-zero attributed revenue. Switch
`attribution_model` to `time_decay` or `first_touch` for different
allocation behaviors.
