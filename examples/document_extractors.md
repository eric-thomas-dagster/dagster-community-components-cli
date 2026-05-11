# Document extractors mega-demo (13 components)

**Validated end-to-end** — 13 LLM-driven document extractors, each
specialized for a different document domain, all running against a
shared synthetic source DataFrame.

```
sample_documents (13 synthetic documents, one per type)
       │
       ├── resume_extractor             → name, skills, experience
       ├── invoice_extractor            → vendor, total, line items
       ├── receipt_extractor            → merchant, items, total
       ├── contract_extractor           → parties, term, amount, clauses
       ├── bank_statement_extractor     → account, transactions, balances
       ├── expense_report_extractor     → employee, expenses, total
       ├── insurance_claim_extractor    → claim_no, policyholder, amount
       ├── job_posting_extractor        → title, company, salary, requirements
       ├── legal_document_extractor     → parties, citations, jurisdiction
       ├── medical_record_extractor     → patient, diagnoses, medications
       ├── purchase_order_extractor     → po_number, vendor, items
       ├── scientific_paper_extractor   → title, authors, abstract
       └── shipping_label_extractor     → tracking, sender, recipient
```

All 13 extractors share the same field interface (`asset_name`,
`upstream_asset_key`, `input_column`, `model`, `api_key_env_var`) — just
swap the class to target a different document domain.

## Validated end-to-end (timings)

| Asset | Time |
|---|---|
| `resume_fields` | 22s |
| `shipping_label_fields` | 25s |
| `scientific_paper_fields` | 29s |
| `contract_fields` | 29s |
| `medical_record_fields` | 31s |
| `insurance_claim_fields` | 33s |
| `job_posting_fields` | 36s |
| `invoice_fields` | 36s |
| `bank_statement_fields` | 37s |
| `receipt_fields` | 39s |
| `expense_report_fields` | 41s |
| `legal_document_fields` | 44s |
| `purchase_order_fields` | 46s |

Total wall-clock: ~50s with parallel multiprocess execution.
~$0.20–$0.50 on `gpt-4o-mini` (13 docs × 13 extractors ≈ 169 LLM calls).

## Sample documents

The demo ships an inline `sample_documents` asset with 13 realistic
synthetic documents — one per extractor type. Examples:

- **Resume**: senior software engineer with 8y experience, skills, edu
- **Invoice**: Acme Office Supplies → Globex, $2,772.96 with line items
- **Bank statement**: monthly transaction history with deposits + withdrawals
- **Medical record**: patient visit note with chief complaint, exam, plan
- **Legal document**: federal court motion for summary judgment
- **Scientific paper**: distributed-systems abstract + methods section

This means each extractor's "domain match" runs against its own document,
but the extractor also tries to extract from the other 12 documents —
nulls/partial extraction in those cells confirm the extractor handles
out-of-domain inputs gracefully.

## Run

```bash
export OPENAI_API_KEY='sk-...'

curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_document_extractors_demo.sh | bash
cd document-extractors-demo
uv run dg launch --assets '*'
# Or in dev UI:
uv run dg dev   # → http://localhost:3000 → Assets graph
```

## When to use which extractor

| Domain | Component |
|---|---|
| Job applicants → typed candidate records | [`resume_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/resume_extractor) |
| AP automation → vendor invoices into ERP | [`invoice_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/invoice_extractor) |
| Expense reimbursement → digital receipts | [`receipt_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/receipt_extractor) |
| Legal review → contract parties / clauses / dates | [`contract_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/contract_extractor) |
| Cash-flow analysis → bank statement transactions | [`bank_statement_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/bank_statement_extractor) |
| T&E spend → categorized expense reports | [`expense_report_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/expense_report_extractor) |
| Claims processing → structured claim records | [`insurance_claim_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/insurance_claim_extractor) |
| Talent intake → job posting metadata | [`job_posting_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/job_posting_extractor) |
| eDiscovery → motion / brief metadata | [`legal_document_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/legal_document_extractor) |
| EHR integration → diagnoses / medications | [`medical_record_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/medical_record_extractor) |
| Procurement → PO line items | [`purchase_order_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/purchase_order_extractor) |
| Lit-review automation → paper metadata | [`scientific_paper_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/scientific_paper_extractor) |
| Shipping operations → tracking / weight / addresses | [`shipping_label_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/shipping_label_extractor) |

## Cost

~$0.20–$0.50 per full run on `gpt-4o-mini`.

## See also

- [`llm_execution.md`](./llm_execution.md) — generic LLM-orchestration
  components (12 components: openai_llm, langchain, dspy, instructor,
  litellm, llm_judge, llm_output_parser, etc.)
- [`ai_with_llm.md`](./ai_with_llm.md) — task-specific LLM components
  (text_classifier, entity_extractor, sentiment_analyzer, etc.)
- [`ai_no_llm.md`](./ai_no_llm.md) — local AI components (no API key)
