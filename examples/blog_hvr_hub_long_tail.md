# Orchestration for the HVR long tail — bringing standalone HVR Hub into the Dagster catalog

*By the Dagster community components team*

If you work in data at a large enterprise, there's a decent chance a
critical slice of your CDC estate runs on standalone **HVR Hub** — the
same product Fivetran acquired in 2021, but installed on your own
hardware, licensed the way it was before the acquisition, and
comfortably chugging along replicating from Oracle and Db2 mainframes
into your warehouse.

If that's you: this post is about you. And it's about a new community
component — `hvr_hub_workspace` — that puts your HVR estate into the
Dagster catalog with one YAML file, without asking you to migrate
anything.

## The situation

HVR (High Volume Replicator) has been a workhorse of enterprise
real-time replication for over a decade. Log-based CDC from Oracle,
Db2, SAP HANA, MSSQL, PostgreSQL — into Snowflake, BigQuery, Databricks,
Kafka. Sub-second latency, multi-source consolidation,
transformations-in-flight. In sectors where downtime is measured in
regulatory fines and moving data means moving it *now*, HVR earned its
license fee many times over.

Fivetran acquired HVR in 2021 and has been steadily integrating the
capability into the Fivetran SaaS platform (now sold as the
"Fivetran HVR" or Enterprise-tier CDC option). New customers with
Fivetran subscriptions get the Fivetran-managed dashboard experience.

But a lot of the HVR install base isn't new. Many enterprises adopted
HVR pre-acquisition — often 5-10 years ago — set it up on their own
infrastructure, spent a quarter tuning it against their Oracle
LogMiner + Db2 log reader + SAP HANA XSA setup, and haven't touched it
since because *it works*. There's a `.hvr` config directory, an
`hvrhubserver` process running on a dedicated VM, a Postgres or Oracle
repo database holding channel definitions, and an integrate job that's
been streaming for years.

These customers are still on **standalone HVR Hub**. Not
Fivetran-platform HVR. Different install, different UI, different REST
API, different Dagster surface.

## The gap for orchestration

Any modern data team wants their CDC replicated tables in the same
catalog as their dbt models, their warehouse assets, their BI extracts,
their ML feature tables. Unified lineage. Unified freshness telemetry.
Automation conditions that fire only when the source data is caught up.

