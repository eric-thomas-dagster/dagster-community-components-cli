# Compliance Evidence Automation — AI drafts, a human signs off, every decision is audited
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh) — point `approval_dir` at cloud storage first.

**Components:** `inline_dataframe`, `vanta_evidence_response_agent`, `multi_approver_gate`, `approval_audit_asset` — all existing, composed via YAML.

**Script:** [`setup_compliance_evidence_demo.sh`](./setup_compliance_evidence_demo.sh)
**Cost:** ~$0.0002/run (two `gpt-4o-mini` calls, one per control)
**Validated:** 2026-10-31 — RUN_SUCCESS; real evidence narratives drafted for 2 SOC2 controls, reviewer sign-off, quorum (1/1) APPROVED, audit trail populated.

## Why this exists

A different flavor of agent governance than [`agent_governance_escalation.md`](agent_governance_escalation.md) — compliance-specific, not generic agent-action approval. An LLM drafts SOC2/ISO27001 evidence-response narratives against your real control set; a reviewer has to sign off before anything goes to an auditor; the decision is permanently audited.

```
soc2_controls (inline_dataframe — 2 sample controls: CC6.1, CC7.2)
       │
       ▼
control_narratives (vanta_evidence_response_agent — real gpt-4o-mini
                     drafts one evidence narrative per control)
       │
       ▼
evidence_review_gate (multi_approver_gate — 1-of-2 reviewer sign-off,
                       vote-by-file-drop)
       │
       ▼
evidence_review_audit (approval_audit_asset — who approved, when, why)
```

## Notes

- `vanta_evidence_response_agent` has **no `demo_mode`** — it always calls a real LLM (it's drafting real compliance language, not a fixed-cost API call worth faking). Cost here is trivially small (~$0.0002/run for 2 short controls).
- No `synthetic_data_generator` schema type fits "compliance controls," so this uses [`inline_dataframe`](inline_dataframe.md) — a small, literal 2-row table. Swap it for a real pull from your Vanta account in production (see "Production variants" below).
- Same `multi_approver_gate` / `approval_audit_asset` interop gotchas as [`agent_governance_escalation.md`](agent_governance_escalation.md) apply here — vote files need `"approver"` in the JSON body, and `token_glob` is set to `"*@*.json"` to skip the gate's own `*.multi_state.json` sidecar. See that walkthrough for the full explanation.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `OPENAI_API_KEY`

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_compliance_evidence_demo.sh | bash
cd compliance_evidence_demo
uv run dg dev
```

## What you'll see

- `control_narratives`: real drafted evidence text for CC6.1 (access control) and CC7.2 (incident response)
- `evidence_review_gate`'s `quorum` check: `quorum pending (0/1 approvals)` → `quorum (1/1) approved`
- `evidence_review_audit`: the reviewer, timestamp, and sign-off reason

## Production variants

- Replace `inline_dataframe` with a real Vanta API pull of your actual control set — see `vanta_evidence_response_agent`'s README for the exact upstream shape it expects (`control_id`/`description` columns)
- Point `approval_dir` at cloud storage for Dagster+ Serverless (the container filesystem is ephemeral)
- Pair with [`agent_governance_escalation.md`](agent_governance_escalation.md)'s `rejection_feedback_loop` sensor to auto-redraft a rejected narrative using the reviewer's specific feedback
