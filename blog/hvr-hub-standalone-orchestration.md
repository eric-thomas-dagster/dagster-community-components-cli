---
title: "Orchestration for the HVR long tail"
date: 2026-08-19
author: Eric Thomas
description: "Standalone HVR Hub is still in production at large enterprises that adopted it pre-Fivetran-acquisition. The new hvr_hub_workspace community component brings that install base into the Dagster catalog with one YAML — full Fivetran-shape, no migration required."
---

# Orchestration for the HVR long tail

*Standalone HVR Hub is still in production at large enterprises that
adopted it pre-Fivetran-acquisition. The new `hvr_hub_workspace`
community component brings that install base into the Dagster catalog
with one YAML — full Fivetran-shape, no migration required.*

**Eric Thomas · August 2026**

---

Every big-company data team eventually collects one: **a product that
got acquired, and the pre-acquisition install is still in production
because it works and nobody has budget to migrate.**

The vendor's new offering runs on their SaaS. Your version runs on a
VM in your data center. Both come from the same lineage, both have
overlapping docs, and neither of the ecosystem's official integrations
knows how to talk to yours — because "official" tools chase the SaaS
customer.

If you own real-time replication at a large enterprise, that product is
probably **HVR Hub** — the log-based CDC platform Fivetran acquired in
2021, still installed on-prem at hundreds of shops that bought it years
before the acquisition, still replicating from Oracle / Db2 / SAP HANA
to your warehouse on the same infrastructure it's been running on for
five to ten years.

This post is about a new community component — `hvr_hub_workspace` —
that gets those tables into the Dagster catalog with one YAML file, no
migration required.

## The situation

HVR (High Volume Replicator) has been a workhorse of enterprise
real-time replication for over a decade. Log-based CDC from Oracle,
Db2, SAP HANA, MSSQL, PostgreSQL — into Snowflake, BigQuery,
Databricks, Kafka. Sub-second latency. Multi-source consolidation.
Transformations in flight. In regulated sectors where downtime is
measured in fines and "moving data" means moving it *now*, HVR earned
its license fee many times over.

Fivetran acquired HVR in 2021 and has been integrating the capability
into the Fivetran SaaS platform (now sold as "Fivetran HVR" or the
Enterprise-tier CDC option). New customers get the Fivetran dashboard
experience.

The install base isn't new. A lot of enterprises adopted HVR five to
ten years ago — set it up on their own hardware, spent a quarter tuning
it against their Oracle LogMiner + Db2 log reader + SAP HANA XSA setup,
and haven't touched it since because *it works*. There's a `.hvr`
config directory, an `hvrhubserver` process on a dedicated VM, a
Postgres or Oracle repo database holding channel definitions, and an
integrate job that's been streaming for years.

These customers are on **standalone HVR Hub**. Not Fivetran-platform
HVR. Different install, different UI, different REST API, different
authentication story, different Dagster surface.

## The gap

Modern data teams want their CDC-replicated tables in the same
catalog as their dbt models, their warehouse assets, their BI extracts,
their ML feature tables. Unified lineage. Freshness telemetry.
Automation conditions that fire only when upstream is caught up.

