# Pub/Sub Publish — DataFrame rows → Pub/Sub messages with attributes
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end against real APIs** (servicepulse-490502, demo-events topic).
5 order events published, all 5 pulled from the subscription with correct attribute routing.

```
events                 ← synthetic_data_generator (events schema)
       │
       └── events_published  ← pubsub_publish_asset
                              (publishes to demo-events topic, attaches
                               event_type + device as message attributes)
```

## Components used

| Component | What it does |
|---|---|
| `synthetic_data_generator` | Synthetic upstream. `schema_type: events` produces `(event_id, user_id, session_id, timestamp, event_type, page, duration_seconds, device, browser)` — `event_type` + `device` make natural filter attributes for subscription routing. |
| `pubsub_publish_asset` | Per-row publish to a Pub/Sub topic. Pick a `message_column` for the body (else the whole row gets JSON-serialized). `attribute_columns` become Pub/Sub message attributes — usable for subscription filter routing. Optional `ordering_key_column` for ordered delivery. |

## Live run output

```
gcloud pubsub subscriptions pull demo-events-sub --limit=10 --auto-ack
```

| DATA | MESSAGE_ID | ATTRIBUTES |
|---|---|---|
| `{"order_id": "o-123", "type": "order_placed", "amount_cents": 4999}` | 19021940496360810 | event_type=order_placed, region=us-east |
| `{"order_id": "o-123", "type": "order_shipped"}` | 19021940496360811 | event_type=order_shipped, region=us-east |
| `{"order_id": "o-124", "type": "order_placed", "amount_cents": 12500}` | 19021940496360812 | event_type=order_placed, region=us-west |
| `{"order_id": "o-100", "type": "order_refunded"}` | 19021940496360813 | event_type=order_refunded, region=eu |
| `{"order_id": "o-125", "type": "order_placed", "amount_cents": 999}` | 19021940496360814 | event_type=order_placed, region=us-east |

All 5 acked SUCCESS.

## Attribute-based routing

Pub/Sub subscriptions can filter by message attributes — meaning one topic can fan out to multiple consumers, each seeing only relevant events:

```bash
gcloud pubsub subscriptions create us-east-orders-sub \
  --topic=demo-events \
  --message-filter='attributes.region = "us-east" AND attributes.event_type = "order_placed"'
```

That's the standard pattern for event-driven multi-consumer systems. Filtering happens server-side — non-matching messages never count against the subscription's read budget.

## Ordering keys

Set `ordering_key_column` to a column like `order_id` — all events with the same key arrive in the order they were published. Requires:
- Component auto-enables it on the publisher (verified live).
- Subscription created with `--enable-message-ordering=true` (set at sub creation, immutable).

## Required setup (one-time)

```bash
# 1. Enable Pub/Sub API
# https://console.cloud.google.com/apis/library/pubsub.googleapis.com

# 2. Create topic + subscription
gcloud pubsub topics create demo-events
gcloud pubsub subscriptions create demo-events-sub --topic=demo-events

# 3. IAM
gcloud projects add-iam-policy-binding $GCP_PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" --role="roles/pubsub.publisher"
```

## Cost

**Free at this scale.** Pub/Sub free tier: 10 GB/mo combined publish + delivery throughput.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pubsub_publish_demo.sh | bash
cd pubsub-publish-demo
uv run dg launch --assets '*'

gcloud pubsub subscriptions pull demo-events-sub --limit=10 --auto-ack
```

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
