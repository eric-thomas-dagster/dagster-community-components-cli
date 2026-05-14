# Broadly applying automation conditions

How to set Dagster `AutomationCondition`s across many assets at once — without editing every `defs.yaml`. With fall-through priority (narrow > broad), preserve-existing (per-asset > rules), and auto-derive from upstream cadences.

> **Validation status:** validated end-to-end 2026-05-13. Scaffolded a project with 4 test assets (3 upstreams of different cadences + 1 silver derived asset). Ran the applicator with `derive_from_upstreams: most_frequent` + catchall `eager`. Confirmed the silver asset got `on_cron("0 * * * *").ignore([daily_upstream])` (the hourly cadence) automatically; siblings got `eager`. `dg check defs` clean.

## Why this exists

Dagster lets you set ONE `automation_condition` per asset. Customers with hundreds of assets typically want:

1. **Set broadly**: "Everything in `group:gold` runs daily at 9am"
2. **Override narrowly**: "But `critical_metrics` runs every 5 minutes"
3. **Handle the mixed-cadence case**: "This asset has 3 daily upstreams + 1 monthly — fire daily, but on the 1st also wait for the monthly"

Without this component, you edit every asset's `defs.yaml` to set its automation_condition. That's brittle at 100+ assets — drift sets in fast.

## Architecture

```
   ┌────────────────────────────────────────────────────────┐
   │ defs/ folder — your existing components                │
   │   bronze/daily_upstream    (cadence=daily)             │
   │   bronze/hourly_upstream   (cadence=hourly)            │
   │   bronze/monthly_upstream  (cadence=monthly)           │
   │   silver/revenue_marts     (deps on bronze/*)          │
   │   gold/exec_dashboard      (deps on silver/*)          │
   └─────────────────────────┬──────────────────────────────┘
                             │ load_from_defs_folder
                             ▼
   ┌────────────────────────────────────────────────────────┐
   │ definitions.py wraps with apply_rules(...)             │
   │   Rules evaluated TOP-TO-BOTTOM, first-match-wins:     │
   │     - critical-tag → on_cron("0 * * * *")              │
   │     - group:silver → derive_from_upstreams (most_freq) │
   │     - group:gold → on_cron("0 9 * * *")                │
   │     - *           → eager                              │
   └─────────────────────────┬──────────────────────────────┘
                             │
                             ▼
   ┌────────────────────────────────────────────────────────┐
   │ Each asset gets exactly ONE final automation_condition │
   │ - Preserves any explicitly-set per-asset condition     │
   │ - First-match-wins fall-through                        │
   └────────────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `automation_condition_applicator` | community | Applies rules to a `Definitions` at project load time — the only component this walkthrough needs |

## Full surface — every rule shape

The component exposes six derive strategies, three escape-hatch shapes (cron / preset / python), and three modifiers. Pick exactly one **shape** per rule + optional **modifiers**:

| Shape | YAML | What it produces |
|---|---|---|
| **Explicit cron** | `cron: "0 9 * * *"` (+ optional `ignore_selection` / `allow_selection`) | `on_cron(cron)` with optional dep filtering |
| **Preset** | `preset: eager` (or `on_missing` / `any_downstream_conditions` / any zero-arg `AutomationCondition.*` factory) | The named factory's return value |
| **Derive: most_frequent** | `derive_from_upstreams: true, strategy: most_frequent` | Fire on fastest upstream's cron, ignore slower deps |
| **Derive: least_frequent** | `derive_from_upstreams: true, strategy: least_frequent` | Fire on slowest upstream's cron, require all deps |
| **Derive: tiered** | `derive_from_upstreams: true, strategy: tiered` | Fire fastest + per-tier allow-gated wait (the "daily-with-monthly-boundary" pattern) |
| **Derive: staggered** | `derive_from_upstreams: true, strategy: staggered, offset_minutes: 60` | Like most_frequent but cron shifted by N min |
| **Derive: any_dep_updated** | `derive_from_upstreams: true, strategy: any_dep_updated` | No cron — fire whenever ANY upstream updates |
| **Derive: all_deps_updated** | `derive_from_upstreams: true, strategy: all_deps_updated` | No cron — wait until ALL upstreams refreshed |
| **Python escape hatch** | `python: "module.path:function_name"` | Calls user's function returning `AutomationCondition` |

Modifiers (compose on top of any cron-producing shape):

| Modifier | YAML | Effect |
|---|---|---|
| **Label override** | `label: "my_custom_label"` | Replace auto-label with explicit name (UI clarity) |
| **Business hours only** | `business_hours_only: true` + optional `business_hours: "9-17"` / `business_days: "1-5"` (both follow cron syntax) | Constrain cron to working hours/days. Refuses for tiered + no-cron strategies |
| **Min interval floor** | `min_interval_minutes: 60` | Debounce: replace cron if its period is faster than the floor. Picks sane round crons |

### Business hours — defaults and overrides

The defaults are US-style 9-5 weekdays:

```yaml
business_hours_only: true        # implies business_hours: "9-17", business_days: "1-5"
```

Both fields follow cron syntax — override for any region/schedule:

```yaml
# Retail dashboards — 8am-6pm Mon-Sat
business_hours_only: true
business_hours: "8-18"
business_days: "1-6"

