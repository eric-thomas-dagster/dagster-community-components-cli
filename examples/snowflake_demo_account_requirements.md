# Snowflake account requirements for the full Dagster demo

This doc captures everything the [Snowflake workspace demo](snowflake_workspace.md) (and its companion [environment seed](setup_snowflake_environment.sh)) can exercise, and what permissions / product tiers each capability needs.

It exists because **a sandboxed Snowflake account will hit walls the demo gracefully degrades around** — and you want your Snowflake partnership contact to know exactly what to grant if they want all features to light up live.

## Why this doc exists

The setup script runs **scaffold-time capability probes** against your account. Components your role + tier can actually materialize are scaffolded into the project. Capabilities you don't have access to are **silently skipped** — they don't end up as broken / red assets in `dg dev`.

When the script finishes, if any capabilities were skipped it prints a security-ask table and writes the same content to `<project>/SECURITY_ASK.md`. Send that file to your security / partnership contact and they'll know exactly what to grant. Re-run the script after the grants land — the missing components automatically scaffold into the project.

This means:

- **Your sandbox demo always runs end-to-end** (only includes what works in your account — every asset is green)
- **Your customer's account scaffolds more components** when they have higher privileges
- **The same script powers both cases** — no separate "demo for sandboxed SE" vs "demo for full ACCOUNTADMIN" paths

## Blocked capabilities — permissions

If your demo account has restricted roles (e.g. `SANDBOX_WRITER`-equivalent — a common SE situation), the following capabilities silently degrade unless your admin grants the listed privilege:

