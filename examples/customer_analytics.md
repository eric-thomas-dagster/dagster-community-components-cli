# Customer analytics — journey, segmentation, attribution, ML, NLP

**Validated end-to-end** — RUN_SUCCESS in seconds. 5 synthetic source
assets feed 6 marketing/retention components. All sklearn/pandas, no
SaaS calls.

```
customer_events       → customer_journeys           ← customer_journey_mapping
orders                → customer_rfm_segments       ← customer_segmentation (RFM)
marketing_touchpoints → channel_attribution         ← multi_touch_attribution
customer_features     → customer_churn_predictions  ← random_forest_model (classifier)
support_tickets       → support_clean_text          ← text_preprocessing
                          │
                          └→ support_topics         ← topic_modeling (LDA, n_topics=4)
```

## Components covered (6)

| Component | Purpose |
|---|---|
| `customer_journey_mapping` | Build per-customer event paths up to a conversion event; emit aggregate path stats |
| `customer_segmentation` | RFM (Recency / Frequency / Monetary) segmentation with quintile scoring |
| `multi_touch_attribution` | Per-channel revenue attribution: time-decay, last-touch, linear, etc. |
| `random_forest_model` | sklearn RandomForest for classification or regression — outputs predictions + feature importance |
| `text_preprocessing` | Lower-case, strip punctuation, optional stop-word removal, optional lemmatization |
| `topic_modeling` | sklearn LDA — discover N latent topics in a text column, emit top-words per topic |

## Cost

**$0.** pandas + sklearn, all local.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_customer_analytics_demo.sh | bash
cd customer-analytics-demo
uv run dg launch --assets '*'
uv run dg dev   # http://localhost:3000
```

## What you can do with these

- Pipe `customer_journeys` → `funnel_analysis` (already in `setup_analytics_demo.sh`) for funnel-shaped paths.
- Pipe `customer_rfm_segments` → `dataframe_to_table` (in `setup_local_sinks_demo.sh`) for RFM-tier persistence.
- Pipe `support_topics` → an LLM-based `text_classifier` (in `setup_ai_with_llm_demo.sh`) for topic-conditional classification.
