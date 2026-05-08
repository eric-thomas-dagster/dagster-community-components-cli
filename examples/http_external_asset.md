# HTTP External Asset — wraps any HTTP-driven external job runner

**Validated end-to-end against the public GitHub Actions REST API** —
RUN_SUCCESS materializing in 841ms. No auth required.

```
github_workflow_run  ← http_external_asset
   │
   ├── trigger: GET /repos/{owner}/{repo}/actions/runs?per_page=1
   │   → returns the ID of the most recent workflow run
   │
   ├── status:  GET /repos/{owner}/{repo}/actions/runs/{run_id}
   │   → polls until status="completed", asserts conclusion="success"
   │
   └── metadata extracted: workflow_name · run_number · conclusion ·
       run_started_at · updated_at · run_url · head_branch · head_sha ·
       event · actor_login
```

## Components covered (1)

| Component | What it does |
|---|---|
| `http_external_asset` | Generic HTTP-driven external job wrapper (trigger → poll → fetch logs). Same shape as community wrappers like `fivetran_assets` / `airbyte_assets`, but declarative — a single component config defines N assets, each with its own trigger / status / logs spec. |

## Cost

**$0.** Anonymous GitHub REST API allows 60 req/hour/IP, plenty for one
demo run (1 trigger + 1 poll = 2 requests).

## Run it

```bash
./setup_http_external_asset_demo.sh
cd http-external-asset-demo
uv run dg launch --assets github_workflow_run
```

To target a different public repo:

```bash
TARGET_REPO=apache/airflow ./setup_http_external_asset_demo.sh airflow-demo
```

Or open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

## Why GitHub Actions for the demo?

The component is meant for any HTTP-driven job runner — Fivetran, Airbyte,
dbt Cloud, Jenkins, internal job APIs. We picked the GitHub Actions REST
API for the validation walkthrough because it has all the right
properties:

- **Public + no-auth.** Reproducible without secrets.
- **Real lifecycle.** Workflow runs go `queued → in_progress → completed`
  with terminal states `success / failure / cancelled / skipped /
  timed_out / action_required` — exactly the trigger/poll shape the
  component is designed for.
- **Real JSON contract.** Tests the JSONPath extractors, condition
  language (`equals`, `in`), and metadata extraction against a
  production API contract, not a mock.
- **Already-terminal runs are fine.** Most-recent runs on a busy public
  repo are already terminal by the time the demo polls, so the asset
  materializes on the first poll without waiting.

## What got fixed validating this demo

The end-to-end run surfaced one real bug in the component:

- **`trigger.run_id` source default.** The `_ConditionExtractor` field
  `source` defaults to `status_response`, but at trigger time only
  `trigger_response` is available — so the extractor read None instead
  of the response body. Fixed by forcing `source=trigger_response` for
  the run-id extractor at evaluation time. Documented as a behavior
  override (the user's `source` setting is ignored on `trigger.run_id`
  — only one source makes sense at trigger time).

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
        # /dispatches returns 204 with no body, so we need a follow-up
        # filter on /actions/runs to find the run we just kicked. Use
        # the from_python: escape hatch for that:
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
