# Slack Approval Gate — HITL approvals via Slack reactions, composed on the existing gate

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is. Reactions polling is outbound HTTPS to Slack's API — no webhook / no public URL required. Trade-off: 30-60s approval-detection latency (fine for humans).

**Components:** `SlackApprovalGateComponent` (Slack side — post + poll + write token) + `HumanApprovalGateComponent` (asset-check gate — reads token, blocks downstream) + `FilesystemMonitorSensorComponent` (auto-progression) + `AssetJobComponent` (sensor target) + optional `AgenticPipelineComponent` (source of the report needing approval).
**Setup script:** [`setup_slack_approval_gate_demo.sh`](./setup_slack_approval_gate_demo.sh).
**Cost:** ~$0.001 per run (gpt-4o-mini for the synthetic report). Slack side is free.

Closes the Airflow 2.10 HITL parity gap: multi-approver Slack quorum, timeout policies, escalation pings, all on Dagster's asset-check-driven substrate.

## Why reactions polling (not interactive buttons)

Slack Interactive Components (buttons, slash commands) need a **public webhook** Slack can POST to. That doesn't work in Dagster+ Serverless — containers are short-lived, no fixed URL. Reactions polling is pure outbound HTTPS to Slack's API — works everywhere Dagster runs. Trade-off: 30-60s latency to detect an approval instead of instant. For human-in-the-loop that's fine; approvers are humans on Slack, not real-time systems.

## Architecture

```
                    (upstream asset needing approval)
                              │
                              ▼
      ┌──────────────────────────────────────────────────┐
      │  SlackApprovalGateComponent                      │
      │  ┌──────────────────────────────────────────┐    │
      │  │  report_approval_slack_posted (asset)    │    │
      │  │    - posts to $SLACK_CHANNEL             │    │
      │  │    - seeds :white_check_mark: / :x:      │    │
      │  │    - records message_ts in sidecar       │    │
      │  └──────────────────────────────────────────┘    │
      │  ┌──────────────────────────────────────────┐    │
      │  │  report_approval_slack_watcher (sensor)  │    │
      │  │    - polls reactions every 30s           │    │
      │  │    - counts allowlisted approvers        │    │
      │  │    - on quorum: writes JSON token        │    │
      │  │    - timeout policy (optional)           │    │
      │  └──────────────────────────────────────────┘    │
      └──────────────────────┬───────────────────────────┘
                             │
                             ▼ writes
              /path/to/approvals/<partition_key>.json
                             │
                             ▼
      ┌──────────────────────────────────────────────────┐
      │  HumanApprovalGateComponent (existing)           │
      │    - reads token, drives 'approved' check        │
      │    - `passed=True` → downstream unblocks         │
      │    - `passed=False` → downstream stays blocked   │
      └──────────────────────┬───────────────────────────┘
                             │
                             ▼
      ┌──────────────────────────────────────────────────┐
      │  FilesystemMonitorSensorComponent                │
      │  ┌──────────────────────────────────────────┐    │
      │  │  approval_watcher                        │    │
      │  │    - fires ship_report_job on token drop │    │
      │  └──────────────────────────────────────────┘    │
      └──────────────────────┬───────────────────────────┘
                             │
                             ▼
              ship_report_job (AssetJobComponent)
```

## Setup

### 1. Create a Slack bot

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**
2. Under **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**, add:
   - `chat:write` — post approval requests
   - `reactions:read` — poll for votes
   - `reactions:write` — seed the initial approve/reject emojis
3. **Install to Workspace** → copy the `xoxb-...` bot token
4. In your Slack workspace, invite the bot to the approval channel:
   ```
   /invite @your-bot-name
   ```
5. Export the token:
   ```bash
   export SLACK_BOT_TOKEN=xoxb-...
   ```

### 2. Get approver user IDs

Right-click a user in Slack → **View profile** → **⋮ More** → **Copy member ID**. Comma-separate them:
```bash
export SLACK_APPROVER_USER_IDS="U1234ALICE,U5678BOB,U9012CAROL"
```

### 3. Set the channel + quorum

```bash
export SLACK_CHANNEL="#dagster-approvals"        # or C123ABC456 ID
export REQUIRED_APPROVERS=2                      # N-of-M quorum
```

### 4. Run the setup

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_slack_approval_gate_demo.sh \
  -o setup_slack_approval_gate_demo.sh