# Always-on (24/7) — degenerate case, equivalent to no constraint
business_hours_only: true
business_hours: "0-23"
business_days: "*"

# Specific hour-of-day list — only run at 9am, noon, 3pm on weekdays
business_hours_only: true
business_hours: "9,12,15"
business_days: "1-5"
```

**Timezone caveat**: cron expressions evaluate in the Dagster instance's timezone (usually UTC). If your team is in PST and you want 9am-5pm PST, the UTC equivalent is 17:00-01:00 — which wraps midnight and isn't expressible as a single cron range. Options:
1. Run Dagster in your local TZ (simplest)
2. Adjust to UTC manually (`business_hours: "17-23"` covers 9am-3pm PST only)
3. Use the `python` escape hatch with `pytz`/`zoneinfo` for timezone math

### Python escape hatch — the universal answer

For anything YAML can't express (e.g. the `@automation_condition` decorator from [Dagster's arbitrary-Python docs](https://docs.dagster.io/guides/automate/declarative-automation/customizing-automation-conditions/arbitrary-python-automation-conditions)):

```python
# my_project/custom_conditions.py
import dagster as dg

def hourly_when_any_dep_updates() -> dg.AutomationCondition:
    return (
        dg.AutomationCondition.in_latest_time_window()
        & dg.AutomationCondition.cron_tick_passed("0 * * * *").since_last_handled()
        & dg.AutomationCondition.any_deps_updated()
    ).with_label("hourly_when_any_dep_updates")
```

```yaml
# defs/automation/defs.yaml
rules:
  - selection: "tag:cadence=event_driven"
    python: "my_project.custom_conditions:hourly_when_any_dep_updates"
```

The function can take zero args, or one arg (the asset spec — for per-asset customization).

## The "3 daily + 1 monthly upstream" pattern (the customer ask)

Customer Jay's exact problem: *"I have an asset with 3 daily upstreams and 1 monthly upstream. I want it to fire daily, ignoring the monthly. But on month boundaries I want both."*

The Dagster-skill answer is correct: compose `on_cron(DAILY).ignore(monthly)` + `all_deps_updated_since_cron(MONTHLY).allow(monthly)`. But writing that manually across 50 assets is painful. The applicator handles it three ways:

### Way 1 — Auto-derive from upstreams (the simplest)

```yaml
- selection: "group:silver"
  derive_from_upstreams: true
  strategy: most_frequent     # fire on the fastest upstream's cron; ignore slower
```

The applicator walks each silver asset's deps, finds their cron schedules (from `automation_condition.cron_schedule` or `freshness_policy.cron_schedule` or `metadata["cron_schedule"]`), picks the most frequent, and generates `on_cron(daily).ignore(<monthly_upstream_keys>)`.

**Result for the 3-daily + 1-monthly case**: `on_cron("0 9 * * *").ignore(monthly_upstream)`. The asset fires daily without blocking on the monthly.

### Way 2 — Tiered strategy (mixed-cadence with month-boundary gate)

The customer follow-up: *"what if I want daily when the dailies finish, but monthly when daily AND monthly finish?"* — fire on the daily cadence with the daily deps; on the month boundary, also wait for the monthly. The `tiered` strategy generates this exact pattern automatically:

```yaml
- selection: "group:silver"
  derive_from_upstreams: true
  strategy: tiered           # buckets upstreams by cron, ANDs them
