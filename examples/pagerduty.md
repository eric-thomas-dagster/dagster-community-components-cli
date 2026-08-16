# PagerDuty — DataFrame → PagerDuty Incidents Upsert

**Components:**
- `PagerDutyResourceComponent` (`resources/pagerduty_resource`)
- `PagerDutyIncidentUpsertComponent` (`assets/sinks/pagerduty_incident_upsert`)
- `InlineDataframeComponent` (`assets/sources/inline_dataframe`)

**Script:** [`setup_pagerduty_demo.sh`](./setup_pagerduty_demo.sh)
**Cost:** $0 (PagerDuty free tier — 5 users, unlimited API calls)
**Duration:** ~30 seconds from cold to green
**Validated:** 2026-08-15 (RUN_SUCCESS end-to-end against dagsterlabs.pagerduty.com's Default Service. 2 back-to-back runs both `0 created, 5 updated`. Final state: 1 acknowledged / 2 triggered / 2 resolved.)

> ✅ **Dagster+ Serverless:** deploys as-is (PagerDuty REST + Events APIs are HTTP-based)

## What it demonstrates

Same resource + sink pattern as [Notion](./notion.md), [GitHub](./github.md), and [Jira](./jira.md), applied to the classic "monitoring → PagerDuty" story: mirror rows from a DataFrame (typically an anomaly-detection output, a triage rollup, a nightly-check result) into PagerDuty incidents keyed by a stable identifier.

## Pipeline

```
┌────────────────────────┐
│  incidents_seed        │  InlineDataframeComponent
│  (5 rows in defs.yaml) │
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────────────┐         ┌───────────────────────────────────┐
│  pagerduty_incidents_mirror    │ ──────▶ │  Incidents on <service>           │
│  PagerDutyIncidentUpsertComp   │         │  (5 incidents, keyed by           │
│                                │         │   incident_key = dagster-demo-*)  │
└────────────────────────────────┘         └───────────────────────────────────┘
```

Re-runs match rows to existing incidents by `incident_key` (PagerDuty's server-side dedup marker). Row status column drives per-row transitions to `acknowledged` / `resolved` (forward-only — PagerDuty doesn't support moving resolved incidents back to open).

## The sink component

### `PagerDutyIncidentUpsertComponent`

Mirrors DataFrame rows into PagerDuty incidents on a target service.

```yaml
type: dagster_community_components.PagerDutyIncidentUpsertComponent
attributes:
  asset_name: pagerduty_incidents_mirror
  upstream_asset_key: incidents_seed
  service_id: PXXXXXX             # PagerDuty service ID (from UI URL)
  resource_key: pd
  key_column: incident_id         # → incident_key = "dagster-{value}"
  title_column: name
  details_column: description
  urgency_column: urgency         # 'high' | 'low'
  status_column: status           # 'triggered' | 'acknowledged' | 'resolved'
  key_prefix: "dagster-"
```

**Why `incident_key`?** PagerDuty enforces dedup on this field server-side — POSTing an incident with an `incident_key` matching an open incident returns the existing one. This means the sink is idempotent by design: same DataFrame twice = same set of incidents, no duplicates.

## `PagerDutyResource` convenience methods (~20 total)

**Reads (REST API):** `whoami`, `list_services` / `iter_services`, `get_service`, `list_incidents` / `iter_incidents`, `get_incident`, `list_users`, `list_teams`, `list_schedules`, `list_escalation_policies`, `list_oncalls`.

**Writes (REST API):** `create_incident`, `update_incident`, `acknowledge_incident`, `resolve_incident`, `add_incident_note`.

**Events API v2:** `send_alert` (idempotent via `dedup_key`), `resolve_alert`. Useful for the "trigger an alert from a Dagster asset when a data-quality check fails" pattern — Events API is what monitoring tools use, and is dedicated to programmatic alerting.

**Escape hatch:** `get_client()` returns an authenticated `requests.Session` for the REST API.

## Requirements

- **`PAGERDUTY_API_TOKEN`** — REST API token (*Integrations → API Access Keys* in the PagerDuty UI, requires admin).
- **`PAGERDUTY_SERVICE_ID`** — target service ID (e.g. `PFF0H74`, from the UI URL of the service).
- **`PAGERDUTY_FROM_EMAIL`** — automatically resolved by the setup script from `/users/me`; the resource uses it as the `From:` header on writes (PagerDuty requires this).
- `uv` / `uvx`, Python 3.12+.

## Running

```bash
export PAGERDUTY_API_TOKEN=<your_token>
export PAGERDUTY_SERVICE_ID=PFF0H74
./setup_pagerduty_demo.sh
```

## Auth notes

PagerDuty has two API surfaces this resource covers:
- **REST API** (`api.pagerduty.com`) — general management via a token. Incident writes need a `From:` header with a valid user email.
- **Events API v2** (`events.pagerduty.com/v2/enqueue`) — alert dispatch via a per-service routing key (integration key). Different from the REST token.

The resource wraps both. Set `events_routing_key_env_var` only if you want `send_alert` / `resolve_alert`.

## Cleanup

The setup script's final step resolves every incident whose `incident_key` starts with `dagster-demo-INC-`. PagerDuty doesn't delete incidents — resolved is the terminal state.
