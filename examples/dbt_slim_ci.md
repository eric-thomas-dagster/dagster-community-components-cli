# dbt Slim CI in Dagster — build only state-modified models on PR deploys

**The problem.** A customer has hundreds of dbt models. On every PR, they want to build only the models that changed vs prod (not all of them). On the morning schedule, they want a full build. Ad-hoc materializes work as usual.

**Three ways to solve this in Dagster.** Approach A and B work with the *stock* `dagster_dbt.DbtProjectComponent` — no subclass. Approach C is a tiny (~30-line) translator subclass that promotes "state:modified" to a first-class Dagster fact.

> ⭐ **Start with Approach A if you're picking one.** It's the least failure-prone option — zero CI plumbing, zero manifest storage to maintain, no prod-vs-current diff to keep in sync. Dagster's built-in automation-tick loop does the work using code_versions the dbt integration already sets on every asset. Approaches B and C exist for when you specifically need immediate-post-deploy triggering or "state:modified" surfaced elsewhere in Dagster.

| Approach | How | Trigger | Best for |
|---|---|---|---|
| **A. `AutomationCondition.code_version_changed()`** | Every dbt asset gets `code_version = SHA1(raw_sql)` automatically. Condition fires when hash changes vs the last materialization. | Dagster's automation-tick loop | Continuous prod — "always catch up if SQL changed and I forgot to trigger a build" |
| **B. Asset-selection at CI time** | GH Action runs `dbt ls --state ./prod --select state:modified+`, translates model names → Dagster asset keys, launches a job with `--asset-selection '[k1, k2, ...]'`. | GH Action after PR deploys | Slim CI validation — one-shot per PR, deterministic set, stock component |
| **C. State-aware translator (tag assets with `dbt/state`)** | Subclass `DagsterDbtTranslator` to read prod manifest at defs-load time + tag each asset `dbt/state=modified\|unchanged\|new`. Then anywhere in Dagster (launch, UI, automation conditions, sensors, alerts) can use `tag:dbt/state=modified`. | Any Dagster surface that accepts asset selections | When state:modified needs to be a first-class fact in Dagster — visible in the UI, combinable with owner/group/tag selections, usable in automation conditions |

They're complementary. Most teams use **A** as a safety net + **B or C** for the sharp PR-validation tool. C is more work upfront but gives the customer a Dagster-native primitive to build on.

---

## Prerequisites

- A Dagster project using the official `dagster_dbt.DbtProjectComponent` (either as a `defs.yaml` or via a Python instance).
- A GH Actions workflow scaffolded via `dg scaffold github-actions` (or your own — the additions below are drop-in).
- Some place to store prior-run manifests. Three options ranked easy → hard:
  1. **dbt Cloud** — pull latest prod manifest via their API (zero infra you own).
  2. **GitHub Actions artifacts** — GH-native, 90-day retention, zero infra.
  3. **S3 / GCS** — customer bucket, persistent, custom retention.

Examples below use option 2 (GH artifacts) because it's the easiest cross-cutting choice. Swapping to option 1 or 3 changes only the upload/download steps.

---

## Approach A — `AutomationCondition.code_version_changed()`  ⭐ *easiest, least failure-prone*

**How it works:** every dbt asset gets a `code_version` = SHA1 of its `raw_sql` (derived automatically by `dagster-dbt` — no config). When the SQL changes, the hash changes. `AutomationCondition.code_version_changed()` fires on the next automation tick, and Dagster kicks off a materialization for the changed assets.

**Why it's the safest pick:** zero CI plumbing, zero prior-manifest storage, zero cross-team coordination on artifact paths / adapter versions / dbt versions between the CI runner and the code-location image. The failure mode is just "the code location didn't reload" — which you'd see immediately in Dagster+ deploy logs anyway. Everything downstream is Dagster's automation-tick loop doing what it always does.

Wire it via `AutomationConditionApplicatorComponent`:

