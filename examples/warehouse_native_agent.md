# Warehouse-Native Agent — judged and approved before anything acts on it

**Components:** `snowflake_cortex_agent`, `llm_judge`, `human_approval_gate` — all existing, composed via YAML + one small adapter asset.

**Script:** [`setup_warehouse_native_agent_demo.sh`](./setup_warehouse_native_agent_demo.sh)
**Cost:** Snowflake Cortex usage (varies by account) + ~$0.001 for one `llm_judge` call.
**Validated:** 2026-10-31 — `dg check defs` passes with no credentials (structurally valid config). The downstream `llm_judge` → `human_approval_gate` mechanics were live-verified end-to-end in a separate harness using a stand-in answer (the exact shape `snowflake_cortex_agent`'s own output takes): a real two-phase `dg launch` — once with no approval token (`answer_approved` materializes, `approved` check fails with `status=approval_pending`), once after dropping a token (`approved` check passes, logs `Approved by <approver>: <reason>`). **The `snowflake_cortex_agent` step itself was not live-tested against a real Snowflake account** — this session had no Snowflake credentials available. See "Honesty note" below.

## Why this exists

A warehouse-native NL→SQL agent (Snowflake Cortex COMPLETE, here) is powerful but you don't want it silently acting on a wrong query or a bad answer. This demo adds an automated judge *and* a human checkpoint before anything downstream consumes the result.

```
warehouse_answer (snowflake_cortex_agent)
       │  (dict output: final_answer, iterations, tool_calls_made, ...)
       ▼
warehouse_answer_df (adapter asset — reads the materialized dict back as a 1-row DataFrame)
       │
       ▼
answer_sanity_check (llm_judge)
       │
       ▼
answer_approved (human_approval_gate — blocks until a JSON token is dropped)
```

## Honesty note: no demo mode on `snowflake_cortex_agent`

Unlike most of this registry, `snowflake_cortex_agent` has **no `demo_mode` and no local/offline path** — confirmed by reading its `component.py` directly. It calls the real Snowflake Cortex REST API via `account_url_env_var`/`auth_token_env_var`. Without real credentials, `dg check defs` still passes (the YAML is structurally valid), but `dg launch` will fail at the `warehouse_answer` step. The script detects missing credentials up front and skips straight to the structural-validation-only path rather than pretending to succeed.

`databricks_genie_query` was the other candidate for this demo's first step — not used here because it additionally requires a pre-created "Genie space" (a UUID you create via the Databricks UI first), an even higher setup bar than a Snowflake account + PAT.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `OPENAI_API_KEY` — for the `llm_judge` step.
- `SNOWFLAKE_ACCOUNT_URL` + `SNOWFLAKE_PAT` — a real Snowflake account with Cortex enabled. Without these, the script still scaffolds and validates the project structurally, but can't run the full chain.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_warehouse_native_agent_demo.sh -o setup_warehouse_native_agent_demo.sh
chmod +x setup_warehouse_native_agent_demo.sh
./setup_warehouse_native_agent_demo.sh
```

## What you'll see (with real Snowflake credentials)

1. `warehouse_answer` — the agent answers your prompt against Cortex COMPLETE.
2. `answer_sanity_check` — `llm_judge` scores the answer's accuracy/clarity/completeness.
3. `answer_approved` — materializes immediately with `status=approval_pending`; the `approved` check fails until a token is dropped.

Approve it:

```bash
echo '{"approved": true, "approver": "you@example.com", "reason": "SQL and totals look right"}' \
  > warehouse_native_agent_demo/approvals/default.json
cd warehouse_native_agent_demo && uv run dg launch --assets answer_approved
```

The `approved` check flips from pending to passed.

## Production variants

- Swap `human_approval_gate` for `multi_approver_gate` for N-of-M sign-off instead of a single approver.
- Pair `approval_dir` with `FilesystemMonitorSensorComponent` to auto-progress the graph the instant a token drops, instead of a manual re-launch.
- Swap `snowflake_cortex_agent` for `databricks_genie_query` if you already have a Genie space configured.