bash setup_slack_approval_gate_demo.sh
```

## The interactive flow

1. **Generate the report** — `dagster asset materialize --select report_source` runs the AgenticPipeline LLM step.
2. **Post to Slack** — materialize `report_approval_slack_posted`. The message shows up in `$SLACK_CHANNEL` with the report text, seed reactions, and `@`-mentions to the approver allowlist.
3. **Start `dg dev`** — this launches the `report_approval_slack_watcher` sensor.
4. **Approvers react** — the allowlisted users react `:white_check_mark:` (or `:x:`) on the post. Once `REQUIRED_APPROVERS` allowlisted approvers have reacted approve, the watcher writes the JSON token file at the next tick (~30s).
5. **`approval_watcher` fires `ship_report_job`** — the FilesystemMonitorSensor detects the new token and materializes `report_approval`, which reads the token via `HumanApprovalGateComponent` and passes the `approved` check.
6. **Downstream unblocks.** Any assets with `AutomationCondition.eager()` waiting on `report_approval:approved` fire.

## The token shape (written by the watcher on quorum)

Compatible with `HumanApprovalGateComponent`'s reader:

```json
{
  "approved": true,
  "approver": "U1234ALICE,U5678BOB",
  "reason": "Slack quorum reached: 2/2 approved",
  "timestamp": "2026-08-19T14:32:16Z",
  "source": "slack_approval_gate",
  "slack_message_ts": "1734567890.123456"
}
```

Rejection quorum (any allowlisted user reacts `:x:` reaching threshold) writes `approved: false`. Timeout policies (`escalate` / `reject` / `approve`) apply if `timeout_hours` is set.

## What Dagster gives you here that Slack alone doesn't

- **Approval state as a first-class asset check.** Auditors query "who approved which report and when" via asset history, not Slack log dives.
- **Downstream automation gates on the check.** Any Dagster automation condition can key off `report_approval:approved` — no bespoke webhook glue.
- **Per-partition approvals.** Composed with a partitioned upstream (e.g. `AgenticPipelineComponent` with dynamic partitions), each partition gets its own Slack message + its own token file. Approvals scale independently.
- **Insights-friendly.** Approval timings + who-approved land in materialization metadata → queryable + dashboardable.
- **Retry-safe.** Re-materializing `report_approval_slack_posted` on the same partition is a no-op (existing sidecar honored) — no duplicate Slack messages.

## Composition patterns

### With `AgenticPipelineComponent`

Use case: agentic pipeline generates a report, Slack approves before it ships.

```
mir_report (AgenticPipelineComponent, dynamic-partitioned)
   ↓
report_approval_slack (SlackApprovalGate, same partitions_def)
   ↓ (sensor writes token on quorum)
report_approval (HumanApprovalGate, same partitions_def)
   ↓ (check passes)
ship_report_job (fires via FilesystemMonitorSensor)
```

### With `InferenceCostReportComponent` merge gate

Use case: A/B test proposes swapping to a local provider. Merge gate on quality; if quality holds, humans approve the switch.

```
InferenceProviderABTest → ProviderABEvaluator (min_winner_score=70)
                              ↓ (winner_meets_threshold check passes)
                          InferenceCostReport
                              ↓
                          SlackApprovalGate ← manual sign-off from data lead
                              ↓ (quorum reached)
                          promote_provider_change (config asset)
```

Automated quality gate + human final sign-off. Full audit trail.

## What this does NOT do

- **Instant approval UX.** Reactions polling is 30-60s. If you need instant (say, incident-response critical paths), you'd need Slack Socket Mode with a long-running Hybrid agent. Not shipping today — future component (`slack_socket_approval_gate` on backlog).
- **Threaded discussion feedback.** Approvers can discuss in the same Slack thread, but the discussion doesn't flow back to Dagster. The `reason` field on the token stays generic (`"Slack quorum reached: 2/2 approved"`).
- **Revise-and-retry on rejection.** Rejection is terminal — token gets `approved: false`, downstream stays blocked. See `rejection_with_feedback_loop` (backlog) for the revise pattern.

## Related components

- **`HumanApprovalGateComponent`** — the existing file-based gate. Slack version composes with it via the shared `approval_dir`.
- **`FilesystemMonitorSensorComponent`** — auto-progresses downstream jobs when tokens appear.
- **`AssetJobComponent`** — the sensor target job that materializes the approval.
- **`AgenticPipelineComponent`** — typical upstream (generates the report needing approval).
