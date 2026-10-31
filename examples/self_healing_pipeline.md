# Self-Healing Pipeline — real failure, real diagnosis, real retry
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Components:** `dagster_instance_tools` (read-only AI-agent tools over a real Dagster instance), plus a plain custom `@asset` pair (`flaky_ingest` / `pipeline_doctor`) for the glue logic.

**Script:** [`setup_self_healing_pipeline_demo.sh`](./setup_self_healing_pipeline_demo.sh)
**Cost:** $0 — no LLM call, no cloud account, pure local Dagster + Python.
**Validated:** 2026-10-31 — RUN_FAILURE on round 1 (real), `pipeline_doctor` correctly diagnosed it via `dagster_instance_tools` (real error class/message/failed step, straight from Dagster's event log), RUN_SUCCESS on round 2 after remediation.

## Why this exists

The "AI ops assistant" story: an agent answers "why did this fail?" with real Dagster data (`list_recent_failures`, `get_run_summary`, `get_run_logs`, `asset_status`, `asset_lineage`, `stale_assets`) instead of a human grepping logs. No demo in this catalog exercised `dagster_instance_tools` before this one.

```
flaky_ingest (fails until a marker file exists)
       │
       ├── [round 1] FAILS for real (RUN_FAILURE)
       │
pipeline_doctor — reads the real failure via dagster_instance_tools,
       │           prints a real diagnosis
       │
[remediation applied — marker file touched]
       │
       └── [round 2] flaky_ingest SUCCEEDS
```

## Three real things this demo found

1. **`run_failure_remediation` (the full production version of this idea) genuinely can't run end-to-end in a single `dg launch` script.** It's a pair of real Dagster sensors — a `run_failure_sensor` gathering evidence and requesting a triage-job run, and an approval sensor executing allow-listed actions once a token is present — that need a **live `dg dev`/daemon process** plus a **custom-authored triage job** (it proposes the fix; the component only gathers evidence and executes an approved one). That's real infrastructure, not something a one-shot script can stand up and tear down. This demo shows the real, verifiable building block underneath it — `dagster_instance_tools` — with the retry applied manually instead of through an automated, approved sensor action.
2. **A real bug in the registry**: `dagster_instance_tools`'s own `__init__.py` doesn't export `DagsterInstanceToolsComponent` (it's empty), so `dg check defs` fails immediately after a fresh `--auto-install`. Reported and fixed upstream in `dagster-component-templates`; this script also patches it defensively after install, since `--auto-install` pulls from the live repo and may not reflect the fix yet depending on when you run this.
3. **A real packaging bug**: `dagster_instance_tools/requirements.txt` had an inline `# comment` on the same line as its `mcp>=1.30,<3` dependency, which `uv add`/pip can't parse as a requirement spec (the error surfaces during `--auto-install`, though it's non-fatal — the component still installs). Also fixed upstream.
4. **A real API detail**: `dagster_instance_tools.call("list_recent_failures", ...)` returns results under the key `"runs"`, not `"failures"` — an easy one to get wrong (this demo's own first draft made exactly this mistake and silently reported "no failures found" right after printing the real failure one line above).

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Nothing else.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_self_healing_pipeline_demo.sh -o setup_self_healing_pipeline_demo.sh
chmod +x setup_self_healing_pipeline_demo.sh
./setup_self_healing_pipeline_demo.sh
```

## Validated run (2026-10-31)

```
Round 1: flaky_ingest — RUN_FAILURE (RuntimeError: UPSTREAM_CONFIG_MISSING...)
pipeline_doctor diagnosis: real run_id, error_class=RuntimeError, failed_steps=[flaky_ingest], full stack trace tail
Remediation applied (marker file touched)
Round 2: flaky_ingest — RUN_SUCCESS ("Ingest succeeded -- remediation was applied.")
```

## What this does and doesn't show

This demo proves `dagster_instance_tools`'s read-only diagnosis is real and works correctly against a genuine failure. It does **not** demo `run_failure_remediation`'s own sensor-automated evidence→proposal→approval→execution loop, which needs a live daemon and a triage job you write — see the "Production variant" section in the script's own output for the real next step.

## Production variant

Swap the script's manual "touch a marker file, re-launch" for the real thing: configure `run_failure_remediation` (sensors, not assets), author a triage job that reads evidence via `dagster_instance_tools` (as shown here) plus an LLM to propose a fix, and run `dg dev` so the sensors are live and ticking.

## See also

- [`dagster_instance_tools` README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/dagster_instance_tools/README.md)
- [`run_failure_remediation` README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/run_failure_remediation/README.md) — the full sensor-based production version.
- Browse the [walkthrough index](README.md) for related demos across every component family.
