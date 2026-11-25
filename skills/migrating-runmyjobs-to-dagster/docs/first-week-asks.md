# What to ask the customer for in the first week

Derived from a real job listing (570 job definitions; the listing has names, descriptions, applications and last-modified dates only). It sizes the estate, but conversion needs steps, parameters and schedules.

## Already known from the listing

- About 340 chains (`JC_`) and 230 definitions (`JD_`); 25 are system-generated wrapper chains (`System_Internal_Chain<n>`) that fold into their parent; 17 are notification templates (`Z_JD_*Email*`), which become alert policies, not jobs; 7 are reports.
- By name: roughly 108 dbt Cloud chains, 6 HVR-refresh definitions, many Python extract and dbt Core ("Linux process server") chains. These counts are approximate; run `runmyjobs-to-dagster inventory-html <report.html>` for the current numbers and a CSV.
- About 50 names look retired or are copies (`Decommissioned`, `DEPRECATED`, `OLD_`, `Archived_`, `(Copied at ...)`). Treat as candidates only; confirm with run history (the customer already said to drop anything not running).
- Reusable definitions the chains call: `JD_dbt_Cloud_Job_Trigger` (dbt Cloud API, optional polling), `JD_HVR_Refresh_CMD` and `JD_LDP_Refresh_CMD` (HVR channel refresh), `JD_Snowflake_dbt_Linux*` (dbt Core), `JD_aws_s3_*` (S3 transfer).

## Ask for (in this order)

1. **Output of the customer's own reports `OEVAL-Q2-Chain-Composition` and `OEVAL-Q3-parameters`** (they already exist in the listing). Q2 gives every step and the definition each call runs, with its type. Q3 gives dbt Cloud job ids, selectors, HVR channels, sleep durations. Together they replace a CAR export for chains. Same format as the listing (an HTML report) is fine; a new parser for these two is small once a sample exists.
2. **`OEVAL-Q1-tl-chains`** output: run volume, errors, runtime and schedule per top-level chain over 30 days. This is the cadence and "is it still running" evidence.
3. **More CAR exports like the three already checked** (a chain with its called definitions; ask for the dbt Cloud and HVR ones). Chains, call parameters, preconditions, handlers and embedded wrapper chains are verified against real exports, but **no export has had a submit frame, a time window definition, a Queue, or the real `JD_*` definitions**, so those shapes are still guesses. **One CAR export** of one chain with its definitions, for example `JC_Snowflake_SAP_BW_dbt_fi_dbtCloud` plus its HVR refresh chain. It settles the XML shapes the extractor still assumes (chains, submit frames, job groups).
4. **Submit frames** (schedules), time windows and **file events** (directory, file pattern, poll interval, minimum size or age, "file in use" check, and the chain each one starts) for the first wave, if Q1 does not carry them. File events are written into the model by hand until a real export shows their shape.
5. **Read access to the Inbound REST Extension**, if they want live extraction: see `docs/rmj-api-setup.md`.
6. **A dbt Cloud API token and account id** (read-only is enough). `runmyjobs-to-dagster fetch-dbt-jobs <account id>` lists the account's jobs; `convert --dbt-jobs` then turns each dbt Cloud step into the mirrored job asset. Loading and validating a project that uses the dbt component also needs the account id, project id, environment id and token in the environment. **The HVR hub details**, only when the build runs against real systems. Never put any of these in the repo.
