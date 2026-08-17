---
title: "Not every SaaS integration should be a workspace"
date: 2026-08-16
author: Eric Thomas
description: "A design essay on when to build a `_workspace` component, when to build a `_resource + _sink` pair, and how to tell the difference. Includes the taxonomy behind the community-components reshape of Notion, GitHub, Jira, and PagerDuty."
---

# Not every SaaS integration should be a workspace

*A design essay on when to build a `_workspace` component, when to build a
`_resource + _sink` pair, and how to tell the difference.*

**Eric Thomas · August 2026**

---

A couple of weeks ago I set out to do what looked like a mechanical
refactor across the community components registry: sweep 22 `_workspace`
components onto the canonical `workspace: <VendorResource>` pattern that
`dagster-databricks`, `dagster-snowflake`, `dagster-fivetran`, and
`dagster-powerbi` all use. Same YAML shape as the official integrations,
easier for customers to reach for, done.

I got through six vendors before it started feeling wrong. I stopped,
reread my own code, and noticed something uncomfortable: **most of these
weren't workspaces at all**. They were ingestion components in workspace
clothing.

This post is the anatomy of that mistake, the diagnostic I settled on for
telling apart the two shapes, and the four rebuilt vendor stories that
came out of it.

## What a workspace actually is

The workspace pattern, as the official Dagster integrations use it, is
opinionated. A single YAML declaration:

```yaml
type: dagster_databricks.DatabricksWorkspaceComponent
attributes:
  workspace:
    host: "{{ env.DATABRICKS_HOST }}"
    token: "{{ env.DATABRICKS_TOKEN }}"
  databricks_filter:
    include_jobs:
      job_ids: [42, 137]
```

That single component:

1. **Enumerates** every job in the workspace via the Jobs API.
2. **Filters** to what the user asked for.
3. **Emits one Dagster asset per job**.
4. **On materialize**, actually *triggers the job on Databricks*, polls
   to terminal state, and surfaces the run as a Dagster materialization.

Snowflake does the same for tasks, pipes, and dynamic tables. Fivetran
does it for connectors. PowerBI does it for semantic models and
refreshes. These aren't just SDK wrappers — they *are the orchestration
surface for that vendor*, expressed in Dagster's asset model.

The key word is **orchestration**. Materializing an asset makes the
vendor *do work*.

## The mistake I found

I looked at what the community registry called `github_workspace`. It
opened a session against `api.github.com`, listed repos × workflows, and
emitted one asset per (repo, workflow). Materializing the asset called
`GET /repos/{owner}/{name}/actions/workflows/{id}/runs` — the LIST
endpoint — and returned the most recent runs as a DataFrame.

That's not orchestration. GitHub Actions doesn't have a "trigger this
workflow from Dagster" story that fits an asset. It has
`POST /actions/workflows/{id}/dispatches` — fire and forget, no return
value — but our component wasn't calling that. It was just polling and
reshaping the response.

I looked at `notion_workspace`. It enumerated databases via the Search
API and emitted one asset per database. Materializing the asset queried
the database and returned rows as a DataFrame. That's ingestion.

I looked at `jira_workspace`. Enumeration + read. `pagerduty_workspace`.
Enumeration + read. `stripe_workspace`. Enumeration + read.

**Sixteen of the twenty-two `_workspace` components in the registry were
performing multi-endpoint ingestion under a name that promised
orchestration.** They emitted materializations, but the materializations
didn't cause the vendor to do anything. The names were lying.

There was already a dedicated `notion_ingestion` component. And
`github_ingestion`. And `jira_ingestion`. And so on. The `_workspace`
versions were duplicating them with different filter semantics and a
worse name.

## The diagnostic

I need a single-question test I can apply to any candidate `_workspace`
component. Here it is:

> **Does materializing an asset make the vendor do work?**

If the answer is *yes* — Snowflake runs a task, Databricks triggers a
job, PowerBI refreshes a semantic model, Fivetran runs a sync — then the
component is genuinely orchestrating something. Workspace-shaped is
right.

