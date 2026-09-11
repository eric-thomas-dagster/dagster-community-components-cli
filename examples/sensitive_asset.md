# Scrub PII & secrets before they hit the event log
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in under a minute (plus `uv run dg launch`
startup overhead).

## What this demo shows

Two assets, one per shape. Both proxy `context.log.*` calls through a
redactor that scrubs matching key patterns BEFORE the log line lands in
the Dagster event log — plus scrub matching keys in returned
`MaterializeResult.metadata`. Both emit `sensitive_redacted_count`
observations for SOC2 audit.

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_user_export` (Python decorator) | log line shows `ssn=[REDACTED] password=[REDACTED] api_token=[REDACTED]` — matching keys scrubbed; `row_count=42` untouched; `MaterializeResult.metadata` also scrubbed |
| 2 | `yaml_user_export` (YAML composability) | inner data-gen doesn't log anything sensitive → `redacted_count=0`, but the observation IS emitted every run (audit proof the redactor was active) |

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you already have Python code that logs sensitive fields:

```python
# src/<pkg>/defs/py_user_export.py
import dagster as dg
from dagster_community_components import sensitive

@dg.asset(group_name="python_decorator")
@sensitive(keys=["password", "*_token", "ssn"], strategy="redact")
def py_user_export(context):
    context.log.info(
        "user secrets — ssn=123-45-6789 password=hunter2 api_token=sk-abc123"
    )
    # → "user secrets — ssn=[REDACTED] password=[REDACTED] api_token=[REDACTED]"
    return dg.MaterializeResult(
        metadata={
            "ssn": "123-45-6789",      # → [REDACTED]
            "password": "hunter2",      # → [REDACTED]
            "row_count": 42,            # untouched
        }
    )
```

**Shape 2: YAML composability — the money shot.** `SensitiveAssetComponent` wraps **another DCC component**. The outer proxies the inner's `context.log` calls through the redactor and scrubs any `MaterializeResult.metadata` the inner returns — zero Python for this asset:

```yaml
# src/<pkg>/defs/yaml_user_export/defs.yaml
type: dagster_community_components.SensitiveAssetComponent
attributes:
  keys:
    - password
    - "*_token"
    - ssn
    - authorization
    - api_key
  strategy: redact
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_user_export
      schema_type: customers
      row_count: 10
      random_state: 42
```

**One asset is registered** (`yaml_user_export`) — no duplication. The outer `SensitiveAssetComponent` proxies the inner's context so any log call the inner makes flows through the redactor. Inner's config (partitions, deps, kinds, tags, group) all pass through unchanged.

Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `SensitiveAssetComponent { wraps: SnapshotAssetComponent { wraps: SnowflakeQueryComponent } }` = `@sensitive @snapshot @snowflake` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, decorator components stack over **any DCC component** with zero user Python — defensive infrastructure that runs on every materialization even when the inner has nothing sensitive to scrub:

- `SensitiveAssetComponent { wraps: RestApiFetcherComponent }` — API-key headers accidentally logged? Scrubbed.
- `SensitiveAssetComponent { wraps: SnowflakeQueryComponent }` — query results containing SSNs accidentally logged? Scrubbed.
- `SensitiveAssetComponent { wraps: LLMPromptExecutorComponent }` — model prompt with a bearer token accidentally logged? Scrubbed.
- `SensitiveAssetComponent { wraps: DataframeFromCsv }` — CSV field values accidentally logged? Scrubbed.
- `SensitiveAssetComponent { wraps: <ANY_COMPONENT> }` — per-asset SOC2 attestation regardless of what the inner does.

## Match rules

Each configured `key` is:

- A **case-insensitive glob** matched against dict keys (via `fnmatch`).
- A **substring pattern** matched inline against structured strings (`key=value`, `"key": "..."`, etc.) using a regex that catches `key=val`, `key="val"`, `key='val'`, and `key: val`.

Wildcards `*` and `?` supported. Defaults if unspecified:

```python
["password", "passwd", "secret", "*_secret",
 "token", "*_token", "api_key", "*_api_key",
 "ssn", "credit_card", "cvv", "authorization"]
```

## Redaction strategies

| Strategy | Result | Use when |
|---|---|---|
| `redact` (default) | `[REDACTED]` | You never want to see the value again |
| `hash` | `sha256:XXXXXXXX` (first 8 hex chars) | You want deterministic redaction so equal values hash to the same digest (useful for group-by) |
| `mask` | `***` + last 4 chars | Support / debug workflows where the last 4 chars help identify the record |

## Components used

| Component | What it does |
|---|---|
| `sensitive_asset` (`@sensitive` decorator + `SensitiveAssetComponent`) | Proxy `context.log.*` calls through a redactor that scrubs matching key patterns before the log line lands. Post-scrub `MaterializeResult.metadata` before it hits the event log. Emit `sensitive_redacted_count` observation every run. |
| `synthetic_data_generator` | Inner component the YAML shape wraps — generates realistic customer/order/event data for demos. |

## Why this belongs in Dagster

- **Event log = PII risk surface** — every `context.log.info` + every `MaterializeResult.metadata` dict ends up persisted. Wrapping compute stops bleed BEFORE persistence.
- **Same primitive, different surface** — Python + YAML users get identical behavior. Same observation events. Same sensor targets.
- **Composability at the component layer** — you can wrap ANY DCC component with the redactor without editing that component's config.
- **Per-asset SOC2 attestation** — audits benefit from scoped proof over a global logger config; every run leaves a `sensitive_redacted_count` observation.

## Known behavior — the regex is left-to-right

The inline `key=val` matcher is left-to-right greedy. `"field A: ssn=123-45-6789"` parses as `field A: ssn` (key=field, val="A: ssn"), leaving `=123-45-6789` unmatched. Rule of thumb: put sensitive `key=value` fragments at the **start** of the log message or separate them from label prefixes with anything other than `:` / `=`.

Working: `"user secrets — ssn=... password=... api_token=..."`
Broken: `"processing user record: ssn=... password=..."`

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_sensitive_asset_demo.sh | bash
cd sensitive-asset-demo
uv run dg dev
```

## Pair with a sensor — alert when redaction stops firing

Every run emits `sensitive_redacted_count`. If a run stops emitting the observation OR the count drops to zero unexpectedly, the redactor may have been silently disabled or the log format may have drifted past the regex.

```python
@dg.sensor(name="redactor_smoke_test")
def redactor_smoke_test(context):
    from dagster import DagsterEventType, EventRecordsFilter
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=100, ascending=False,
    )
    for r in recs[:10]:
        tags = (r.asset_observation.tags or {})
        if "sensitive_redacted_count" not in tags:
            # No redactor observation on recent runs — decorator may have been removed
            ...
```

## After the demo — inspect in the UI

```bash
cd sensitive-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click either asset → **Observations panel** shows `sensitive_redacted_count` tag + count metadata for every materialization.

## See also

- [`sensitive_asset` component reference](https://dagster-component-ui.vercel.app/c/sensitive_asset)
- [`snapshot_asset` walkthrough](snapshot_asset.md) — pair with `@sensitive` so snapshots don't capture secrets
- [`shadow_asset` walkthrough](shadow_asset.md) — same "wraps" pattern, dual-run + diff primitive
- [`throttle_asset` walkthrough](throttle_asset.md) — same "wraps" pattern, rate-limiting primitive
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
