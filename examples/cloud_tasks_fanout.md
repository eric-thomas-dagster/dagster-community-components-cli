# Cloud Tasks Fan-out — async work dispatch from Dagster
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Validated end-to-end against real APIs** (servicepulse-490502 →
us-central1/demo-queue → httpbin.org). A Dagster asset emits 10
synthetic events; the `cloud_tasks_enqueue_asset` component
pushes each row as an HTTP task onto Cloud Tasks; the queue dispatches
them asynchronously to a target URL.

```
events                   ← synthetic_data_generator (events schema, 10 rows)
       │
       └── tasks_enqueued  ← cloud_tasks_enqueue_asset
                             (POSTs each row to https://httpbin.org/post
                              via demo-queue in us-central1)
```

## Components used

| Component | What it does |
|---|---|
| `synthetic_data_generator` | Synthetic upstream. `schema_type: events` produces event-log rows — each one becomes a Cloud Tasks job body. |
| `cloud_tasks_enqueue_asset` | Per-row HTTP task push to a Cloud Tasks queue. Supports body_columns, URL templating (`{col}` placeholders), `schedule_time_column` for deferred work, OIDC auth for invoking private Cloud Run / Cloud Functions, and `dispatch_deadline_seconds`. |

## Live run output

```
2026-05-11 08:35:48 - dagster - INFO - tasks_enqueued
  - 5 rows enqueued, 0 failed
2026-05-11 08:35:48 - dagster - DEBUG - RUN_SUCCESS
```

Queue state after run: 0 tasks (all dispatched + 200'd by httpbin within seconds).

## Why use it

| Pattern | Win |
|---|---|
| Fan-out N work items without blocking the Dagster run | Materialize fast; workers run async |
| Deferred work ("send reminder at T+24h") | Use `schedule_time_column` per row |
| Throttle outbound API calls | Queue-level rate limiting (default 500/s, 1000 concurrent) |
| Built-in retries | Cloud Tasks retries on 4xx/5xx per queue config (max 100 attempts by default) |
| Auth to private Cloud Run / Cloud Function targets | Set `oidc_service_account_email` — token minted automatically |

## Run

```bash
# 1) Enable Cloud Tasks API
# https://console.cloud.google.com/apis/library/cloudtasks.googleapis.com

# 2) Create a queue (one-time)
gcloud tasks queues create demo-queue --location=us-central1 \
  --project=$GCP_PROJECT_ID

# 3) Grant the service account roles/cloudtasks.enqueuer
gcloud projects add-iam-policy-binding $GCP_PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" --role="roles/cloudtasks.enqueuer"
```

Or just `roles/cloudtasks.admin` if the SA also needs to create the queue programmatically.

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GCP_PROJECT_ID=your-project
```

## Cost

**Free at this scale.** Cloud Tasks pricing: $0.40 per million tasks above 1M/month free tier.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cloud_tasks_fanout_demo.sh | bash
cd cloud-tasks-fanout-demo
uv run dg launch --assets '*'
```

## Verify dispatch

```bash
gcloud tasks queues describe demo-queue --location=us-central1
gcloud tasks list --queue=demo-queue --location=us-central1
```

After dispatch (typically seconds), the queue is empty — successful tasks are removed. To see them mid-flight, use a longer `dispatch_deadline_seconds` or schedule them in the future via `schedule_time_column`.

## What you can do downstream

- **Real workers**: replace `target_url` with a Cloud Run or Cloud Function URL + `oidc_service_account_email`. The queue handles retries / throttling for you.
- **Scheduled / deferred work**: add a `send_at` column to `pending_jobs` and set `schedule_time_column: send_at`. Cloud Tasks holds tasks until their scheduled time.
- **URL templating**: `target_url: https://api.example.com/orders/{order_id}/process` substitutes row values per task.
- **Backpressure on external APIs**: set `max_dispatches_per_second` on the queue to rate-limit outbound calls regardless of how fast Dagster materializes.

## Teardown

```bash
gcloud tasks queues delete demo-queue --location=us-central1 \
  --project=$GCP_PROJECT_ID --quiet
```

## See also

<!-- TODO: link related walkthroughs -->
