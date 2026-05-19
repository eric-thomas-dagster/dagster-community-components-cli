#!/usr/bin/env bash
# HTTP External Asset demo — http_external_asset against the public
# GitHub Actions REST API. No auth required.
#
# WHAT THIS DEMONSTRATES
#   The http_external_asset component wrapping a real, public, no-auth
#   HTTP-driven job runner — the GitHub Actions REST API on a public
#   repo. The pattern (trigger → poll → finalize) is the same shape as
#   Fivetran / Airbyte / dbt Cloud / Jenkins / internal job APIs.
#
#   The "external job" is a real GitHub Actions workflow run on the
#   public dagster-io/dagster repo:
#
#     trigger  = GET /repos/{owner}/{repo}/actions/runs?per_page=1
#                → returns the ID of the most-recent workflow run
#     status   = GET /repos/{owner}/{repo}/actions/runs/{run_id}
#                → poll until status=completed; assert conclusion=success
#     metadata = workflow name, conclusion, started/completed timestamps,
#                run URL — all extracted from the JSON response
#
#   Most recent runs on a busy public repo are already terminal by the
#   time we look them up, so the asset materializes on the first poll.
#
# Asset graph:
#   github_workflow_run  ← http_external_asset (trigger → poll)
#
# REQUIRED ENV VAR
#   None. Public GitHub REST API allows 60 anonymous req/hour/IP, plenty
#   for one demo run.
#
# COST while running
#   \$0. One trigger + 1–2 polls.

set -euo pipefail
PROJECT_DIR="${1:-http-external-asset-demo}"
TARGET_REPO="${TARGET_REPO:-dagster-io/dagster}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q httpx jinja2 jsonpath-ng pandas
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing http_external_asset"
$CLI add http_external_asset --auto-install

mkdir -p "src/$PKG/defs/http_external_asset"
cat > "src/$PKG/defs/http_external_asset/defs.yaml" <<EOF
type: $PKG.components.http_external_asset.component.HttpExternalAssetComponent
attributes:
  base_url: https://api.github.com
  assets:
    # ─── Asset 1: most-recent run (no partitions) ────────────────────────
    - key: github_workflow_run
      description: |
        Polls the most recent GitHub Actions workflow run on $TARGET_REPO
        and surfaces its conclusion + timing as Dagster metadata.
      group_name: external
      kinds: [http]
      tags:
        owner: data-platform
        upstream: github-actions

      trigger:
        # The "trigger" is read-only — it fetches the most recent run
        # and treats its id as the external run-id. (For an actually
        # write-side trigger, e.g. POST /repos/{owner}/{repo}/actions/
        # workflows/{workflow_id}/dispatches, you'd swap in a PAT via
        # auth_resource_key + BearerTokenAuth.)
        method: GET
        path: /repos/$TARGET_REPO/actions/runs
        query_params:
          per_page: "1"
        headers:
          Accept: application/vnd.github+json
          X-GitHub-Api-Version: "2022-11-28"
        run_id:
          jsonpath: \$.workflow_runs[0].id

      status:
        method: GET
        path: /repos/$TARGET_REPO/actions/runs/{run_id}
        poll_interval_seconds: 5
        timeout_seconds: 600
        headers:
          Accept: application/vnd.github+json
          X-GitHub-Api-Version: "2022-11-28"
        is_terminal:
          jsonpath: \$.status
          equals: completed
        is_success:
          jsonpath: \$.conclusion
          equals: success
        metadata:
          workflow_name:
            jsonpath: \$.name
          run_number:
            jsonpath: \$.run_number
          conclusion:
            jsonpath: \$.conclusion
          run_started_at:
            jsonpath: \$.run_started_at
          updated_at:
            jsonpath: \$.updated_at
          run_url:
            jsonpath: \$.html_url
          head_branch:
            jsonpath: \$.head_branch
          head_sha:
            jsonpath: \$.head_sha
          event:
            jsonpath: \$.event
          actor_login:
            jsonpath: \$.actor.login

    # ─── Asset 2: PARTITIONED by day — proves partition_key flows into the request ──
    - key: github_runs_by_day
      description: |
        Daily-partitioned asset: each partition fetches workflow runs
        for that calendar day from the GitHub Actions API. Demonstrates
        partition_key flowing into the request via Jinja templating
        (\`created\` query param uses {% raw %}{{ partition_key }}{% endraw %}).
      group_name: external
      kinds: [http]
      tags:
        owner: data-platform

      partition_type: daily
      partition_start: "2026-04-01"

      trigger:
        method: GET
        path: /repos/$TARGET_REPO/actions/runs
        query_params:
          per_page: "1"
          created: "{% raw %}{{ partition_key }}{% endraw %}"
        headers:
          Accept: application/vnd.github+json
          X-GitHub-Api-Version: "2022-11-28"
        run_id:
          jsonpath: \$.workflow_runs[0].id

      status:
        method: GET
        path: /repos/$TARGET_REPO/actions/runs/{run_id}
        poll_interval_seconds: 5
        timeout_seconds: 600
        headers:
          Accept: application/vnd.github+json
          X-GitHub-Api-Version: "2022-11-28"
        is_terminal:
          jsonpath: \$.status
          equals: completed
        is_success:
          any_of:
            - jsonpath: \$.conclusion
              equals: success
            - jsonpath: \$.conclusion
              equals: skipped
        metadata:
          workflow_name:
            jsonpath: \$.name
          conclusion:
            jsonpath: \$.conclusion
          run_url:
            jsonpath: \$.html_url
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    github_workflow_run     ← http_external_asset (trigger → poll → metadata)

    github_runs_by_day      ← http_external_asset (DAILY-partitioned)

Materialize the un-partitioned chain end-to-end:
    cd $PROJECT_DIR
    uv run dg launch --assets github_workflow_run+

Materialize a single partition of the daily-partitioned asset:
    uv run dg launch --assets github_runs_by_day --partition 2026-05-01

(Pick any date back to 2026-04-01; the partition_key flows through to
the GitHub API as the \`?created=YYYY-MM-DD\` query param.)

The asset hits api.github.com (anonymous, 60 req/hr/IP rate limit). The
trigger fetches the most-recent workflow run on $TARGET_REPO; the
status poller asserts \`status == "completed"\` and surfaces the run's
conclusion, timing, branch, SHA, actor, and URL as Dagster metadata.

Inspect:
    uv run dg dev   # http://localhost:3000

To target a different public repo, re-run with TARGET_REPO=owner/name:
    TARGET_REPO=apache/airflow ./setup_http_external_asset_demo.sh airflow-demo

To convert into a real trigger (write side), edit defs.yaml:
  - trigger.method: POST
  - trigger.path: /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches
  - trigger.body: { ref: main }
  - top-level: auth_resource_key: github
  - definitions.py: GitHub PAT via BearerTokenAuth + EnvVar.
The trigger response is empty for dispatches, so trigger.run_id would
need to be fetched via a follow-up filter on /actions/runs. See the
http_external_asset README for the from_python: escape hatch pattern.
MSG
