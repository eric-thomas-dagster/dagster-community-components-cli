#!/usr/bin/env bash
# Document Extractors mega-demo — 13 LLM-driven document extractors,
# each tuned for a different domain (resume, invoice, receipt, contract,
# bank statement, expense report, insurance claim, job posting, legal,
# medical, purchase order, scientific paper, shipping label).
#
# WHAT THIS DEMONSTRATES
#   A single source DataFrame of 13 synthetic documents (one per type)
#   fans out through 13 LLM extractors. Each extractor returns its
#   domain-specific structured fields (e.g. resume → name/skills/years;
#   invoice → vendor/total/line items; medical → diagnosis/procedures).
#
# Pipeline:
#   sample_documents (custom inline asset, 13 synthetic documents)
#         │
#         ├── resume_extractor             → name, skills, years_experience, etc.
#         ├── invoice_extractor            → vendor, invoice_no, total, line_items
#         ├── receipt_extractor            → merchant, items, total
#         ├── contract_extractor           → parties, term, amount, clauses
#         ├── bank_statement_extractor     → account_no, transactions, balances
#         ├── expense_report_extractor     → employee, expenses, total
#         ├── insurance_claim_extractor    → claim_no, policyholder, amount, type
#         ├── job_posting_extractor        → title, company, salary, requirements
#         ├── legal_document_extractor     → parties, citations, jurisdiction
#         ├── medical_record_extractor     → patient, diagnoses, medications
#         ├── purchase_order_extractor     → po_number, vendor, items
#         ├── scientific_paper_extractor   → title, authors, abstract, methods
#         └── shipping_label_extractor     → tracking, sender, recipient, weight
#
# REQUIRED ENV
#   OPENAI_API_KEY   OpenAI API key (sk-...)
#
# COST
#   ~$0.20–$0.50 against gpt-4o-mini for 13 docs × 13 extractors.

set -euo pipefail
PROJECT_DIR="${1:-document-extractors-demo}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "ERROR: set OPENAI_API_KEY"
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas openai litellm
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 13 extractor components"
for c in resume_extractor invoice_extractor receipt_extractor \
         contract_extractor bank_statement_extractor expense_report_extractor \
         insurance_claim_extractor job_posting_extractor legal_document_extractor \
         medical_record_extractor purchase_order_extractor \
         scientific_paper_extractor shipping_label_extractor; do
  $CLI add $c --auto-install
done

echo ">>> Writing inline source documents asset"
mkdir -p "src/$PKG/defs/sample_documents"
cat > "src/$PKG/defs/sample_documents/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg


SAMPLES = [
    ("resume", """JANE SMITH
Email: jane.smith@example.com | Phone: (555) 123-4567 | LinkedIn: /in/janesmith

PROFESSIONAL SUMMARY
Senior software engineer with 8 years of experience building distributed systems in Python and Go.

EXPERIENCE
Acme Corp — Staff Software Engineer (2021-present)
  • Led migration of monolith to microservices, reducing p99 latency 40%
  • Mentored team of 6 engineers
Globex Inc — Senior Engineer (2017-2021)
  • Built real-time analytics pipeline processing 2B events/day

SKILLS
Python, Go, Kubernetes, AWS, PostgreSQL, gRPC

EDUCATION
B.S. Computer Science, MIT (2017)"""),
    ("invoice", """INVOICE
Acme Office Supplies, Inc.
123 Industrial Way, Springfield, IL 62701
Invoice #: INV-2025-04012  |  Date: 2025-04-15  |  Due: 2025-05-15

Bill To: Globex Corp, 500 Market Street, San Francisco, CA 94105

Description                            Qty    Unit Price    Total
Letter-size copy paper, 5000 sheets     10        $42.00   $420.00
Black ballpoint pens (box of 12)        25         $8.50   $212.50
Standing desk, electric                  3       $649.00 $1,947.00

Subtotal: $2,579.50
Tax (7.5%): $193.46
Total Due: $2,772.96"""),
    ("receipt", """STARBUCKS COFFEE — STORE #4521
1234 Main Street, Anytown, USA
Date: 2025-05-12  Time: 8:42 AM

Grande Latte           $5.45
Blueberry Muffin       $3.25
Subtotal               $8.70
Tax                    $0.70
Total                  $9.40

Paid by VISA ending 4242  Auth: 084213"""),
    ("contract", """SERVICES AGREEMENT

This Agreement is entered into on April 1, 2025 between
Acme Consulting LLC ("Consultant") and Globex Corporation ("Client").

1. SCOPE: Consultant shall provide cloud architecture review services
   for Client's e-commerce platform.
2. TERM: 6 months commencing April 15, 2025, ending October 14, 2025.
3. COMPENSATION: $25,000 monthly, due on the 1st of each month.
4. CONFIDENTIALITY: Both parties agree to non-disclosure of all
   proprietary information.
5. TERMINATION: Either party may terminate with 30 days written notice.

Signed:
___________________ (Consultant)   ___________________ (Client)
Jane Doe, Principal               John Roe, CTO"""),
    ("bank_statement", """BANK OF NORTHEAST — STATEMENT OF ACCOUNT
Account Holder: Robert Johnson
Account #: ****-****-1234   Period: 04/01/2025 - 04/30/2025

Opening Balance: $12,450.32
Deposits:
  04/02 Payroll - Acme Corp                +$4,200.00
  04/15 Direct Deposit - Treasury Refund    +$1,250.00
Withdrawals:
  04/05 Rent - Smith Properties             -$2,100.00
  04/10 Grocery - Whole Foods                  -$184.55
  04/22 Utility - PG&E                         -$165.40
  04/28 ATM - Main Street Branch               -$200.00
Closing Balance: $15,250.37"""),
    ("expense_report", """EXPENSE REPORT — Q2 2025
Submitted by: Sarah Chen, Engineering
Period: April 1-30, 2025
Approver: Michael Rodriguez, VP Engineering

Date     Description                        Category     Amount
04/03    Flight to NYC for client mtg       Travel       $452.00
04/03    Hotel - Marriott (3 nights)        Travel       $720.00
04/04    Client dinner — Per Se             Meals        $245.00
04/12    AWS re:Invent ticket               Training   $1,795.00
04/22    Office supplies — Staples          Supplies      $42.50

Total: $3,254.50
Status: Pending Approval"""),
    ("insurance_claim", """AUTOMOBILE INSURANCE CLAIM
Claim #: AUT-2025-77192
Policyholder: Maria Garcia
Policy #: POL-998234

Date of Incident: April 18, 2025
Location: Intersection of 5th Ave & Pine St, Seattle WA
Type: Collision

Description: Insured vehicle (2022 Honda Civic, plate WA-7XJK230) struck
from behind at red light by 2018 Ford F-150. No injuries reported.
Police report filed: SPD-2025-441872.

Damage Estimate: $4,250.00 (rear bumper, trunk, sensor module)
Adjuster Assigned: Tom Williams, ext. 1422"""),
    ("job_posting", """SENIOR DATA ENGINEER
Globex Corporation — Remote (US)

We're seeking a Senior Data Engineer to lead our data platform team.
You'll architect and operate a multi-petabyte lakehouse serving 200+
internal users.

Responsibilities:
  • Design and operate streaming + batch pipelines (Kafka, Spark, Flink)
  • Lead migration to Iceberg + dbt
  • Mentor 5+ engineers

Requirements:
  • 8+ years data engineering experience
  • Expert Python, SQL; production Spark or Flink
  • AWS or GCP

Compensation: $185,000 - $245,000 + equity + benefits
Apply: careers@globex.example"""),
    ("legal_document", """IN THE UNITED STATES DISTRICT COURT
FOR THE NORTHERN DISTRICT OF CALIFORNIA

ACME, INC.,                       )
                Plaintiff,        )    Case No. 3:25-cv-04125-JST
        v.                        )
GLOBEX CORPORATION,               )    MOTION FOR SUMMARY JUDGMENT
                Defendant.        )

Plaintiff Acme, Inc. respectfully moves this Court for summary judgment
pursuant to Federal Rule of Civil Procedure 56. The undisputed facts
show that Defendant breached the Master Services Agreement dated
January 15, 2024, by failing to deliver milestones 4-7 on schedule.
See Restatement (Second) of Contracts § 235. Damages of $4.2 million
are owed under Section 8.2 of the agreement.

Date: May 1, 2025          /s/ Sarah Lee
                           Sarah Lee, SBN 218742"""),
    ("medical_record", """PATIENT VISIT NOTE
Patient: John Anderson, DOB: 1978-03-22, MRN: 887421
Date: 2025-05-10        Provider: Dr. Emily Park, MD

CHIEF COMPLAINT: Persistent cough, 3 weeks duration

HISTORY OF PRESENT ILLNESS: 47-year-old male presenting with non-productive
cough that began 3 weeks ago. Denies fever, chills, weight loss. No travel.

EXAMINATION: BP 128/82, HR 76, T 98.4, RR 14, SpO2 98%
Lungs: Clear to auscultation bilaterally. No wheezes/rales.

ASSESSMENT: Likely post-viral cough vs. cough-variant asthma.

PLAN:
  • Trial of fluticasone HFA 110 mcg, 2 puffs BID x 2 weeks
  • Chest X-ray to rule out infiltrate
  • Return in 2 weeks if not improved"""),
    ("purchase_order", """PURCHASE ORDER
PO #: PO-2025-008834
Date: 2025-05-08
Buyer: Globex Corporation, Procurement Dept

Vendor: Acme Industrial Equipment Co.
        4500 Manufacturing Blvd, Cleveland OH 44114

Ship To: Globex Plant 2, 8800 Highway 6, Houston TX 77001

Item #     Description                     Qty   Unit Cost     Ext
A-7821     Hydraulic press, 50-ton, 220V     2    $14,500   $29,000
B-9912     Replacement seals, set of 6      24       $185    $4,440
C-5500     Safety mat, anti-fatigue          8       $215    $1,720

Subtotal:  $35,160.00
Shipping:     $850.00
Total:     $36,010.00

Payment Terms: Net 30
Required by: 2025-06-15"""),
    ("scientific_paper", """Title: Improving Latency in Distributed Consensus Protocols Through Adaptive Quorum Sizing

Authors:
  Wei Zhang^1, Maria Hernandez^2, Raj Patel^1
  ^1 Stanford University, ^2 MIT CSAIL

Abstract:
We present AQS, an adaptive quorum-sizing approach for Raft and
Multi-Paxos consensus protocols. Unlike fixed-quorum schemes, AQS
dynamically resizes quorums based on observed latency distributions
across replicas. Across 100 cloud-region deployments, AQS reduced p99
commit latency by 38% compared to standard Raft, with no impact on
fault tolerance. Source: github.com/stanford-aqs/aqs.

1. Introduction
Distributed consensus is a fundamental primitive in modern data systems...

2. Methods
We modified Raft's append-entries pathway to compute a per-quorum
weighted majority based on a sliding-window latency estimator..."""),
    ("shipping_label", """FROM:
Acme Distribution
8500 Logistics Pkwy
Memphis, TN 38116

TO:
Sarah Chen
1212 Park Avenue, Apt 4B
New York, NY 10128

Tracking: 1Z 999 AA1 0123 4567 84
Service: UPS Ground (3-5 business days)
Weight: 4.2 lbs
Dimensions: 12 x 8 x 6 in
Value: $89.00

Reference: ORDER-2025-77231-3"""),
]


