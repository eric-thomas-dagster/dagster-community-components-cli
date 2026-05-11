# Cloud DLP — two flavors of PII detection in Dagster

**Validated end-to-end against real APIs** (servicepulse-490502). Same
Cloud DLP service, two Dagster object types — pick the right one for
your use case:

```
support_tickets       ← synthetic_data_generator (support_tickets schema, embeds PII)
       │
       ├── tickets_dlp_scanned    ← cloud_dlp_inspect_asset
       │                            (adds dlp_finding_count + dlp_infotypes + dlp_findings)
       │
       └── [asset check] no_contact_pii_in_tickets  ← cloud_dlp_pii_check
                                                      (fails when forbidden infoTypes detected)
```

## Components covered (3)

| Component | Category | Object type | Purpose |
|---|---|---|---|
| `synthetic_data_generator` | `ai` | `@dg.asset` | Synthetic upstream. `schema_type: support_tickets` produces multilingual ticket text with embedded names, emails, phones, and credit-card fragments — exactly the shape DLP is built for. |
| `cloud_dlp_inspect_asset` | `ai` | `@dg.asset` | Augments DataFrame with PII finding columns. Use for redaction routing, compliance reporting, training-data labeling. |
| `cloud_dlp_pii_check` | `asset_checks` | `@dg.asset_check` | Pass/fail gate. Use to BLOCK downstream materialization when forbidden PII appears. |

## Why two components, not one?

These are **different Dagster object types** with different integration shapes:
- Assets show up as lineage nodes that produce data downstream.
- Asset checks show up as gates under an existing asset (and with `blocking: true`, they actually block downstream materializations).

You can't satisfy both shapes with one component. Same DLP service, different consumer.

If you want BOTH augmented data AND a gate, chain them: scan with the asset, run the check on the inspected output, or run them independently on the upstream.

## Live run output

| Step | Result |
|---|---|
| `support_tickets` materialize | 5 rows |
| `tickets_dlp_scanned` materialize | 5 rows + `dlp_findings` columns. Rows T-003 and T-004 show non-zero `dlp_finding_count`. |
| `no_payment_or_ssn_pii` check | **FAILED** — `total_forbidden_findings: 2` (1 SSN + 1 credit-card number), `hits_by_info_type: {US_SOCIAL_SECURITY_NUMBER: 1, CREDIT_CARD_NUMBER: 1}` |

Run still succeeded because `blocking: false` on the check (failure recorded, downstream not blocked). Set `blocking: true` for hard gates.

## When to gate vs. annotate

| Scenario | Pick |
|---|---|
| Compliance boundary (raw → analytic schema): SSN/payment must NEVER cross | Check, `blocking: true` |
| Per-row redaction pipeline: need findings on every row | Asset |
| Audit dashboard ("which tickets touched PII this week?") | Asset (write findings to BQ downstream) |
| Drift detection: alert if PII shows up in a column it shouldn't | Check, `severity: WARN`, `blocking: false` |

## Cost

**< $0.01 for this demo.** DLP charges per inspection unit; first 1 GB/month free. The check defaults to scanning `sample_size: 200` rows to keep cost bounded — set to `null` to scan all.

## Required env vars + IAM

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GCP_PROJECT_ID=your-project

# Enable: https://console.cloud.google.com/apis/library/dlp.googleapis.com
# IAM:    roles/dlp.user on the service account
```

## Bugs surfaced fixing this demo

None — clean live validation. Both components worked first try once DLP API was enabled.

## Run it

```bash
./setup_cloud_dlp_demo.sh
cd cloud-dlp-demo
uv run dg launch --assets '*'
```
