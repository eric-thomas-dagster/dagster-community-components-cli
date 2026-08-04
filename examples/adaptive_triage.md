# Adaptive Triage Router — the agent picks *which downstream runs*
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Components:** `synthetic_data_generator` (support_tickets), `langchain_chain_asset`, `router`, `dataframe_to_csv` — **100% composition of existing components, no new primitive required.**

**Script:** [`setup_adaptive_triage_demo.sh`](./setup_adaptive_triage_demo.sh)
**Cost:** ~$0.005 per run (20 tickets × gpt-4o-mini)
**Validated:** 2026-07-07 — 20 synthetic support tickets → LLM classified into 5 routes (billing/bug/churn_risk/spam/other) with high-confidence, sensible reasons, correctly handling English + Spanish + German + French tickets.

## Why this exists

Classic ETL routers use hand-coded predicates: `if total > 1000 then high_value else low_value`. The rules are frozen at pipeline-write time. **What if the router asks an agent per row, and the agent picks the downstream based on content?**

That's this demo. Incoming support tickets → LLM classifies each one → the `router` component fan-outs to different downstream assets based on the LLM's tag. Each route can have a completely different downstream pipeline (Slack for churn risks, JIRA for bugs, CRM for billing questions, dead-letter for spam).

```
raw_tickets            (synthetic_data_generator, support_tickets)
       ↓
classified_tickets     (langchain_chain_asset — gpt-4o-mini adds
                        `route` + `confidence` + `reason` per row)
       ↓
┌── billing_queue      ┐   (router splits by `route == "..."`)
│── bug_queue          │
│── churn_risk_queue   │
│── spam_queue         │
└── other_queue        ┘
       ↓ (each)
<route>.csv            (simulated sinks — swap for real destinations in prod)
```

## Why no new component

The pattern falls out of composing three existing pieces:

1. **`langchain_chain_asset` with `parse_json: true`** — LLM emits `{route, confidence, reason}`, which becomes new columns on the DataFrame.
2. **`router`** — filters the DataFrame by pandas query expressions like `'route == "billing"'`, one output asset per condition. Existing component; already ships.
3. **`dataframe_to_csv` (or any sink) per route** — the downstream that actually acts on each classification.

The agent picks BY NAME from a bounded route set. It cannot invent code, cannot invent routes (unrecognized routes fall through to `other_queue` — the router's `default_asset_name`). Every classification's confidence + reason is stored on the row → full audit trail.

## Prerequisites

- `uv` + `OPENAI_API_KEY`

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_adaptive_triage_demo.sh -o setup_adaptive_triage_demo.sh
chmod +x setup_adaptive_triage_demo.sh
./setup_adaptive_triage_demo.sh
```

## Validated run output (2026-07-07)

Per-route counts (20 tickets total):

```
billing:     4 tickets
bug:         9 tickets
churn_risk:  2 tickets
spam:        0 tickets   (no synthetic spam in the support_tickets generator)
other:       5 tickets
```

Sample classifications from the audit trail (`classified_tickets` asset):

```
ticket_text                                                         route   conf  reason
Login fails with 2FA. Please reset.                                 bug     0.90  Login failure — feature not working.
Charge my card ending in 7978.                                      billing 0.90  Card charge request — payment processing.
Mi tarjeta de crédito 4532-*** fue rechazada. ¿Por qué?             billing 0.90  Rejected credit card — payment issue.
Site is down — getting 502 errors since 9am EST.                    bug     0.95  Specific 502 error, service not working.
Bekommen ich Rabatte als jährlicher Abonnent?                       billing 0.90  Discount inquiry on annual subscription.
Bug report: search results show duplicates when filtering by date.  bug     0.95  Specific search functionality issue.
Order #57466 hasn't arrived. Can you check status?                  other   0.80  Order status inquiry, doesn't fit specific categories.
```

Multi-lingual (Spanish, German, French, English all classified correctly). Confidence scores let downstream sinks gate on high-confidence-only classifications.

## Extension patterns

The demo uses CSV sinks for reproducibility. Swap each per-route CSV with the destination that actually acts on that class of ticket:

| Route | Real destination |
|---|---|
| `billing` | `crm_lead_create` / `stripe_customer_note_add` / JIRA billing project |
| `bug` | `jira_ticket_create` (Engineering project) / `linear_issue` / GitHub Issue |
| `churn_risk` | `slack_notification` to `#save-the-account` / `gong_call_summary` reroute / CS handoff |
| `spam` | `dead_letter` sink / `s3_archive` for review |
| `other` | Queue for human triage / lower-priority auto-response |

Each is a component swap in the `<route>_export/defs.yaml` file — the classifier + router stay unchanged.

Other extensions:

- **Human-in-the-loop for low confidence.** Add an `asset_check` on `classified_tickets` that fails the downstream if `confidence < 0.7` on any row — those get held for human review.
- **Multi-route per ticket.** Set `exclusive: false` in the router. The LLM emits an array of routes; a downstream `array_exploder` fans the row out to each.
- **Add a router for priority.** Chain a second `router` after `classified_tickets` that also splits by `priority == "urgent"` — 2D routing (route × priority).
- **Real data upstream.** Replace `synthetic_data_generator` with a `zendesk_ingestion` / `intercom_ingestion` / `salesforce_case_ingestion` / IMAP source.

## Part of the agent-pipeline patterns family

See [agent_pipeline_patterns.md](./agent_pipeline_patterns.md) — overview of all seven agent-pipeline demos with a selection guide + adjacent-but-not-agentic patterns (`langgraph_agent`, `dbt_llm_pipeline`, `pii_redaction`, `data_quality_agent`, `cube_llm`).

## See also

- [PII detection + LLM redaction](./pii_redaction.md) — different agentic shape: LLM as a **double-checker** on rule-based output.
- [Router (deterministic version)](./router.md) — the same `router` component, hand-coded predicates instead of LLM-tagged routes.
- [Cube + LLM](./cube_llm.md) — LLM over structured metrics, no routing.