```

For an asset with 3 daily upstreams + 1 monthly upstream, the applicator generates the equivalent of:

```python
# What the applicator hand-builds for you:
condition = (
    dg.AutomationCondition.in_latest_time_window()
    & dg.AutomationCondition.cron_tick_passed("0 9 * * *").since_last_handled()
    & dg.AutomationCondition.all_deps_updated_since_cron("0 9 * * *").ignore(monthly_keys)
    & dg.AutomationCondition.all_deps_updated_since_cron("0 9 1 * *").allow(monthly_keys)
).with_label("tiered_on_cron(0 9 * * *)")
```

**Mid-month evaluation** (e.g., Jan 15 9:30am): MONTHLY's most-recent tick is Jan 1 9am, monthly upstream already updated then → monthly clause is True. Daily deps fresh since today's 9am → asset fires.

**Month boundary** (Feb 1 9:30am): the MONTHLY tick just rolled to Feb 1 9am — the monthly clause now demands monthly fresh since Feb 1 9am. Daily asset blocks until the monthly lands, then both fire together.

**N-tier generalization**: works for any number of cron buckets (hourly + daily + weekly + monthly, etc.). Each tier gets its own `all_deps_updated_since_cron(...).allow(<tier_keys>)`. Single-tier degenerate case (all upstreams share one cron) falls back to plain `on_cron`.

**Strategy comparison**:

| Strategy | What it does | Use when |
|---|---|---|
| `most_frequent` | Fire on fastest upstream; **ignore** slower tiers entirely | Slow deps are nice-to-have but shouldn't block |
| `least_frequent` | Fire on slowest upstream; require **all** deps | Downstream must see every dep's update |
| `tiered` | Fire on fastest, **gate by each tier on its own cron boundary** | Want the daily/weekly/monthly heartbeat with boundary-aligned waits |

### Way 2b — Two-rule AND via raw `cron` + `ignore_selection` / `allow_selection`

Manual control over the cron strings (e.g., monthly tick set conservatively):

```yaml
rules:
  - name: silver_daily_skeleton
    selection: "group:silver"
    cron: "0 9 * * *"
    ignore_selection: "tag:cadence=monthly"

  # Same selection — Dagster ANDs them via composition implicit in fall-through
  # (For TRUE composition you write a custom Python rule; see below.)
```

Use `tiered` for auto-derivation; use this when you need explicit cron control.

### Cadence propagation through deeper chains

The applicator processes assets in **topological order** so derived cadences flow downstream:

```
A (root, metadata.cron_schedule: daily)        → effective: daily
B (root, metadata.cron_schedule: monthly)      → effective: monthly
C (deps: [A, B]; rule: least_frequent)         → effective: monthly  ← bound by B
D (deps: [C]; rule: derive most_frequent)      → effective: monthly  ← inherits C's effective cadence
```

Without topological propagation, D would see C as having no cron (C's automation_condition wasn't applied yet when D was processed) and fall through. The applicator gets this right.

### Way 3 — Custom Python (full Dagster power)

For complex conditions the YAML can't express:

```python
import dagster as dg
from dagster_community_components import apply_rules

DAILY = "0 9 * * *"
MONTHLY = "0 9 1 * *"

monthly_sel = dg.AssetSelection.tag("cadence", "monthly")

complex_cond = (
    dg.AutomationCondition.in_latest_time_window()
    & dg.AutomationCondition.cron_tick_passed(DAILY).since_last_handled()
    & dg.AutomationCondition.all_deps_updated_since_cron(DAILY).ignore(monthly_sel)
    & dg.AutomationCondition.all_deps_updated_since_cron(MONTHLY).allow(monthly_sel)
).with_label("daily_with_monthly_boundary")

@definitions
def defs():
    base = load_from_defs_folder(...)
    # Apply the complex condition to silver, eager to the rest
    base = base.map_asset_specs(
        func=lambda spec: spec.replace_attributes(automation_condition=complex_cond)
                          if "silver" in spec.group_name else spec,
    )
    return apply_rules(base, rules=[{"selection": "*", "preset": "eager"}])
```

The applicator stays out of your way when you need full control.

## Fall-through priority — like CSS specificity

```yaml
rules:
  # 1. Narrowest first — specific tag wins for matching assets
  - selection: "tag:cadence=hourly"
    cron: "0 * * * *"

  # 2. Group-scoped — wins for silver assets that didn't match above
  - selection: "group:silver"
    derive_from_upstreams: true

  # 3. Catchall last — wins only if nothing above matched
  - selection: "*"
    preset: eager