If the answer is *no* — the materialization just reads state, or lists
things, or maps API responses to a DataFrame — then the component is
ingestion. Calling it a workspace is a lie in the name.

Applying this to the 22 community `_workspace` components:

| Bucket | Vendors | Verdict |
|---|---|---|
| **A — real orchestration** (8) | snowflake, mlflow, qlik_replicate, qlik_compose, tm1, jde_orchestrator, cognos, fabric | Keep as `_workspace`. Materialize = vendor starts a task / refresh / run. |
| **B — dlt-like enumeration** (16) | notion, github, jira, salesforce, hubspot, shopify, stripe, pagerduty, wandb, facebook_ads, linkedin_ads, airtable, google_analytics, google_sheets, servicenow, doris | Reshape. Materialize just reads state. |

The eight in Bucket A stay. They earned their name. The sixteen in
Bucket B need a different shape entirely.

## The alternative: resource + sink

Here's the shape I landed on for Bucket B. For a SaaS API with no
orchestration primitive, you don't want *one* fat component that
pretends to enumerate everything. You want a **rich resource** the
customer can call from their own assets, plus **purpose-built sinks**
for the common "compute-something-in-Dagster-and-push-it-to-the-vendor"
patterns.

```
<vendor>_resource         # 15–20 read + write convenience methods
<vendor>_ingestion        # dlt-backed bulk read into a warehouse (usually exists)
<vendor>_<x>_sync         # asset — "vendor mirrors my DataFrame" upsert
<vendor>_<event>_sensor   # sensor — if the vendor has pollable events
# no <vendor>_workspace
```

The `_ingestion` component often already existed — the reshape didn't
touch it. The `_sensor` often already existed too. The new work was
(a) beefing up the resource from a `get_client()` stub to something you
can actually call from an asset, and (b) shipping the specific write-back
sinks that account for 90% of what people want to do to a SaaS from
Dagster: **upsert a DataFrame of computed rows back into the vendor's
data model.**

## Four rebuilds, four stories

Same pattern, four vendors, four different flavors of "the API is
special." I'll speed through the highlights.

### Notion — 19 methods, 2 sinks, one API redesign

Notion's `_resource` went from 5 lines wrapping `notion-client.Client`
to 19 methods spanning search, page CRUD, database CRUD, block trees,
comments, users, and file uploads. Every method exposes the same client
under the hood, so the escape hatch is one call away:

```python
def query_database(self, database_id=None, data_source_id=None, ...):
    ds_id = self._resolve_data_source_id(database_id, data_source_id)
    return self.get_client().data_sources.query(data_source_id=ds_id, ...)
```

That `_resolve_data_source_id` helper hides Notion's 2025 API
redesign, which split `database` (container) from `data_source`
(queryable schema). Old integrations that call
`databases.query(database_id=...)` broke at some point in
`notion-client v3`. The community resource swallows the split so
customers don't need to know.

Two sinks:
- `NotionDatabaseUpsertComponent` — DataFrame rows into a Notion
  database, keyed by any property, type-aware serialization from the
  DB schema. Optional `delete_missing: true` archives rows not present
  upstream.
- `NotionPageSyncComponent` — a specific Notion page's properties
  patched from a DataFrame's first row. Optional markdown body replace.
  Common shape: nightly rollup → sync into a fixed KPI dashboard page.

Full demo lives at [`examples/notion.md`](../examples/notion.md). Three
back-to-back runs, zero duplicates.

### GitHub — 21 methods, keyed by body-embedded HTML comment

GitHub is the CRUD-heaviest of the four. The resource covers issues,
PRs, workflows, releases, comments, dispatches — 21 methods, all raw
HTTP (I dropped `dagster-github`'s dependency; the PAT-based path is
90% of what people want, and the App-auth path still exists via the
official integration alongside).

The interesting problem is the sink's keying. GitHub issues don't have
a first-class "external ID" field. If you match rows to existing issues
by title, you'll create a duplicate every time someone in your team
edits the title in the GitHub UI. Bad.

The fix is a hidden marker in the issue body:

```
<!-- dagster-key: INC-1001 -->

The actual issue description text.
```

