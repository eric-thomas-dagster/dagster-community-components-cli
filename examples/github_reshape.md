# GitHub Reshape — Resource + Sink Pattern

**Components:**
- `GithubResourceComponent` (`resources/github_resource`)
- `GitHubIssueUpsertComponent` (`assets/sinks/github_issue_upsert`)
- `InlineDataframeComponent` (`assets/sources/inline_dataframe`)

**Script:** [`setup_github_reshape_demo.sh`](./setup_github_reshape_demo.sh)
**Cost:** $0 (GitHub REST API is free within the 5000/hr authenticated rate limit)
**Duration:** ~30 seconds from cold to green (including a 10s wait for GitHub to index new issues)
**Validated:** 2026-08-15 (RUN_SUCCESS end-to-end against `eric-thomas-dagster/scratch`. Three back-to-back runs all showed `0 created, 5 updated`; final state confirmed 5 unique keys with correct open/closed states — no duplicates.)

> ✅ **Dagster+ Serverless:** deploys as-is (GitHub API is HTTP-based, no local dependencies)

## What it demonstrates

Same reshape pattern as the [Notion demo](./notion_reshape.md), applied to a second SaaS API with no orchestration primitive: a rich `_resource` with convenience methods + a purpose-built `_upsert` sink component.

Replaces the old `github_workspace` component (which enumerated repos × workflows and was really just multi-repo ingestion in workspace clothing).

## Pipeline

```
┌────────────────────────┐
│  incidents_seed        │  InlineDataframeComponent
│  (5 rows in defs.yaml) │
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────────────┐         ┌────────────────────────────────┐
│  github_incidents_mirror       │ ──────▶ │  GitHub issues in <repo>       │
│  GitHubIssueUpsertComponent    │         │  (5 issues, keyed by body      │
│                                │         │   marker <!-- dagster-key: -->)│
└────────────────────────────────┘         └────────────────────────────────┘
```

Second and subsequent runs match rows to existing issues by the body-embedded marker, then patch title/body/labels/state. Titles can be edited in the GitHub UI without breaking the sync — only the marker matters.

## The sink component

### `GitHubIssueUpsertComponent`

Mirrors DataFrame rows into GitHub issues. Each row's `key_column` value gets embedded in the issue body as `<!-- dagster-key: <value> -->`; on subsequent runs the sink scans existing issues for this marker and updates matches instead of creating duplicates.

```yaml
type: dagster_community_components.GitHubIssueUpsertComponent
attributes:
  asset_name: github_incidents_mirror
  upstream_asset_key: incidents_seed
  repo: my-org/incidents-tracker
  resource_key: github
  key_column: incident_id          # → embedded as HTML comment marker
  title_column: name
  body_column: description
  labels_column: labels            # comma-separated string or list
  state_column: state              # 'open' or 'closed'
  default_labels: [auto-synced]    # always applied on top of labels_column
  close_missing: false             # true = full mirror (closes issues not in upstream)
```

**Why marker-based, not title-based:** titles routinely get edited by humans reading GitHub. A title-based match would create a duplicate every time someone edited a title. The HTML comment marker is invisible in the rendered issue and stable across edits.

## `GitHubResource` convenience methods (21 total)

Beyond the sink, the resource exposes what most Dagster assets need to talk to GitHub without touching HTTP:

**Reads:** `get_repo`, `list_issues` / `iter_issues`, `get_issue`, `list_pull_requests` / `iter_pull_requests`, `get_pull_request`, `list_commits`, `list_workflow_runs`, `get_workflow_run`, `list_releases`, `list_labels`, `whoami`.

**Writes:** `create_issue`, `update_issue`, `close_issue`, `add_issue_comment`, `create_release`, `dispatch_workflow`, `set_labels`.

**Escape hatch:** `get_client()` returns an authenticated `requests.Session` pointed at the API base URL.

Supports GitHub Enterprise Server via the `api_base_url` field. Auto-paginates via `Link` headers.

## Requirements

- **`GITHUB_TOKEN`** — a PAT with `Issues: Read and write` on the target repo. Fine-grained is fine; classic with `repo` scope also works.
- **`GITHUB_REPO`** — a scratch repo in `owner/name` form. Cleanup at end of run closes all issues labeled `dagster-demo`, so nothing is left open. (GitHub doesn't allow deleting issues via API, so closed demo issues will linger in the repo — recommend using a truly disposable scratch repo the first time.)
- `uv` / `uvx`, Python 3.12+.

## Running

```bash
export GITHUB_TOKEN=ghp_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
export GITHUB_REPO=my-user/scratch-repo
./setup_github_reshape_demo.sh
```

The script scaffolds a Dagster project, materializes the pipeline twice with a 10-second gap between runs, and verifies the outcome via the GitHub API. The gap exists because GitHub's issue-list endpoint is **eventually consistent** — brand-new issues take a few seconds to appear via `/repos/{owner}/{name}/issues`. Real pipelines run minutes apart and never hit this; the demo intentionally re-runs to prove idempotency, which is why it waits.

## Auth notes

The community `GithubResourceComponent` uses PAT (personal access token) auth — the simplest path and enough for most demos. For **GitHub App** auth (installation tokens, higher rate limits, org-scale deployments), pair this resource with the official [`dagster_github.GithubResource`](https://docs.dagster.io/integrations/libraries/github) under a different resource key.

## Cleanup

The setup script's final step closes every issue labeled `dagster-demo`. To remove them entirely, delete the scratch repo (GitHub REST doesn't allow deleting individual issues).