```yaml
# src/<pkg>/defs/dbt_automation/defs.yaml
type: dagster_community_components.AutomationConditionApplicatorComponent
attributes:
  preserve_existing: true
  rules:
    # For every dbt asset — rebuild when SQL changes since last materialization.
    # `on_missing()` handles the very first materialization (no prior code_version to compare).
    - name: dbt_rebuild_on_code_change
      selection: 'kind:dbt'
      python: dagster.AutomationCondition.code_version_changed() | dagster.AutomationCondition.on_missing()
```

Plus the `apply_rules(...)` wiring in `definitions.py` (see the applicator's README).

**When A alone isn't enough:** A is passive (waits for the next automation tick) and comparison-scoped-to-previous-materialization (not to prod). If you need immediate-post-deploy triggering OR a "vs prod" comparison surfaced elsewhere in Dagster, look at B or C below.

---

## Approach B — Asset-selection at CI time (stock component, no subclass)

**How it works:** the GH Action figures out which asset keys correspond to state-modified models (using dbt itself, on the CI runner). It then launches a Dagster job with `--asset-selection` narrowed to those specific keys. Dagster's dbt op sees `context.selected_asset_keys` = just those keys, and `dbt.cli(context=context)` auto-adds `--select` for them. dbt runs only the modified models.

**Why it works with the stock component:** the narrowing lives entirely at Dagster's job-selection layer. No custom cli_args, no subclass, no per-run config schema. Just leveraging what Dagster + dagster-dbt already do.

### Full GH Action snippet

Add to the workflow that `dg scaffold github-actions` generated. Assumes the workflow already sets `DAGSTER_PROJECT_DIR`; adjust if not.

```yaml
# ─── ON MAIN: produce + upload prod manifest as GH artifact ─────────────
- name: Install dbt-core + adapter (for manifest generation)
  if: github.ref == 'refs/heads/main'
  run: pip install dbt-core dbt-postgres    # swap adapter for your warehouse

- name: Compile prod manifest
  if: github.ref == 'refs/heads/main'
  working-directory: ${{ env.DAGSTER_PROJECT_DIR }}/dbt_project
  run: |
    dbt deps
    dbt parse                    # no warehouse creds needed for parse

- name: Upload prod manifest artifact
  if: github.ref == 'refs/heads/main'
  uses: actions/upload-artifact@v4
  with:
    name: prod-dbt-manifest
    path: ${{ env.DAGSTER_PROJECT_DIR }}/dbt_project/target/manifest.json
    retention-days: 90

# ─── ON PR: download prior main-branch manifest + compute modified asset keys ─
- name: Download prior prod manifest
  if: github.event_name == 'pull_request'
  uses: dawidd6/action-download-artifact@v6
  with:
    workflow: dagster-plus-deploy.yml
    branch: main
    name: prod-dbt-manifest
    path: ${{ env.DAGSTER_PROJECT_DIR }}/prod_dbt_state
    if_no_artifact_found: warn   # succeed even if main hasn't run yet

- name: Install dbt-core + adapter
  if: github.event_name == 'pull_request'
  run: pip install dbt-core dbt-postgres

- name: Enumerate state-modified dbt models
  if: github.event_name == 'pull_request'
  id: modified
  working-directory: ${{ env.DAGSTER_PROJECT_DIR }}/dbt_project
  run: |
    dbt deps
    # `dbt ls --output name` prints just the model name per line
    # (defaults to `<database>.<schema>.<name>` unless you tweak resource_type)
    modified=$(dbt ls \
      --state ../prod_dbt_state \
      --select state:modified+ \
      --output name \
      --resource-type model || true)
    echo "modified models:"
    echo "$modified"
    # Translate to Dagster asset keys (default translator = 1:1 with model name)
    keys=$(echo "$modified" | jq -R -s -c 'split("\n") | map(select(. != ""))')
    echo "keys=$keys" >> $GITHUB_OUTPUT
    # Count for logging + short-circuit
    count=$(echo "$modified" | grep -c . || true)
    echo "count=$count" >> $GITHUB_OUTPUT

# ─── DEPLOY (existing scaffold steps: build image, refresh defs state, etc.) ─
# ...   ← the workflow that `dg scaffold github-actions` created lives here

# ─── AFTER DEPLOY: launch the state-modified build ─────────────────────
- name: Launch Slim CI build (state-modified only)
  if: github.event_name == 'pull_request' && steps.modified.outputs.count != '0'
  run: |
    dagster-cloud job launch \
      --location my_location \
      --job dbt_full_asset_job \
      --asset-selection '${{ steps.modified.outputs.keys }}' \
      --tags '{"trigger": "slim-ci", "pr": "${{ github.event.pull_request.number }}"}'

- name: No-op message when nothing changed
  if: github.event_name == 'pull_request' && steps.modified.outputs.count == '0'
  run: echo "No dbt models modified vs prod. Skipping Slim CI build."
```

### The dagster side stays stock

```yaml
# src/<pkg>/defs/dbt/defs.yaml
type: dagster_dbt.DbtProjectComponent
attributes:
  project: "{{ project_root }}/dbt_project"
  # ... whatever the customer already has; no cli_args changes needed
```

No `--state` or `--select` in `cli_args`. Morning schedule + user materializes → full build.

### Model-name → Dagster asset key mapping

The default `DagsterDbtTranslator` maps dbt model names → Dagster asset keys 1:1 by default. So if `dbt ls` prints `orders_mart`, the Dagster asset key is `orders_mart`. Applies to seeds + snapshots too.

**If the customer has customized their translator** (e.g. `get_asset_key(self, props): return AssetKey(["marts", props["name"]])`), then the CI step must apply the same transformation before calling `job launch --asset-selection`. Simplest way: put the mapping logic in a tiny Python script called from the workflow.

---

## Approach C — State-aware translator (dbt state as a Dagster tag)

Promote `state:modified` from a dbt-CLI concept to a **first-class Dagster fact**. A tiny `DagsterDbtTranslator` subclass reads the prod manifest at defs-load time, hashes each model's raw_sql, and tags every dbt asset with `dbt/state=modified` (or `unchanged` / `new`). After that, any Dagster surface that speaks the selection query language can filter on it:

```bash
# Slim CI launch — same intent as Approach B, but Dagster-native
dagster-cloud job launch --asset-selection 'tag:dbt/state=modified'

# Combined with your own tag hygiene
dagster-cloud job launch --asset-selection 'tag:dbt/state=modified and owner:data-team'

# Or in an automation condition — auto-materialize any modified model on next tick
# (drop into automation_condition_applicator rules):
- selection: 'tag:dbt/state=modified'
  preset: eager
```

**The subclass** (drop into `src/<pkg>/lib/state_aware_translator.py`):

```python
"""State-aware dbt translator — tags each asset with `dbt/state` by comparing
the current manifest against a prod manifest baked into the deployed image.
"""
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from dagster_dbt import DagsterDbtTranslator


def _hash_raw(props: Mapping[str, Any]) -> str:
    code = props.get("raw_sql") or props.get("raw_code") or ""
    return hashlib.sha1(code.encode("utf-8")).hexdigest() if code else ""


class StateAwareDbtTranslator(DagsterDbtTranslator):
    """Tag each dbt asset with `dbt/state=modified|unchanged|new` based on
    a diff against a prior (prod) manifest.json.
    """

    def __init__(self, prod_manifest_path: str, **kwargs):
        super().__init__(**kwargs)
        self._prod_hashes: dict[str, str] = {}
        p = Path(prod_manifest_path)
        if p.exists():
            try:
                prod = json.loads(p.read_text())
                for uid, node in (prod.get("nodes") or {}).items():
                    if node.get("resource_type") in ("model", "seed", "snapshot"):
                        self._prod_hashes[uid] = _hash_raw(node)
            except Exception:
                # Degrade to "no prior manifest" — everything gets tagged `new`
                pass

    def get_tags(self, dbt_resource_props: Mapping[str, Any]) -> Mapping[str, str]:
        base = dict(super().get_tags(dbt_resource_props) or {})
        uid = dbt_resource_props.get("unique_id", "")
        if not self._prod_hashes:
            base["dbt/state"] = "new"     # no prior manifest = treat everything as new
        elif uid not in self._prod_hashes:
            base["dbt/state"] = "new"     # newly-added model
        elif self._prod_hashes[uid] != _hash_raw(dbt_resource_props):
            base["dbt/state"] = "modified"
        else:
            base["dbt/state"] = "unchanged"
        return base
```

**Wire it into the component:**

```yaml
# src/<pkg>/defs/dbt/defs.yaml
type: dagster_dbt.DbtProjectComponent
attributes:
  project: "{{ project_root }}/dbt_project"
  translation:
    type: <pkg>.lib.state_aware_translator.StateAwareDbtTranslator
    attributes:
      prod_manifest_path: "{{ project_root }}/prod_dbt_state/manifest.json"
```

**GH Action difference from Approach B:** the prod manifest has to be baked into the DEPLOYED code-location image, not just present on the CI runner. Change the download step to run BEFORE `dg plus deploy build-and-publish` so the file ships inside the image:

```yaml
- name: Download prior prod manifest
  if: github.event_name == 'pull_request'
  uses: dawidd6/action-download-artifact@v6
  with:
    workflow: dagster-plus-deploy.yml
    branch: main
    name: prod-dbt-manifest
    path: ${{ env.DAGSTER_PROJECT_DIR }}/prod_dbt_state
    if_no_artifact_found: warn

# Existing `dg plus deploy build-and-publish` step follows — the image now includes prod_dbt_state/
```

Also add `prod_dbt_state/` to whatever include list your Docker build uses (or ensure it's not in `.dockerignore`). Then on code-location load, the translator reads it, tags fire, and every downstream surface can filter on `tag:dbt/state=modified`.

**Trade-offs vs Approach B:**

| | Approach B (asset-selection at CI) | Approach C (translator tag) |
|---|---|---|
| Stock component? | Yes | No (30-line translator subclass) |
| Where the manifest lives at runtime | CI runner (dbt ls) | Inside the deployed code-location image |
| Visible in Dagster UI as "which assets changed?" | No — CI computes it once and forgets | Yes — every asset carries the tag |
| Usable in automation conditions? | No (would need re-enumeration each tick) | Yes — `selection: 'tag:dbt/state=modified'` |
| Combinable with owner/group/other tags? | Only via boolean at CI enumeration time | Yes — full selection expression at any Dagster surface |
| Complexity | Deploy plumbing only | Deploy plumbing + subclass file + `translation:` YAML |

**When to pick which:**
- **B** — one-shot Slim CI validation, don't need "state:modified" elsewhere in Dagster → simplest correct answer.
- **C** — want state:modified surfaced in the UI, combinable with your own selection syntax, usable in automation conditions / sensors / alerts → worth the small subclass investment.

---

## Alternate manifest-storage backends

The GH Action snippet above uses **GitHub artifacts**. Swap the upload/download steps for either:

### dbt Cloud (zero infra)

```yaml
- name: Fetch prod manifest from dbt Cloud
  if: github.event_name == 'pull_request'
  env:
    DBT_CLOUD_API_TOKEN: ${{ secrets.DBT_CLOUD_API_TOKEN }}
    DBT_CLOUD_ACCOUNT_ID: 12345
    DBT_CLOUD_PROD_JOB_ID: 67890
  run: |
    mkdir -p ${{ env.DAGSTER_PROJECT_DIR }}/prod_dbt_state
    # Get most recent successful run for the prod job
    run_id=$(curl -s \
      -H "Authorization: Token $DBT_CLOUD_API_TOKEN" \
      "https://cloud.getdbt.com/api/v2/accounts/$DBT_CLOUD_ACCOUNT_ID/runs/?job_definition_id=$DBT_CLOUD_PROD_JOB_ID&status=10&order_by=-finished_at&limit=1" \
      | jq -r '.data[0].id')
    # Download its manifest artifact
    curl -s -H "Authorization: Token $DBT_CLOUD_API_TOKEN" \
      "https://cloud.getdbt.com/api/v2/accounts/$DBT_CLOUD_ACCOUNT_ID/runs/$run_id/artifacts/manifest.json" \
      -o ${{ env.DAGSTER_PROJECT_DIR }}/prod_dbt_state/manifest.json
```

(No main-branch upload step needed — dbt Cloud already stores manifests for every prod run.)

### S3

```yaml
# On main:
- uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: arn:aws:iam::123456789012:role/GitHubActionsDeploy
    aws-region: us-east-1
- name: Upload prod manifest to S3
  if: github.ref == 'refs/heads/main'
  run: |
    aws s3 cp ${{ env.DAGSTER_PROJECT_DIR }}/dbt_project/target/manifest.json \
              s3://your-bucket/dbt/prod_manifest.json

# On PR:
- name: Download prod manifest from S3
  if: github.event_name == 'pull_request'
  run: |
    mkdir -p ${{ env.DAGSTER_PROJECT_DIR }}/prod_dbt_state
    aws s3 cp s3://your-bucket/dbt/prod_manifest.json \
              ${{ env.DAGSTER_PROJECT_DIR }}/prod_dbt_state/manifest.json
```

### GCS (same pattern with `gsutil` or `gcloud storage cp`)

---

## Common pitfalls

**1. First PR after main has never run.** No prod manifest exists yet — `dbt ls` fails or returns empty. The `if_no_artifact_found: warn` + `count != '0'` gate above handles this cleanly (first PR is a no-op; deploys ship; nothing to compare against).

**2. `dbt parse` needs `dbt deps` first.** If your project uses packages (99% do), `dbt deps` must run before `dbt parse` or `dbt ls`. Included in the snippets above.

**3. Custom translator asset keys.** If the customer overrode `get_asset_key(...)` on their translator, the model-name → key mapping in CI has to match. See the "Model-name → Dagster asset key mapping" note above.

**4. Downstream models on modified ones.** `state:modified+` (with the `+` suffix) includes DOWNSTREAM models of modified ones. That's usually what you want. If not, drop the `+`. If you want upstreams too (rare), use `+state:modified+`.

**5. Skipping the run if the deploy image doesn't have the changed asset keys yet.** Dagster+ deploys are atomic — the `job launch` call runs AFTER `refresh-defs-state`, so the code location knows about the new model. But if you're on Hybrid with a lagging agent, add a small `sleep 30` after refresh or poll for readiness.

**6. Job name matches your actual dbt job.** The example uses `dbt_full_asset_job`. Your project may not have an asset job that includes all dbt models — check `dg list defs` for the actual job name, or create one:
```python
# definitions.py — a job that includes all dbt assets
from dagster import define_asset_job, AssetSelection
dbt_full_asset_job = define_asset_job("dbt_full_asset_job", selection=AssetSelection.groups("dbt"))
```

**7. `dbt-core` version compatibility.** Whatever version you install on the CI runner should be compatible with the manifest schema your prod deploy produces. If prod runs dbt 1.9 and CI installs 1.7, `dbt ls --state` may reject the manifest as newer-schema.

---

## When to reach for the subclass component instead

The above uses the stock `DbtProjectComponent`. Skip it if you need any of:

- Per-run `--vars` (per-model config injected by a sensor) — see [`dbt_queue_driven.md`](./dbt_queue_driven.md) for the subclass pattern
- Per-run `--defer --state ./prod` for dev iteration (borrow missing upstreams from prod) — same subclass exposes `state_path` in op_config
- Per-run `--full-refresh` toggle — trivial addition to the same subclass

For pure "run only state-modified on PR deploys" — you don't need any of that. Stock component + the CI narrowing above is the simplest correct answer.

---

## Related

- [`dbt_queue_driven.md`](./dbt_queue_driven.md) — message-driven dbt orchestration + subclass component pattern (`DbtProjectWithRuntimeVarsComponent`)
- [`automation_condition_applicator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/automation_condition_applicator) — the component wiring for Approach B
- [Official `dagster-dbt` docs](https://docs.dagster.io/integrations/libraries/dbt) — DbtProjectComponent, translator, cli_args reference
- [dbt state selection docs](https://docs.getdbt.com/reference/node-selection/methods#state) — the `state:modified` / `state:modified+` selector reference