@dg.asset(group_name="ingest", description="13 synthetic documents (1 per extractor type)")
def sample_documents() -> pd.DataFrame:
    df = pd.DataFrame(SAMPLES, columns=["doc_type", "text"])
    df["doc_id"] = range(len(df))
    return df


defs = dg.Definitions(assets=[sample_documents])
PYEOF

echo ">>> Writing 13 extractor defs.yaml"
# Use parallel arrays instead of associative arrays so the script works on
# Apple's stock bash 3.2 (which doesn't support `declare -A`).
set +u  # the for-loop tolerates unbound during expansion
EXTRACTORS="resume:ResumeExtractorComponent invoice:InvoiceExtractorComponent receipt:ReceiptExtractorComponent contract:ContractExtractorComponent bank_statement:BankStatementExtractorComponent expense_report:ExpenseReportExtractorComponent insurance_claim:InsuranceClaimExtractorComponent job_posting:JobPostingExtractorComponent legal_document:LegalDocumentExtractorComponent medical_record:MedicalRecordExtractorComponent purchase_order:PurchaseOrderExtractorComponent scientific_paper:ScientificPaperExtractorComponent shipping_label:ShippingLabelExtractorComponent"
for entry in $EXTRACTORS; do
  c="${entry%:*}"
  CLASS="${entry#*:}"
  cat > "src/$PKG/defs/${c}_extractor/defs.yaml" <<EOF
type: $PKG.components.${c}_extractor.component.${CLASS}
attributes:
  asset_name: ${c}_fields
  upstream_asset_key: sample_documents
  input_column: text
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: extractors
EOF
done
set -u

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

13 LLM extractors × 13 docs each = ~169 OpenAI calls. Cost <\$0.50 on gpt-4o-mini.

Inspect:
    uv run dg dev   # http://localhost:3000 → Assets graph
MSG
