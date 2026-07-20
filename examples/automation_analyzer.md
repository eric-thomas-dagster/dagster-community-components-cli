# `dagster-analyze-schedules` — imperative → declarative migration tool

Companion to [`automation_condition_applicator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/automation_condition_applicator). Reads an existing Dagster project's schedules + jobs and emits a proposed `AutomationConditionApplicatorComponent` YAML block — plus a plan of which schedules to disable and which jobs to keep manual/sensor-only.

**Right for:** teams migrating from imperative Dagster scheduling (`ScheduleDefinition` + `define_asset_job`) to declarative automation conditions.

## Install + run — no repo clone needed

```bash
# Run against your project (any dir containing a canonical create-dagster
# layout with src/<pkg>/definitions.py):

uvx --from dagster-community-components-cli dagster-analyze-schedules --project-dir ./my_project
```

Or as a `dagster-component` subcommand if you already have the CLI installed:

```bash
uvx --from dagster-community-components-cli dagster-component analyze-schedules --project-dir ./my_project
```

Or just run it from inside your project's dir:

```bash
cd my_project
uvx --from dagster-community-components-cli dagster-analyze-schedules
```

Requirements: [uv](https://docs.astral.sh/uv/). Runs against the target project's own venv (via `uv run --project`), so your project's Dagster version is what gets introspected.

## What you get

Two artifacts:

1. **`automation_conditions_proposal.yaml`** — a drop-in rules block for `AutomationConditionApplicatorComponent`. Review it, then wire it into your project via `apply_rules(defs, load_yaml("./automation_conditions_proposal.yaml"))` in `definitions.py`.

2. **A stdout report** — an itemized plan:
   - Which schedules can be disabled + which rule replaces each
   - Which schedules need manual review (weird cadence, no resolvable asset selection, etc.)
   - Which jobs to keep manual/sensor-only
   - Which assets don't fit any rule (usually a sign of missing group/tag hygiene)

## Example output

Given a project with 3 schedules (`daily_bronze` @ 6am, `daily_marts` @ 6am, `hourly_events` @ 0 * * * *) driving 3 jobs across 8 assets in `bronze`/`silver`/`gold` groups:

```yaml
type: dagster_community_components.AutomationConditionApplicatorComponent
attributes:
  preserve_existing: true
  rules:
    # replaces 1 schedule(s): hourly_events
    - name: hourly_top_of_hour
      selection: 'key:"raw_events"'
      cron: '0 * * * *'

    # replaces 2 schedule(s): daily_bronze, daily_marts
    - name: daily_at_6am
      selection: 'key:"customers_mart" or key:"orders_mart" or key:"raw_customers" or key:"raw_orders"'
      cron: '0 6 * * *'

    # downstream groups (gold, silver) inherit their cadence from upstreams
    - name: derive_downstream_from_upstreams
      selection: 'group:gold or group:silver'
      derive_from_upstreams: true
      strategy: most_frequent
```

And the report:

```
Inventory:  8 assets · 3 schedules · 4 jobs · 0 sensors

✓ Proposed rules (3):
    • hourly_top_of_hour  (key:"raw_events")
    • daily_at_6am         (4 raw + mart keys)
    • derive_downstream_from_upstreams  (group:gold or group:silver)

✓ Schedules to disable AFTER applying the rules (3):
    • hourly_events  → covered by 'hourly_top_of_hour'
    • daily_bronze   → covered by 'daily_at_6am'
    • daily_marts    → covered by 'daily_at_6am'  (merged — same cron)

✓ Jobs to keep manual/sensor-only (1):
    • manual_backfill_job  (trigger=manual)
```

Notice: **two schedules that fire the same cron get MERGED into one rule** (`daily_at_6am`). This is a common cleanup the analyzer does automatically.

## Heuristics the analyzer uses

- **Same-cron schedules → single rule.** If N schedules all fire `0 6 * * *`, their target-job asset selections union into one rule with `cron: "0 6 * * *"`.
- **Cron name → human-friendly.** `0 * * * *` → `hourly_top_of_hour`; `0 6 * * *` → `daily_at_6am`; `*/15 * * * *` → `every_15_minutes`; falls back to `cron_<slug>` for exotic expressions.
- **Downstream groups auto-derived.** Assets in groups named `silver`, `gold`, `mart`/`marts`, `prod`, `warehouse` (and not already covered by a cron rule) get a `derive_from_upstreams: true, strategy: most_frequent` rule — matching the common medallion pattern.
- **Uncovered assets → eager catchall.** If any asset isn't matched by an explicit rule, an `eager_default` rule (`selection: "*"`, `preset: eager`) is appended so nothing gets left un-conditioned.
- **`preserve_existing: true` by default.** Assets that already have an `automation_condition` set keep it. The applicator only fills in the gaps.

## Wiring the output into your project

The analyzer writes to `automation_conditions_proposal.yaml` by default (in the project root). Two ways to consume it:

### Option 1 — drop as a `defs.yaml` (YAML-native)

Move it to `src/<your_pkg>/defs/automation_conditions/defs.yaml`. `dg`'s autoloader picks it up. Also add the `apply_rules(...)` call to `definitions.py` (the applicator requires a project-level wiring step — see [its README](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/automation_condition_applicator) for details).

### Option 2 — load from Python

```python
# definitions.py
from pathlib import Path
import yaml
from dagster import definitions, load_from_defs_folder
from dagster_community_components import apply_rules

@definitions
def defs():
    base = load_from_defs_folder(path_within_project=Path(__file__).parent)
    rules_block = yaml.safe_load(Path("automation_conditions_proposal.yaml").read_text())
    return apply_rules(base, rules=rules_block["attributes"]["rules"])
```

## After applying — disable the old schedules

The proposal replaces your schedules but doesn't remove them. Once the rules are live and verified:

1. Delete the `ScheduleDefinition` objects from `definitions.py`
2. Delete the `schedules=[...]` argument to `Definitions(...)`
3. Optionally, delete `define_asset_job(...)` calls if the jobs were only there as schedule targets

Jobs called out as "keep manual/sensor-only" stay put — those aren't managed by automation conditions.

## Options

```
--project-dir, -p    Path to the Dagster project (default: current dir)
--output, -o         Output YAML path (default: automation_conditions_proposal.yaml)
--stdout             Print YAML to stdout instead of writing a file
```

## Limitations

- Only reads projects with a canonical `create-dagster` layout (`src/<pkg>/definitions.py` with `@definitions` factory named `defs`). Older / custom layouts may not introspect cleanly — file an issue with the project shape.
- Doesn't yet handle **partitioned schedules** — those are flagged for manual review.
- Downstream-group detection is name-based (`silver`, `gold`, `mart`, etc.). If your project uses different group names, the tool won't auto-generate the `derive_from_upstreams` rule — you can hand-add one.
- The applicator's `python:` escape hatch isn't proposed — the tool sticks to YAML-expressible rules.

## Related

- [`automation_condition_applicator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/automation_condition_applicator) — the target component this tool generates YAML for
- [Dagster automation conditions docs](https://docs.dagster.io/concepts/automation/declarative-automation)