`GitHubIssueUpsertComponent` scans existing issues for that comment
prefix on every run and matches by the extracted key. HTML comments
don't render in the GitHub UI. Titles can be edited freely.

Full demo at [`examples/github.md`](../examples/github.md). Three
back-to-back runs, all `0 created, 5 updated`.

### Jira — 16 methods, JQL-server-side keying, one API retirement

Jira's `_resource` also drops its previous dependency (the `jira`
Python package) in favor of raw HTTP over Atlassian's REST v3. Two
Jira-specific complications land in the resource:

**1. Atlassian retired `GET /rest/api/3/search` in 2025.** Old
integrations that still call it get 410 Gone. The replacement is
`POST /rest/api/3/search/jql` with a JSON body and token-based
pagination (via `nextPageToken`, not offset). My first run of the
demo blew up on this. The resource's `iter_search_issues` now uses
the new endpoint transparently.

**2. Descriptions and comments use Atlassian Document Format (ADF).**
It's a JSON tree; you can't just send `"description": "hello"` to
the create-issue endpoint. The resource wraps plain strings into the
minimum-viable ADF document (`{type: doc, version: 1, content: [...]}`)
so callers can pass plain markdown.

For keying, Jira has a nicer answer than GitHub: labels are
first-class and JQL can filter on them server-side. Every synced issue
gets a `dagsterkey-<value>` label. Matching is one JQL call:

```
project = "SCRATCH" AND labels = "dagsterkey-INC-1001"
```

Full demo at [`examples/jira.md`](../examples/jira.md). Two
back-to-back runs, both `0 created, 5 updated`.

### PagerDuty — dedup built into the platform

PagerDuty is the most opinionated of the four about deduplication.
Every incident has an optional `incident_key` field, and PagerDuty's
server *rejects* duplicate keys on open incidents natively — POST an
incident with a key that already matches an open one, and it returns
the existing incident instead of creating a new one. The dedup is
built in.

`PagerDutyIncidentUpsertComponent` writes `dagster-{value}` into
`incident_key` and lets the server enforce uniqueness. The client-side
existing-issues check still happens (to know whether to update or
create), but it's a safety net, not the primary dedup mechanism.

One nuance: PagerDuty's `incident_key` uniqueness applies to open
(triggered/acknowledged) incidents only. If your demo row has a
`resolved` state and matches a previously-resolved incident, POSTing
again WILL create a duplicate. The sink handles this by querying
`statuses=["triggered", "acknowledged", "resolved"]` in its pre-flight
check — matches against ALL states, only creates on true misses.

The resource also exposes PagerDuty's Events API v2 (`send_alert` +
`resolve_alert`), which is the API dedicated to programmatic alerting
from monitoring systems. That's a different auth (routing key) and a
different endpoint (`events.pagerduty.com/v2/enqueue`), but conceptually
belongs on the same resource.

Full demo at [`examples/pagerduty.md`](../examples/pagerduty.md). Two
back-to-back runs, both `0 created, 5 updated`.

## Gotchas that only show up during live-validation

Every one of these demos passed `dg check defs`. Every one of them
imported cleanly. Every one of them ran their first materialize green.
And every one of them had a bug that only came out on the second
back-to-back run.

**GitHub's issue list endpoint is eventually consistent.** After
`POST /repos/{owner}/{name}/issues` returns 201, the new issue doesn't
immediately show up in `GET /repos/{owner}/{name}/issues`. The lag is
5–10 seconds. My demo scheduled the second run three seconds after
the first — two brand-new issues weren't visible yet, and the sink
duped them. Fix: (a) in-run cache — after each `create_issue`, add
the new issue to `existing_by_key` so subsequent iterations within
the same run don't re-create; (b) 10-second sleep between the two
demo runs. Real pipelines run minutes apart; the demo is the only
place this bites.

**Atlassian's API retirement wasn't quiet-but-not-fatal.** The old
`GET /search` endpoint returns 410 Gone with an error message
pointing at the new endpoint. That's honest. But if I hadn't
live-validated, the resource would have shipped in a state that fails
against every existing Jira Cloud instance.

