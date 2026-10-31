# Agent Governance Escalation Loop — quorum sign-off, audit trail, real rejection feedback
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh) — point `approval_dir` at cloud storage (`s3://`/`gs://`/`abfs://`) first, since the container filesystem is ephemeral.

**Components:** `agentic_pipeline`, `multi_approver_gate`, `approval_audit_asset`, `asset_job`, `rejection_feedback_loop` — all existing, composed via YAML.

**Script:** [`setup_agent_governance_demo.sh`](./setup_agent_governance_demo.sh)
**Cost:** ~$0.0001/run (one `gpt-4o-mini` call; everything else is local/free)
**Validated:** 2026-10-31 — RUN_SUCCESS on both the approve path (quorum 2/2 APPROVED) and the reject path (quorum 2/2 REJECTED, feedback captured); sensor verified to actually fire a re-run RunRequest + write feedback files.

## Why this exists

A single `HumanApprovalGateComponent` answers "did someone look at this?" This demo answers a harder, more realistic question: did a **quorum** of the right people sign off, is the decision **permanently audited**, and when someone says no, does their **feedback actually go somewhere** instead of the loop just dying? That's five components working together to tell one governance story, not five one-off demos.

```
deployment_proposal (agentic_pipeline, real gpt-4o-mini call)
       │
       ▼
deployment_gate (multi_approver_gate — 2-of-3 quorum, vote-by-file-drop)
       │                                           │
  (2 approve)                                (2 reject + feedback)
       │                                           │
       ▼                                           ▼
   ships                          rejection_feedback_loop sensor:
                                    writes .feedback/<approver>.txt,
                                    triggers regenerate_deployment_proposal

deployment_approval_audit (approval_audit_asset) reads the same approval_dir
as the gate — one row per vote: who, when, why, and (for rejections) what
feedback they left.
```

## Real gotchas found building this (not guessed)

1. **`multi_approver_gate` and `approval_audit_asset` identify the voter differently.** `multi_approver_gate` derives the approver's identity from the **filename** (`<key>.<approver_id>.json`) — its own docs' example vote body is just `{"approved": true, "reason": "..."}`, no approver field needed. `approval_audit_asset` was built around `HumanApprovalGateComponent`'s token shape, which puts `"approver"` **in the body**. Chain the two and you need both — the vote files in this demo's `defs.yaml` carry `"approver"` in the body AND the matching filename suffix.
2. **`multi_approver_gate` writes its own `<key>.multi_state.json` sidecar** (the timeout clock) into the same `approval_dir`. `approval_audit_asset`'s default `token_glob: "*.json"` picks that up as a spurious all-false row with no approver. Fix: a glob that only matches real vote files — `"*@*.json"` works here since every approver ID is an email.
3. **A real crash in `rejection_feedback_loop`**, found and fixed while building this: its internal file-move helper called `Path(dst).rsplit("/", 1)` — `.rsplit()` is a `str` method, not a `pathlib.Path` one — on every attempt to move a consumed rejection token. The line was dead code anyway (its result was never used; the very next line already creates the parent directory correctly), so the fix was to delete it. Fixed upstream in `dagster-component-templates`. If a fresh `dagster-component add rejection_feedback_loop --auto-install` still throws `AttributeError: 'PosixPath' object has no attribute 'rsplit'` the first time the sensor processes a real rejection, the registry hasn't picked up the fix yet.

## Scope note

`rejection_feedback_loop` is a **sensor** — it needs a running daemon (`dg dev`) to fire on its own schedule, not a one-shot `dg launch`. The script demonstrates the approve/reject quorum mechanics fully headlessly; triggering the sensor itself is a manual `dg dev` step (instructions printed at the end of the script), or can be evaluated once directly in Python via `dagster.build_sensor_context` — which is exactly how bug #3 above was actually caught and verified fixed.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `OPENAI_API_KEY` (or swap `model`/`api_key_env_var` in `agentic_pipeline`'s `defs.yaml` for a different provider — see [`litellm_inference_asset`](litellm.md)'s provider table)

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agent_governance_demo.sh | bash
cd agent_governance_demo
uv run dg dev
```

The script runs the full approve path headlessly (draft → pending → 2 votes dropped → APPROVED), then prints the exact commands to try the reject path and to fire the feedback-loop sensor yourself.

## What you'll see

- `deployment_gate`'s `quorum` check: `quorum pending (0/2 approvals)` → `quorum (2/2) approved`
- `deployment_approval_audit`: one row per vote with `approver`/`reason`/`decided_at`
- On the reject path: the same audit asset also captures each rejecter's `feedback` text, and `.approvals/.feedback/<approver>.txt` files appear once the sensor runs, each triggering `regenerate_deployment_proposal`

## Production variants

- Point `approval_dir` at `s3://`/`gs://`/`abfs://` for Dagster+ Serverless (the container filesystem is ephemeral)
- Swap the single `llm_call` step for `agentic_pipeline`'s `route`/`critique_loop`/`debate` ops for a more sophisticated proposal
- Wire the regenerated proposal to actually read `.feedback/<key>.txt` — see `rejection_feedback_loop`'s own README for the `mcp_call` wiring pattern that threads human feedback back into the next LLM call
