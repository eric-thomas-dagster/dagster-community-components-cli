# X12 EDI Parser — synthetic envelopes → flat transaction DataFrame

**Validated end-to-end** (pure Python). 15 synthetic ASC X12 messages → 15 flat transaction rows ready for warehouse / claims / order-management pipelines.

```
x12_messages         ← synthetic_data_generator (x12_messages, 15 msgs)
       │
       └── x12_flat   ← x12_edi_parser (one row per ST/SE transaction)
```

## Components covered (2)

| Component | What it does |
|---|---|
| `synthetic_data_generator` | `schema_type: x12_messages` rotates 5 transaction sets: 270 (eligibility inquiry), 271 (eligibility response), 835 (remittance), 837 (healthcare claim), 850 (purchase order). Each wrapped in full ISA/GS envelopes. |
| `x12_edi_parser` | Auto-detects ISA delimiters per spec, walks segments, emits one row per ST/SE transaction with ISA/GS context columns + transaction-specific fields (payment_amount, claim_total_charge, po_number, payer_name, …). |

## Live output

```
15 rows × 29 cols
By transaction_set:
  270    3
  271    3
  835    3
  837    3
  850    3
```

**837 sample (healthcare claims):**

| isa_sender_id | gs_version | payer_name | subscriber_last_name | claim_account_num | claim_total_charge |
|---|---|---|---|---|---|
| PROVIDER12 | 005010X222A1 | BCBS | LEE | CLAIM000000004 | 1,247.50 |

**835 sample (remittance advice):**

| payer_name | payment_amount | payment_method | payment_date | credit_debit |
|---|---|---|---|---|
| BCBS | 1,027.57 | ACH | 20250115 | C |

**850 sample (purchase orders):**

| po_type | po_number | po_date |
|---|---|---|
| SA | PO000000005 | 20250115 |

## Supported transaction sets

| Code | Name | Domain |
|---|---|---|
| **270 / 271** | Eligibility inquiry / response | Healthcare |
| **835** | Remittance advice / claim payment | Healthcare |
| **837** | Healthcare claim (P / I / D) | Healthcare |
| **850 / 855** | Purchase order / ack | Retail / supply chain |

Other transaction sets fall through with the ISA/GS/ST envelope context populated; extend the component for new extractors.

## Auto-detected delimiters

ISA fields are positionally fixed-width per spec, so element/component/segment separators are read from byte offsets in the header — no configuration. Works with both `~`-segment / `*`-element (canonical) and pipe-rendered logs.

## Bugs surfaced fixing this demo

1. **BPR16 payment_date positional mismatch**: synthetic data placed the date at offset 15 (BPR15) rather than 16. Fixed in the generator by adding one element separator. Parser was correct per spec.

## Why X12 EDI matters

ASC X12 is the dominant US-domestic EDI standard. Every major payer, retailer, logistics provider, and bank speaks it. Migrations to FHIR + JSON are happening but the legacy X12 fire-hose isn't going away — claims clearinghouses, ERP integrations, and supply-chain ETL all ingest these envelopes daily.

## Run it

```bash
./setup_x12_edi_demo.sh
cd x12-edi-demo
uv run dg launch --assets '*'
```