| Capability | What breaks without the grant | Privilege needed | Who can grant |
|---|---|---|---|
| **Register an RSA keypair on your user** (`ALTER USER … SET RSA_PUBLIC_KEY=…`) | Silently no-ops without `OWNERSHIP` on your user. Falls back to SSO. | `OWNERSHIP` on the user, OR have a `USERADMIN` run the `ALTER USER` once for you | `USERADMIN` |
| **Programmatic Access Tokens (PATs)** | `Programmatic access token failed authentication. No active network policy found …` | A network policy attached to the user (or to the user's role) | `SECURITYADMIN` |
| **`USE ROLE SYSADMIN`** | Required by the seed script's `CREATE DATABASE` step. The seed auto-detects this and switches to "sandbox mode" (creates schemas inside an existing DB you already own). | `SYSADMIN` granted to the user | `SECURITYADMIN` |
| **`CREATE DATABASE`** | Sandbox mode kicks in: seed creates schemas inside an existing DB instead. Demo still works at smaller scope. | `CREATE DATABASE` on the account | `SYSADMIN` / `ACCOUNTADMIN` |
| **`EXECUTE TASK`** | `ALTER TASK … RESUME` fails; tasks materialize as suspended. Click-to-materialize from Dagster still works (it calls `EXECUTE TASK <name>` on demand), but tasks won't auto-fire on their cron schedules. | `EXECUTE TASK` on the account | `ACCOUNTADMIN` |
| **`CREATE EXTERNAL VOLUME`** (for **Iceberg Tables**) | `snowflake_iceberg_table` materializes as "needs CREATE EXTERNAL VOLUME — see metadata". | `CREATE EXTERNAL VOLUME` on the account + an IAM role registered in AWS pointing at a bucket | `ACCOUNTADMIN` |
| **`CREATE NOTIFICATION INTEGRATION`** (for **Snowpipe auto-ingest**) | Auto-ingest from S3/GCS/Azure can't be wired. PUT-then-COPY pipes still work. | `CREATE NOTIFICATION INTEGRATION` on the account | `ACCOUNTADMIN` |
| **`CREATE CORTEX SEARCH SERVICE`** | `snowflake_cortex_search` materializes as "needs CREATE CORTEX SEARCH SERVICE — see metadata". | `CREATE CORTEX SEARCH SERVICE` on the target schema + `USAGE` on the warehouse | `SECURITYADMIN` (privilege grant) |
| **`CREATE OPENFLOW DEPLOYMENT`** + **`CREATE OPENFLOW RUNTIME`** | OpenFlow setup page in Snowsight is hard-blocked — shows only "Request access with your admin" buttons. | Both privileges granted; **plus** the BYOC deployment has to actually be provisioned (an EKS-in-your-cloud install — multi-hour to stand up) | `ACCOUNTADMIN` |
| **`CREATE RESOURCE MONITOR`** | Can't demo credit-usage observability orchestrated by Dagster. | `CREATE RESOURCE MONITOR` on the account | `ACCOUNTADMIN` |
| **Data Sharing (producer side)** | Can't show outbound shares as Dagster-orchestrated assets. | `CREATE SHARE` on the account + `IMPORT SHARE` on the database | `ACCOUNTADMIN` |

## Blocked capabilities — product tier / account parameters

These aren't permission issues — they're features your account doesn't have enabled:

| Capability | Why blocked | What's needed |
|---|---|---|
| **Materialized Views** | Account is on **Standard** edition | Upgrade to **Enterprise** edition (or higher) for MV support |
| **Hybrid Tables (Unistore)** | `ENABLE_UNISTORE_FEATURES = false` at account level | `ALTER ACCOUNT SET ENABLE_UNISTORE_FEATURES = TRUE` (Unistore is GA — no tier upgrade needed, just the parameter) |
| **Cortex LLM functions** in some regions | `SNOWFLAKE.CORTEX.COMPLETE` etc. only land in specific Snowflake regions | Either deploy the demo account in a Cortex-available region (most US AWS regions work today), OR enable cross-region inference: `ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'AWS_US'` |
| **Search Optimization Service** | Requires Enterprise edition | Upgrade to Enterprise edition |
| **Time Travel beyond 1 day** | Standard edition caps `DATA_RETENTION_TIME_IN_DAYS` at 1 day | Enterprise edition allows up to 90 days |
| **Replication / Failover (cross-region)** | Requires Business Critical edition | Upgrade to Business Critical |
| **Dynamic Data Masking + Row Access Policies** (governance demo) | Requires Enterprise edition | Upgrade to Enterprise edition |
| **Account Usage views** (`SNOWFLAKE.ACCOUNT_USAGE.*`) | Requires `SNOWFLAKE` database imported share granted to the role | `IMPORT SHARE` on `SNOWFLAKE` database — usually `ACCOUNTADMIN`-only |

## The single ask for a partnership contact

If you want one paragraph to paste into Slack / email / a ticket:

> Hey — I'm building a public Dagster + Snowflake demo against our partnership account. For the demo to exercise the full Snowflake surface (Iceberg, Cortex Search, Cortex LLM, Unistore / Hybrid Tables, Materialized Views, OpenFlow, Snowpipe auto-ingest, Resource Monitors), can you:
>
> 1. Move the demo database — or a dedicated `DAGSTER_DEMO` database — onto **Enterprise** edition (Business Critical if we want to show replication too)
> 2. Grant me `ACCOUNTADMIN` on that database scope, OR create a service role with: `CREATE EXTERNAL VOLUME`, `CREATE NOTIFICATION INTEGRATION`, `EXECUTE TASK`, `CREATE OPENFLOW DEPLOYMENT`, `CREATE OPENFLOW RUNTIME`, `CREATE CORTEX SEARCH SERVICE`, `CREATE RESOURCE MONITOR`, and `CREATE SHARE`
> 3. Enable account parameters: `ALTER ACCOUNT SET ENABLE_UNISTORE_FEATURES = TRUE`, and `ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'AWS_US'`
> 4. Attach a permissive network policy to my user so PATs / keypair JWT auth work from my laptop
> 5. Run `ALTER USER <me> SET RSA_PUBLIC_KEY = '<my pubkey>'` once so headless keypair auth works
>
> If you can also stand up an **OpenFlow runtime** (Snowflake-managed flavor is faster than the BYOC EKS path), that unblocks the OpenFlow integration demo too.

That single ask unlocks the full surface the Dagster demo covers.

## How the scaffold-time skip works

Capability probes run as part of the discovery step (after credential verify). For each probe, the script tries a non-destructive query against your account:

| Probe | What it checks |
|---|---|
| `SHOW EXTERNAL VOLUMES` | Iceberg add-on enabled iff ≥1 volume exists |
| `SHOW CORTEX SEARCH SERVICES IN ACCOUNT` | Cortex Search add-on enabled iff ≥1 service exists |
| `SELECT SNOWFLAKE.CORTEX.COMPLETE('mistral-7b', 'hi')` | Cortex LLM add-on enabled iff function responds |
| `SHOW MATERIALIZED VIEWS IN ACCOUNT LIMIT 1` | MV component in DDL showcase included iff query succeeds (Enterprise+) |
| `SHOW NOTIFICATION INTEGRATIONS` | Snowpipe uses AUTO_INGEST mode iff ≥1 integration exists; else PUT-COPY |
| `SHOW HYBRID TABLES IN ACCOUNT LIMIT 1` | Hybrid Tables capability noted (no component yet) |

Probe failures are non-fatal: if the privilege/feature isn't there, the script just sets a capability flag to `no` and moves on. Every probe is wrapped in `try/except` so a permission error on one capability doesn't blow up the rest of the scan.

### Resulting demo project

After scaffold completes, your project contains **only the components that can materialize**:

```
src/<pkg>/defs/
├── snowflake_workspace/           # always
├── regional_top_paid_pipeline/    # warehouse_pipeline (always)
├── snowpark_pipeline_demo/        # snowpark_pipeline (always)
├── snowflake_time_travel_asset/   # always (queries existing tables)
├── snowflake_cortex_asset/        # ⊘ skipped if Cortex LLM unavailable
├── snowflake_iceberg_table/       # ⊘ skipped if no EXTERNAL VOLUMES exist
├── snowflake_cortex_search/       # ⊘ skipped if no CORTEX SEARCH SERVICES
├── dg_materialized_view/          # ⊘ skipped from DDL showcase on Standard edition
└── ... (other always-on add-ons)
```

Every asset in the resulting `dg dev` graph is green. The skipped components live in the registry waiting for you to re-run the script once permissions/tier are unlocked.

### SECURITY_ASK.md output

When at least one capability is skipped, the script also writes a `SECURITY_ASK.md` file inside the project containing a table formatted exactly like the [permissions table above](#blocked-capabilities--permissions), but scoped to **the specific capabilities your account couldn't reach** — not the full universe. Send that file to your security team and they know precisely what's needed for this customer's demo to add the missing pieces.

## See also

- [Snowflake workspace walkthrough](snowflake_workspace.md) — the full walkthrough with auth options + add-on details
- [`setup_snowflake_environment.sh`](setup_snowflake_environment.sh) — the seed script (sandbox-mode aware)
- [`setup_snowflake_workspace_demo.sh`](setup_snowflake_workspace_demo.sh) — the workspace + add-on scaffolder