Dagster ships an official [`dagster-fivetran`](https://docs.dagster.io/integrations/fivetran)
integration that does exactly this — for Fivetran-platform connectors,
including Fivetran-platform-managed HVR. Auto-discovers every
connector, emits external assets, ties into scheduling, surfaces sync
status.

But `dagster-fivetran` calls the Fivetran SaaS API. It cannot reach
your on-prem HVR Hub. Different API entirely: channels vs connectors,
bearer JWT vs API key, hub-scoped URLs vs account-scoped, no
`fivetran.com/v1/account/*` path in sight. The two products share
DNA — one is the descendant of the other — but the Dagster surface
built for one doesn't fit the other.

**That's the gap this component fills.**

## The component: `hvr_hub_workspace`

One YAML wires the whole Hub. Every replicated table shows up as a
Dagster asset. Optional polling sensor emits integrate-lag
observations. Optional per-asset check fails when lag breaches your SLA.

Full [Fivetran-shape](https://docs.dagster.io/integrations/fivetran) —
same `workspace:` block, same `channel_selector:` filter, same
`translation:` callable hook, same `polling_sensor` opt-in, same
`StateBackedComponent` discovery caching. If you've used
`dagster-fivetran` or `dagster-databricks`, the shape is instantly
familiar:

```yaml
type: dagster_community_components.HvrHubWorkspaceComponent
attributes:
  workspace:
    hub_url:  "{{ env.HVR_HUB_URL }}"
    hub_name: "{{ env.HVR_HUB_NAME }}"
    username: "{{ env.HVR_USERNAME }}"
    password: "{{ env.HVR_PASSWORD }}"

  channel_selector:
    by_pattern: [sales_*, orders_*]
    exclude_by_pattern: [*_test]

  polling_sensor: true
  freshness_lag_threshold_seconds: 900   # 15-minute SLA
```

That's the whole configuration. Every channel in your Hub, every
replicated table under it, every target location — auto-discovered.

## What good looks like

Once the component's live, your HVR-replicated tables are first-class
citizens in your data platform. The Dagster asset graph shows the
lineage from HVR channel → warehouse table. dbt models declared
downstream get automatic upstream dependencies. Automation conditions
can gate downstream materialization on HVR integrate lag being under
threshold. Alerts fire when replication lag breaches SLA — not when
someone notices the dashboard is stale.

The polling sensor writes an observation event every 5 minutes with
`integrate_lag_seconds` metadata. That number lives in each asset's
history and becomes visible in Dagster+ Insights as a time series.
You can chart integrate lag by channel, alert on trending regression,
correlate with load spikes downstream.

If you want the Fivetran-style "click asset → refresh runs" experience,
flip `action: refresh` and every asset becomes materializable —
triggers `POST /channels/{c}/refresh` on the Hub, polls to completion,
returns a materialization result. Handy for the periodic bulk-refresh
pattern that HVR shops often layer on top of continuous CDC.

## What we're NOT saying

We're not saying you should migrate off HVR to Fivetran. We're not
saying HVR is legacy. We're not saying anything about Fivetran's
roadmap.

We're saying: **standalone HVR Hub is a real product in production at
serious scale, and Dagster's community components meet it where it
lives.** The team maintaining HVR at your organization shouldn't have
to choose between "keep the replication that works" and "get their
tables into the modern data platform." Both, please. Now.

## Getting started

```bash
bash setup_hvr_hub_workspace_demo.sh
```

That spins up a mock HVR Hub (no license, no docker, no Fivetran
Support ticket), scaffolds a Dagster project, installs the component,
and prints your first HVR catalog view — takes about 30 seconds.

Then swap 4 environment variables to point at your real Hub:

```bash
export HVR_HUB_URL=https://your-hub.corp.internal:4340
export HVR_HUB_NAME=prod_hub
export HVR_USERNAME=hvradmin
export HVR_PASSWORD='<your-password>'
```

Rerun `uv run dg utils refresh-defs-state`. Your replicated tables are
now in Dagster.

Full walkthrough: [`examples/hvr_hub_workspace.md`](hvr_hub_workspace.md).
Component reference: [`integrations/hvr_hub_workspace/README.md`](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/integrations/hvr_hub_workspace/README.md).

## The broader shape

`hvr_hub_workspace` isn't a one-off. It's part of a pattern the Dagster
community components project uses for the *long tail of enterprise
integrations that don't get official packages*:

- **`qlik_replicate_workspace`** — same shape for Qlik Replicate (also
  a CDC platform, also Fivetran-adjacent, also has a large standalone
  install base)
- **`ab_initio_job_sensor`** — Ab Initio job status into Dagster
- **`db2_iseries_resource`** — Db2 for i (AS/400) resource + reader
- **`cognos_workspace`** — IBM Cognos catalog into Dagster
- **`sap_s4hana` / `oracle_ebs` / `dynamics_365`** patterns

Whenever a real product runs at real customers and there's no official
Dagster integration, the community components project is where the
gap closes. `dagster-community-components-cli` on PyPI; browse the
[registry](https://dagster-component-ui.vercel.app/) or run
`dagster-component search <keyword>` to find what's there.

If HVR is your gap, it isn't anymore.

## Roadmap

Companion components we'll ship on customer request:

- **`hvr_channel_refresh`** — dedicated single-channel bulk-refresh trigger for scheduling
- **`hvr_definition_snapshot`** — versioned channel-definition asset for drift detection
- **`hvr_alert_sensor`** — poll HVR Alert Interface → emit Dagster asset failures

If you're on standalone HVR Hub and one of these matches a real
workflow you'd wire up: [open an issue](https://github.com/eric-thomas-dagster/dagster-community-components-cli/issues)
or reach out. We build against real customer shapes, not speculation.

---

*Community components are unofficial. They're maintained by the
community, live at `dagster-community-components` on PyPI, and don't
carry the same support guarantees as Dagster's official integrations.
That's the tradeoff — official integrations get the long tail of
enterprise-tier support; community components get the long tail of
integrations that don't have an official maintainer yet. Both matter.*