**Notion's 2025 database → data_source split is silent.** The
old `databases.query(database_id=...)` isn't there anymore — you get
an AttributeError, not a 410. And the new endpoint requires a
`data_source_id`, which you get by first calling
`databases.retrieve(database_id=...)` to look up the primary data
source in `db["data_sources"]`. My initial resource skipped the
retrieve step and querying failed with `"Available: []"`.

**PagerDuty only dedups open incidents.** The default sink query
excluded resolved incidents. Run 2 saw 3 open incidents from run 1
and duped the 2 resolved ones (INC-1003 and INC-1005 from the demo
data). Symptom: `2 created, 3 updated` instead of `0 created, 5
updated`. Fix: query all three statuses.

And one that has nothing to do with the vendor APIs:

**Shipping a walkthrough `.md` isn't enough to make it discoverable.**
The Vercel-deployed docs at [dagster-component-ui.vercel.app][ui] have
a "CLI demos using this template" section on every component page.
That section reads `examples/README.md` in the CLI repo and filters
by exact template ID strings in the `Components` column of the tables
there. If you ship a walkthrough but don't add a row indexing it,
the component page says: *"No indexed demos list this template by id.
Try searching the examples index."* Even though the walkthrough
exists.

## What stays as a `_workspace`

Bucket A is still a real thing. The workspace shape is right when
you're wrapping a genuine orchestration surface. The examples that
still deserve the name after this pass:

- **`snowflake_workspace`** — tasks, pipes, dynamic tables, stored
  procedures. Materializing an asset runs the object.
- **`mlflow_workspace`** — model runs, experiment tracking, registry
  transitions.
- **`qlik_replicate`** — start/stop/resume replication tasks.
- **`qlik_compose`** — trigger DW-automation workflow runs.
- **`tm1`** — process execution and cube ops.
- **`jde_orchestrator`** — trigger JDE orchestrations.
- **`cognos`** — trigger report runs.
- **`fabric`** — trigger pipeline runs and lakehouse ops.

Notice the shape of that list — every one of them has an API endpoint
whose name is a verb: **run**, **trigger**, **start**, **execute**.
That's the diagnostic in action. If the vendor's docs describe an
endpoint like `POST /jobs/{id}/run`, workspace-shaped is correct. If
the vendor's most action-verby endpoint is `POST /issues` (i.e., you're
just adding a thing to their data model), you're doing ingestion or
write-back, not orchestration.

## Why this matters

Names in a public component registry are contracts. When a customer
searches for `github_workspace` and finds a thing that lists workflows
without triggering them, they either give up on us or hand-build the
integration they actually needed. Neither is fine.

The reshape is honest about what each API can do. It also, incidentally,
makes Dagster look like a much better fit for the *write-back* half of
your data platform than most people realize. Dagster is usually pitched
as an ELT orchestrator — the L in ELT. But some of the highest-leverage
pipelines are the ones that push *computed* data back into your ops
tools: your KPI dashboard in Notion, your incidents in PagerDuty, the
Jira tickets your DQ agent files. Four fresh sinks say Dagster is
comfortable in that role, and they all use the same YAML shape.

There are twelve more Bucket B vendors to work through: hubspot,
stripe, airtable, shopify, salesforce, servicenow, google_sheets,
google_analytics, facebook_ads, linkedin_ads, wandb, doris. Each one
will have its own version of the "GitHub eventual consistency" or
"Notion 2025 redesign" story — that's just what integration work is.

I'll ship them as they get built. The pattern is now the pattern.

---

**Read next:**

- [`notion.md`](../examples/notion.md) — DataFrame → Notion DB upsert + KPI page sync
- [`github.md`](../examples/github.md) — DataFrame → GitHub Issues upsert
- [`jira.md`](../examples/jira.md) — DataFrame → Jira Issues upsert
- [`pagerduty.md`](../examples/pagerduty.md) — DataFrame → PagerDuty incidents upsert
- The full [community components registry][ui] — ~960 components, 619 live-validated.

[ui]: https://dagster-component-ui.vercel.app/