```

`preserve_existing: true` (default) acts like CSS `!important` on per-asset settings: if an asset already has `automation_condition` set explicitly in its own component config, no rule overrides it. **Per-asset always wins.**

## Wiring it in (`definitions.py`)

Components in Dagster can't post-process other components' output via `build_defs` alone. You wire the applicator at the project level:

```python
from pathlib import Path
from dagster import definitions, load_from_defs_folder
from dagster_community_components import apply_rules

@definitions
def defs():
    base = load_from_defs_folder(path_within_project=Path(__file__).parent)
    return apply_rules(
        base,
        rules=[
            {
                "name": "critical_hourly",
                "selection": "tag:cadence=hourly",
                "cron": "0 * * * *",
            },
            {
                "name": "silver_from_upstreams",
                "selection": "group:silver",
                "derive_from_upstreams": True,
                "strategy": "most_frequent",
            },
            {
                "name": "gold_daily_morning",
                "selection": "group:gold",
                "cron": "0 9 * * *",
                "ignore_selection": "tag:cadence=monthly",
            },
            {"name": "catchall_eager", "selection": "*", "preset": "eager"},
        ],
        preserve_existing=True,
    )
```

That's the entire wiring. No defs.yaml changes needed.

## Validation script

To confirm what got applied to each asset:

```python
from your_project.definitions import defs

resolved = defs()
for spec in resolved.resolve_all_asset_specs():
    print(f"{spec.key.to_user_string():30s} → {spec.automation_condition}")
```

You'll see for each asset:
- `daily_upstream` → `eager(...)` (matched catchall)
- `hourly_upstream` → `on_cron("0 * * * *")` (matched tag rule)
- `silver_revenue` → `on_cron(<derived>).ignore(<slower_deps>)` (derive mode)
- `gold_exec` → `on_cron("0 9 * * *").ignore(monthly_*)` (group rule)

If the result isn't what you expected, fall-through ordering is the most common cause — re-order rules.

## Customer scenarios

### Scenario A — Brownfield "we have 200 assets, want consistent automation"

```yaml
rules:
  # Pre-existing critical assets with explicit settings stay (preserve_existing: true)
  # Add tag-based overrides for special cases:
  - selection: "tag:tier=realtime"
    cron: "*/5 * * * *"
  # Everything else → eager (default for new projects)
  - selection: "*"
    preset: eager
```

Onboarding: tag a handful of special assets, drop in the applicator. Done.

### Scenario B — Greenfield medallion architecture

```yaml
rules:
  - selection: "group:bronze"
    preset: eager        # bronze gets refreshed eagerly (raw ingestion)
  - selection: "group:silver"
    derive_from_upstreams: true
    strategy: most_frequent
  - selection: "group:gold"
    cron: "0 9 * * *"     # gold runs daily at 9am
  - selection: "*"
    preset: eager
```

Bronze hot-keeps, silver follows upstream cadence, gold publishes on a fixed schedule.

### Scenario C — Per-team SLAs via tags

```yaml
rules:
  - selection: "tag:team=marketing and tag:sla=realtime"
    cron: "*/15 * * * *"
  - selection: "tag:team=marketing"
    cron: "0 6,12,18 * * *"
  - selection: "tag:team=finance"
    cron: "0 6 * * *"
  - selection: "*"
    preset: eager
```

Teams own their tags; SLAs translate to cron without code changes per asset.

## Trade-offs & gotchas

- **`Definitions.map_asset_specs` is in preview** (Dagster ~1.10). The component uses it; you'll see preview warnings. Not blocking, but be aware.
- **Selection ordering matters.** First match wins. If `"*"` is first, nothing else fires.
- **`derive_from_upstreams` needs cron metadata on upstreams.** If your bronze layer doesn't expose cron schedules in `metadata` or `freshness_policy`, the derive mode falls through (returns None). Tag upstreams with `metadata: {cron_schedule: "..."}` for the applicator to find them.
- **Composition limits.** YAML rules use fall-through (first-match-wins), not AND-composition. For ANDed conditions across rules, use the Python escape hatch.
- **Per-asset wins.** If you find a rule isn't applying, check whether the asset's own component set `automation_condition:` — that always overrides rules unless you flip `preserve_existing: false`.

## See also

- `asset_job` — bundle assets into a stable job
- `cron_schedule` — fixed-cadence schedule (alternative to AutomationCondition)
- [Dagster AutomationCondition docs](https://docs.dagster.io/concepts/automation/declarative-automation)
- [Component README](https://dagster-component-ui.vercel.app/c/automation_condition_applicator)
