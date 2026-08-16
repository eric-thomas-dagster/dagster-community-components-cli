# Jira — DataFrame → Jira Issues Upsert

**Components:**
- `JiraResourceComponent` (`resources/jira_resource`)
- `JiraIssueUpsertComponent` (`assets/sinks/jira_issue_upsert`)
- `InlineDataframeComponent` (`assets/sources/inline_dataframe`)

**Script:** [`setup_jira_demo.sh`](./setup_jira_demo.sh)
**Cost:** $0 (Jira Cloud free tier — 10 users, no time limit)
**Duration:** ~30 seconds from cold to green (including a 10s wait for JQL indexing)
**Validated:** 2026-08-15 (RUN_SUCCESS end-to-end against a fresh `dagsterlabs-team-*.atlassian.net` workspace + `SE` project. Both runs showed `0 created, 5 updated` after initial creation, then cleanup transitioned all 5 to Done.)

> ✅ **Dagster+ Serverless:** deploys as-is (Jira Cloud REST is HTTP-based, no local dependencies)

## What it demonstrates

Applies the same resource + sink pattern as [Notion](./notion.md) and [GitHub](./github.md) to a third SaaS API with no orchestration primitive: a rich `_resource` with convenience methods + a purpose-built `_upsert` sink.

Replaces the old `jira_workspace` component (which enumerated projects × boards — dlt-shaped multi-project ingestion in workspace clothing).

## Pipeline

```
┌────────────────────────┐
│  incidents_seed        │  InlineDataframeComponent
│  (5 rows in defs.yaml) │
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────────────┐         ┌───────────────────────────────────┐
│  jira_incidents_mirror         │ ──────▶ │  Jira issues in <project>         │
│  JiraIssueUpsertComponent      │         │  (5 issues, keyed by label        │
│                                │         │   `dagsterkey-<value>`)           │
└────────────────────────────────┘         └───────────────────────────────────┘
```

Re-runs match rows to existing issues by the `dagsterkey-<value>` label via server-side JQL, then patch summary/description/labels/status.

## The sink component

### `JiraIssueUpsertComponent`

Mirrors DataFrame rows into Jira issues in a target project. Each row's `key_column` value is added as a label of the form `dagsterkey-<value>`; on subsequent runs the sink queries JQL for that project's labeled issues and updates matches instead of creating duplicates.

```yaml
type: dagster_community_components.JiraIssueUpsertComponent
attributes:
  asset_name: jira_incidents_mirror
  upstream_asset_key: incidents_seed
  project_key: SCRATCH
  resource_key: jira
  key_column: incident_id             # → dagsterkey-INC-1001 label
  summary_column: name
  description_column: description
  labels_column: labels
  transition_column: status           # optional: 'Done', 'In Progress' — best-effort
  issue_type: Task
  default_labels: [auto-synced]
```

**Why label-based, not description-marker?** Jira issue descriptions are ADF (Atlassian Document Format, JSON) — HTML comments aren't a thing. Labels are first-class in Jira, searchable via JQL server-side (`labels = "dagsterkey-INC-1001"` is a fast indexed lookup), and stable across UI edits.

## `JiraResource` convenience methods (16 total)

**Reads:** `get_issue`, `search_issues` / `iter_search_issues` (JQL-based, uses the new `POST /search/jql` endpoint), `get_project`, `list_projects`, `list_transitions`, `get_comments`, `whoami`, `get_issue_types`.

**Writes:** `create_issue`, `update_issue`, `transition_issue` (by name — auto-resolves the transition ID from current state), `add_comment`, `assign_issue`, `delete_issue`.

**Escape hatch:** `get_client()` returns an authenticated `requests.Session`. Plain-text descriptions and comments are auto-wrapped into Atlassian Document Format — you don't build ADF trees yourself.

## Requirements

- **`JIRA_EMAIL`** — Atlassian account email (Basic auth username).
- **`JIRA_API_TOKEN`** — API token from [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens). No scopes needed on the classic token; it inherits your user's permissions.
- **`JIRA_BASE_URL`** — e.g. `https://<workspace>.atlassian.net`.
- **`JIRA_PROJECT_KEY`** — target project key (e.g. `SCRATCH`). Must already exist; create via the Jira UI.
- `uv` / `uvx`, Python 3.12+.

## Running

```bash
export JIRA_EMAIL=you@company.com
export JIRA_API_TOKEN=ATATT3xFf...
export JIRA_BASE_URL=https://your-workspace.atlassian.net
export JIRA_PROJECT_KEY=SCRATCH
./setup_jira_demo.sh
```

The script scaffolds a Dagster project, materializes the pipeline twice with a 10-second gap between runs (JQL indexing has a few-second delay for brand-new issues), and verifies via the Jira API. Cleanup at the end transitions every `dagster-demo`-labeled issue to Done.

## Auth notes

Uses classic **email + API-token Basic auth** — simplest path, works for any Atlassian user. The community `JiraResourceComponent` doesn't currently support OAuth 2.0 / Atlassian Forge auth; open an issue if you need it.

## Cleanup

The setup script transitions every demo issue to Done at the end. Jira REST does allow deleting issues via API (`DELETE /rest/api/3/issue/{key}`) — the resource exposes it as `delete_issue()` if you want to fully nuke the demo issues afterward. Prefer transitioning to Done in normal usage.
