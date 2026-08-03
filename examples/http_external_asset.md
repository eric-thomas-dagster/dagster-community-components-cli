# HTTP External Asset — wraps any HTTP-driven external job runner

**Validated end-to-end against the public GitHub Actions REST API** —
RUN_SUCCESS materializing through the full chain in ~4s. Three asset
shapes covered: un-partitioned trigger/poll, downstream pandas
consumer, and daily-partitioned with `partition_key` flowing into the
HTTP request. No auth required.

```
github_workflow_run     ← http_external_asset (trigger → poll → metadata)
        │
        └─→ github_run_summary  ← pandas (consumes upstream metadata,
                                  writes /tmp/github_run_summary.csv)

github_runs_by_day      ← http_external_asset (DAILY-partitioned;
                          partition_key flows into ?created=YYYY-MM-DD)
```

## Components used

| Component | What it does |
|---|---|
| `http_external_asset` | Generic HTTP-driven external job wrapper (trigger → poll → fetch logs). Same shape as community wrappers like `fivetran_assets` / `airbyte_assets`, but declarative — a single component config defines N assets, each with its own trigger / status / logs spec. |

## Cost

**$0.** Anonymous GitHub REST API allows 60 req/hour/IP. The full demo
issues ~4 requests (un-partitioned trigger + poll, plus partition
trigger + poll), well under the budget.

## What this demo proves end-to-end

| Capability | Where shown | Validation |
|---|---|---|
| Trigger → poll → finalize loop | `github_workflow_run` | RUN_SUCCESS in 1.02s, external_run_id 25584195096 |
| JSONPath extractor + `equals` operator | `is_terminal: $.status equals completed` | terminal correctly detected on first poll |
| `any_of` boolean composition | `is_success` on partitioned asset accepts `success` OR `skipped` | matched against real conclusions |
| Multiple `metadata` extractors | `workflow_name`, `run_number`, `conclusion`, `run_started_at`, `run_url`, `head_branch`, `head_sha`, `event`, `actor_login` | all surfaced as Dagster MaterializeResult metadata |
| Downstream Dagster lineage | `github_run_summary` reads upstream materialization metadata via `context.instance` | RUN_SUCCESS, real CSV written with the upstream's metadata |
| Daily partitions | `github_runs_by_day` with `partition_type: daily` + `partition_start: 2026-04-01` | partition `2026-05-01` materialized, external run `25238007065` from that day |
| `{{ partition_key }}` Jinja templating into HTTP request | `query_params.created: "{% raw %}{{ partition_key }}{% endraw %}"` | server received `?created=2026-05-01` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_http_external_asset_demo.sh | bash
cd http-external-asset-demo

# Un-partitioned chain (http asset → downstream pandas summary):
uv run dg launch --assets github_workflow_run+

# A single daily partition:
uv run dg launch --assets github_runs_by_day --partition 2026-05-01
```

To target a different public repo:

```bash
TARGET_REPO=apache/airflow ./setup_http_external_asset_demo.sh airflow-demo
```

Open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

## Why GitHub Actions for the demo?

The component is meant for HTTP-driven job runners that **don't already
have a dedicated registry component** — internal job APIs,
less-common SaaS tools, GitHub Actions / Jenkins / CircleCI / Argo /
Kestra, or prototyping. (For Fivetran, Airbyte, dbt Cloud, Matillion,
Rivery, Precisely, Coalesce, Databricks, Dataiku — use the dedicated
component, not this.) We picked the GitHub Actions REST API for the
validation walkthrough because:

- **Public + no-auth.** Reproducible without secrets.
- **Real lifecycle.** Workflow runs go `queued → in_progress → completed`
  with terminal states `success / failure / cancelled / skipped /
  timed_out / action_required` — exactly the trigger/poll shape the
  component is designed for.
- **Real JSON contract.** Tests the JSONPath extractors, condition
  language, and metadata extraction against a production API contract,
  not a mock.
- **Filterable by date** via the `?created=YYYY-MM-DD` query param —
  ideal for proving the daily-partition pattern.

## Convert into a real (write-side) trigger

The demo is read-only. To wire this up to actually trigger a workflow:

```yaml
type: <pkg>.components.http_external_asset.component.HttpExternalAssetComponent
attributes:
  base_url: https://api.github.com
  auth_resource_key: github            # ← BearerTokenAuth(token=EnvVar("GITHUB_PAT"))
  assets:
    - key: deploy_pipeline
      trigger:
        method: POST
        path: /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches
        body:
          ref: main
          inputs:
            environment: production
        # /dispatches returns 204 with no body, so use from_python: to
        # follow up with /actions/runs and find the run we just kicked.
        run_id:
          from_python: "myproject.helpers:resolve_dispatched_run_id"
      status:
        path: /repos/{owner}/{repo}/actions/runs/{run_id}
        is_terminal:
          jsonpath: $.status
          equals: completed
        is_success:
          jsonpath: $.conclusion
          equals: success
```

Wire the auth in `definitions.py`:

```python
from dagster import EnvVar, Definitions
from <pkg>.components.http_external_asset.component import BearerTokenAuth

defs = Definitions.merge(
    ...,
    Definitions(resources={"github": BearerTokenAuth(token=EnvVar("GITHUB_PAT"))}),
)
```

The condition language (`jsonpath` / `regex` / `header` / `literal` +
`equals` / `in` / `matches` / `exists` / `truthy` / `gt` / `lt` +
`any_of` / `all_of` / `not`) is the same regardless of the upstream
service.

## See also

<!-- TODO: link related walkthroughs -->