Dagster ships an official
[`dagster-fivetran`](https://docs.dagster.io/integrations/fivetran)
integration that does exactly this — for Fivetran-platform connectors,
including Fivetran-platform-managed HVR. Auto-discovers every
connector, emits external assets, wires in scheduling, surfaces sync
status.

But `dagster-fivetran` calls the Fivetran SaaS API. It cannot reach
your on-prem HVR Hub. Entirely different API surface: channels vs
connectors, hub-scoped URLs vs `fivetran.com/v1/account/*`, bearer JWT
against your Hub vs API key against Fivetran's cloud. The two products
share DNA — one is the descendant of the other — but the Dagster
surface built for one doesn't fit the other.

**Before this component, your two options were:**

1. **Hand-roll one `@dg.asset` per replicated table**, plus a sensor
   that polls HVR's REST API for lag, plus custom code to translate
   channel definitions into asset keys, plus a way to keep all of that
   in sync when someone renames a channel. Multiple days of work, and
   the first schema change breaks it.
2. **Declare 200 `external_asset` definitions with no live sync
   status.** Fast to set up, useless for anything except lineage
   diagrams. No automation-condition can gate on staleness because
   there's nothing observing HVR.

`hvr_hub_workspace` is option three: **one YAML file, full sync
status, works today.**

## The component in one YAML

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

That's the whole configuration. On first load, the component calls the
HVR Hub REST API (`GET /hubs/{hub}/channels`, `GET /channels/{c}/tables`,
`GET /locations`), caches the results as a
`StateBackedComponent` (a Dagster pattern for expensive discovery calls
that shouldn't repeat on every run), and emits **one Dagster asset per replicated table** — grouped by
channel, tagged with source + target location, with source-table lineage
attached where the channel definition exposes it.

The shape is deliberately **full Fivetran-shape** — same `workspace:`
block, same `channel_selector:` filter, same `translation:` callable
hook, same `polling_sensor` opt-in, same state caching. If you've
worked with `dagster-fivetran` or `dagster-databricks`, the
`hvr_hub_workspace` YAML is instantly familiar.

## What it looks like in `dg dev`

Run `dg dev` after installation and the Dagster UI shows:

```
                     ┌───────────────────────────────┐
                     │  hvr_hub_workspace (top-level)│
                     └───────────────┬───────────────┘
                                     │
     ┌───────────────────┬───────────┴───────────┬──────────────────┐
     │                   │                       │                  │
┌────▼────┐        ┌────▼────┐             ┌────▼────┐        ┌────▼────┐
│ orders  │        │ orders_ │             │ sales_  │        │ sales_  │
│ _header │        │ _items  │             │ leads   │        │ opps    │
└─────────┘        └─────────┘             └─────────┘        └─────────┘
  channel:           channel:                channel:            channel:
  orders             orders                  sales               sales
  target:            target:                 target:             target:
  SNOWFLAKE.RAW      SNOWFLAKE.RAW           SNOWFLAKE.RAW       SNOWFLAKE.RAW
```

Each asset is materializable if you set `action: refresh` (triggers
`POST /channels/{c}/refresh` on the Hub and polls to completion), or
observation-only otherwise. Downstream dbt models declared in the same
project pick up the lineage automatically — dbt sources named after
the HVR-replicated tables become downstream of the HVR assets, no
manual `deps:` needed.

## Ops changes: lag as a first-class observation

The polling sensor writes an `AssetObservation` every 5 minutes per
channel with `integrate_lag_seconds` metadata. That number lives in
each asset's history and, in Dagster+, becomes a time series in
Insights:

- Chart integrate lag by channel.
- Alert on trending regression, not just point-in-time breach.
- Correlate replication lag with load spikes downstream.
- Fail an asset check when lag breaches SLA — surfaces in the checks
  panel like any other data-quality gate.
- Gate downstream materialization with `AutomationCondition` that
  requires upstream `integrate_lag_seconds < N`.

Concretely: dbt runs no longer fire against half-fresh replicated
tables. Feature stores don't publish stale features. Alerts arrive
when replication lag rises for the third consecutive interval, not when
someone opens a dashboard and notices the number is old.

## Deployment shape

Nothing about the component moves data. HVR keeps doing what HVR does;
Dagster observes and (optionally) triggers refreshes over REST:

```
  ┌─────────────────────┐                          ┌───────────────────────┐
  │  Sources            │                          │  Warehouse            │
  │  Oracle / Db2 /     │  ═══ HVR log-based ═══▶  │  Snowflake / BigQuery │
  │  SAP HANA / MSSQL   │        replication       │  / Databricks         │
  └─────────────────────┘                          └───────────────────────┘
             │                                                 ▲
             │                                                 │
             ▼                                                 │
  ┌─────────────────────┐                                      │
  │  HVR Hub (on-prem)  │                                      │
  │  hvrhubserver +     │◀───── REST poll ────────┐            │
  │  channels + repo DB │       (lag, refresh)    │            │
  └─────────────────────┘                         │            │
                                                  │            │
                                        ┌─────────┴────────────┴────┐
                                        │  Dagster                  │
                                        │  hvr_hub_workspace        │
                                        │  + asset graph            │
                                        │  + polling sensor         │
                                        │  + freshness checks       │
                                        └───────────────────────────┘
```

The data plane is HVR. The control plane is Dagster. Neither knows the
other exists at runtime except through the REST endpoints the component
calls.

## What we're NOT saying

Not that you should migrate off HVR to Fivetran. Not that HVR is
legacy. Not anything about Fivetran's roadmap.

The point is: **standalone HVR Hub is a real product in production at
serious scale, and the community components project meets it where it
lives.** The team maintaining HVR at your organization shouldn't have
to choose between "keep the replication that works" and "get their
tables into the modern data platform." Both, please. Now.

## Try it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_hvr_hub_workspace_demo.sh | bash
```

That spins up a mock HVR Hub (no license, no docker, no Fivetran
Support ticket), scaffolds a Dagster project, installs the component,
and prints your first HVR catalog view in about 30 seconds.

Then swap four env vars to point at your real Hub:

```bash
export HVR_HUB_URL=https://your-hub.corp.internal:4340
export HVR_HUB_NAME=prod_hub
export HVR_USERNAME=hvradmin
export HVR_PASSWORD='<your-password>'
uv run dg utils refresh-defs-state
```

Your replicated tables are now Dagster assets.

- **Walkthrough:** [`examples/hvr_hub_workspace.md`](https://dagster-component-ui.vercel.app/examples/hvr_hub_workspace) — setup script, per-knob behavior, custom-translation example, version-compatibility notes.
- **Component reference:** [`hvr_hub_workspace`](https://dagster-component-ui.vercel.app/c/hvr_hub_workspace) — full field-by-field schema, tags, dependencies, validation status.

## The broader shape

`hvr_hub_workspace` isn't a one-off. It's the same pattern the
community components project uses across the **long tail of enterprise
integrations that don't get official packages**:

- **`qlik_replicate_workspace`** — same shape for Qlik Replicate (also
  a CDC platform, also Fivetran-adjacent, also has a large standalone
  install base)
- **`abinitio_run_asset`** — Ab Initio job status into Dagster
- **`db2_iseries_resource`** — Db2 for i (AS/400) resource + reader
- **`cognos_workspace`** — IBM Cognos catalog into Dagster
- **`sap_s4hana` / `oracle_ebs` / `dynamics_365`** patterns

Whenever a real product runs at real customers and there's no official
Dagster integration, the community components project is where the gap
closes. Browse the [registry](https://dagster-component-ui.vercel.app/)
or run `dagster-component search <keyword>` to find what's there.

**If HVR was your gap, it isn't anymore.**

---

*Community components are unofficial. They live at
`dagster-community-components` on PyPI, are maintained outside the
Dagster team, and don't carry the same support guarantees as Dagster's
official integrations. That's the tradeoff: official integrations get
enterprise-tier support; community components get the long tail of
integrations that don't have an official maintainer yet. Both matter.*
