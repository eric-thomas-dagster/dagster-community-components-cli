# Event Automation — Prefect-Automations shape as one Dagster component

Prefect users get to wire triggers → actions in one YAML/UI object — no Python, no separate sensors and schedules and run-status handlers to keep in sync. Dagster has all the underlying primitives (`@sensor`, `@run_status_sensor`, `AutomationCondition`, freshness policies, asset checks) but they're Python-first. This demo shows the `EventAutomationComponent` — one YAML surface that collapses common trigger-action patterns into a single component, with real Dagster sensors under the covers.

## Setup

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_event_automation_demo.sh | bash

cd event-automation-demo
uv run dg dev
```

Then open <http://localhost:3000>.

## What the demo does

Two automations in one YAML shape each.

### `demo_alert_on_failure_plus_heartbeat`

`src/<pkg>/defs/automations/defs.yaml`:

```yaml
type: dagster_community_components.EventAutomationComponent
attributes:
  name: demo_alert_on_failure_plus_heartbeat
  default_status: RUNNING
  when:
    - type: run_status
      status: FAILURE
    - type: schedule
      cron: "* * * * *"
  then:
    - type: webhook
      url: "https://httpbin.org/post"
      method: POST
      body_template: >
        {"event":"{event_type}","job":"{job_name}","run_id":"{run_id}"}
```

**Two triggers, one action bundle.** Both fire the same webhook, but with different template tokens filled in.

### `production_alert_shape` (STOPPED by default)

The same shape you'd ship to prod — Slack + PagerDuty alerting via env-var-driven webhooks:

```yaml
type: dagster_community_components.EventAutomationComponent
attributes:
  name: production_alert_shape
  default_status: STOPPED
  when:
    - type: run_status
      status: FAILURE
      job_name: job_that_fails
  then:
    - type: slack
      webhook_url_env_var: SLACK_WEBHOOK_URL
      message: "🚨 {job_name} failed — run_id={run_id}"
    - type: pagerduty
      routing_key_env_var: PAGERDUTY_ROUTING_KEY
      severity: error
      summary_template: "Prod {job_name} failed"
      dedup_key_template: "prod-failure:{job_name}"
```

Left STOPPED so the demo doesn't need real credentials. To activate:

```bash
export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
export PAGERDUTY_ROUTING_KEY='...'
# Edit the defs.yaml → default_status: RUNNING
uv run dg dev
```

## Watch it fire

1. Open <http://localhost:3000>
2. Navigate to **Jobs → `job_that_fails`**
3. Click **Materialize** (or Launch Run) — it fails on purpose
4. Navigate to **Sensors** tab. You'll see two sensors from the automation:
   - `demo_alert_on_failure_plus_heartbeat__run_status_0` (the FAILURE watcher)
   - `demo_alert_on_failure_plus_heartbeat__schedule_1` (the cron heartbeat)
5. The `run_status_0` sensor ticks within ~30s of the failure and POSTs to `https://httpbin.org/post`. Click into its **Tick History** to see the sensor evaluation timeline.
6. The `schedule_1` sensor ticks every minute, hitting `https://httpbin.org/post` with `event_type=schedule`.

## Trigger + action catalog

Every automation is `when: [triggers…]` OR-composition + `then: [actions…]` all-run-sequentially.

**Triggers** (`when:`):

| Type | Fires on | Fields |
|---|---|---|
| `run_status` | Any Dagster run finishing with `SUCCESS/FAILURE/CANCELED/STARTED` | `status`, optional `job_name` filter |
| `asset_materialized` | Named assets get materialized | `asset_keys` |
| `schedule` | Cron | `cron`, `execution_timezone` (default UTC) |
| `http_poll` | GET / POST a URL — fires on response change, HTTP 2xx, or a JSON path being non-empty | `url`, `method`, `condition`, `json_path`, `minimum_interval_seconds` |
| `freshness_violation` | An asset hasn't been materialized recently enough | `asset_keys`, `max_age_minutes` |

