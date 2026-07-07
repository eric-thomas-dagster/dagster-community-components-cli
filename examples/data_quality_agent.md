# Data Quality Agent — Anomaly Detection + LLM Explanations

**Components:** `synthetic_data_generator`, `anomaly_detection`, `filter`, `langchain_chain_asset` — all existing, composed via YAML.

**Script:** [`setup_dq_agent_demo.sh`](./setup_dq_agent_demo.sh)
**Cost:** ~$0.005 per run (~50 LLM calls on gpt-4o-mini)
**Validated:** 2026-07-07 — 50 anomalies flagged from 500 synthetic transactions, LLM wrote plausible_reason + followup_check per row.

## Why this exists

Every data quality pipeline says "row 42 is anomalous, z-score 4.2" — and then no one has time to figure out **why**. That's the actual bottleneck. This demo runs classical anomaly detection and hands each flagged row to an LLM that writes:

- **`plausible_reason`** — a business-plausible hypothesis (fraud, testing, bulk-purchase, bad upstream ETL, chargeback attempt, etc.)
- **`followup_check`** — a concrete next step the on-call analyst should run

The result: your on-call gets a queue of anomalies with pre-thought-out investigation notes, not a wall of z-scores.

```
transactions (500 rows)  →  anomalies (z_score > 1.5 flag)  →  anomalies_only (filter)  →  anomaly_narratives (LLM)
```

## Prerequisites

- `uv` + `OPENAI_API_KEY`

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dq_agent_demo.sh -o setup_dq_agent_demo.sh
chmod +x setup_dq_agent_demo.sh
./setup_dq_agent_demo.sh
```

## Validated run samples (2026-07-07)

```
amount=$876.99  score=1.764  (food category)
  reason:   Possible fraudulent refund request due to the high amount relative to typical food purchases.
  followup: Verify if the refund was initiated by the original purchaser and check for any previous
            transactions related to this refund.

amount=-$997.34  score=1.505  (transfer)
  reason:   Possible fraudulent transaction or error in data entry.
  followup: Verify if the account has reported any unauthorized withdrawals or if there are any
            recent changes to account settings.

amount=$954.05  score=1.899  (deposit, category: gas)
  reason:   This could be a fraudulent transaction, as the deposit amount for a gas category is
            unusually high compared to typical transactions.
  followup: Verify the transaction history for the account associated with this transaction to see
            if there are any similar high-value deposits or patterns that indicate potential fraud.
```

The LLM correctly connects the anomaly to the row's context (category, amount sign) — not generic "this is a high number" answers.

## Extension patterns

- **Fail the asset check on threshold.** Add a `dagster_asset_check` that fails the run if `anomaly_narratives` has > N rows — CI gate.
- **Route to Slack on 'fraud'.** Add a `slack_notification` sink that fires when `plausible_reason` contains "fraud" — on-call page automation.
- **Bring real data.** Replace `synthetic_data_generator` with `snowflake_query_asset` / `bigquery_query_asset` / `postgres_query_asset` reading your actual transactions table.
- **Multi-column anomaly.** `anomaly_detection` supports one metric column — chain multiple instances (one per column) then union the flagged sets.
- **Feed to `langgraph_agent` for multi-step investigation.** Replace the single-shot LangChain with a LangGraph flow: plan → look up account history → check other transactions → synthesize.

## Related

- [PII detection + LLM redaction](./pii_redaction.md) — same "row-wise LLM as pass 2" shape, different problem (compliance vs. quality).
- [Cube semantic layer + LLM](./cube_query.md) — LLM-over-structured-metrics pattern for BI.