**Actions** (`then:`):

| Type | Effect | Fields |
|---|---|---|
| `materialize` | Launch a materialization run | `asset_keys`, optional `partition_key` |
| `launch_job` | Launch a job | `job_name`, optional `tags` |
| `webhook` | Arbitrary HTTP call w/ templated body | `url`, `method`, `headers`, `body_template` |
| `slack` | Slack incoming webhook | `webhook_url_env_var`, `message`, optional `channel`/`username`/`icon_emoji` |
| `pagerduty` | PagerDuty Events API v2 | `routing_key_env_var`, `severity`, `summary_template`, `dedup_key_template`, `event_action` |
| `discord` | Discord webhook | `webhook_url_env_var`, `message` |
| `emit_event` | Emit for downstream sensors | `asset_key`, `metadata_template` |

**Template tokens available in every action:** `{event_type}`, `{run_id}`, `{job_name}`, `{asset_key}`, `{status}`, `{timestamp}`, `{message}`, `{url}`.

## Common shapes

**Reprocess on upstream change:**

```yaml
when:
  - type: asset_materialized
    asset_keys: [raw_data]
then:
  - type: materialize
    asset_keys: [derived_data, aggregated_data]
```

**Freshness → auto-heal + escalate:**

```yaml
when:
  - type: freshness_violation
    asset_keys: [hourly_summary]
    max_age_minutes: 90
then:
  - type: materialize
    asset_keys: [hourly_summary]        # attempt self-heal
  - type: pagerduty
    routing_key_env_var: PD_ROUTING_KEY
    severity: warning
    summary_template: "{asset_key} stale — {message}"
```

**External queue → job launch:**

```yaml
when:
  - type: http_poll
    url: "https://api.example.com/pending-batches"
    condition: json_path_present
    json_path: "pending"
    minimum_interval_seconds: 30
then:
  - type: launch_job
    job_name: process_batch
```

**Cron + webhook (heartbeat pattern):**

```yaml
when:
  - type: schedule
    cron: "0 * * * *"
then:
  - type: webhook
    url: "https://uptime.example.com/hourly-heartbeat"
    method: GET
```

## Why vs Dagster+ native alerting

Dagster+ Pro ships native Slack/PagerDuty/email alerting as a paid feature. This component intentionally duplicates the alerting surface so:

- **OSS Dagster users** get alert-on-failure without needing a Dagster+ Pro seat
- **Dagster+ Serverless** users on the entry tier can wire the same alerts without upgrading
- **Prefect migrators** can port their Automations 1:1 without changing plans

If you're already on Dagster+ Pro, the native notifications UI is more polished — one less moving piece in code. This component's value is the "same shape as Prefect Automations, works everywhere Dagster runs" story.

## Why vs writing a Python sensor

You could hand-write every automation as a `@sensor` — that's the current Dagster idiom. The component's value:

- **YAML-declarative** → editable in the Dagster+ components UI, no Python editing
- **Composable with other components** → drops into `defs.yaml` alongside anything else
- **Prefect-migration path** → same mental model, one-file-per-automation
- **Consistent template surface** → every action gets the same `{event_type}` / `{run_id}` / etc. tokens

Under the hood, each trigger IS a real Dagster sensor — you get the full Dagster+ sensor UI, tick history, backfill, everything. Zero magic.

## Verified

- **Component loads:** `dg check defs` → "All definitions loaded successfully"
- **Sensors materialize:** `dg list defs` shows one sensor per trigger, named `<automation>__<trigger_type>_<i>`
- **Registered in the community-components manifest** with `agent_hints` covering when-to-use / typical-upstream / typical-downstream / example-prompts

## See also

- Component source: [`sensors/event_automation/component.py`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/event_automation)
- Prefect Automations concept (for comparison): <https://docs.prefect.io/v3/concepts/automations>
- Umbrella deploy doc: [`deploying.md`](deploying.md)
