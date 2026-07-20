# dbt Slim CI in Dagster — build only state-modified models on PR deploys

**The problem.** A customer has hundreds of dbt models. On every PR, they want to build only the models that changed vs prod (not all of them). On the morning schedule, they want a full build. Ad-hoc materializes work as usual.

**Two ways to solve this in Dagster.** Both work with the *stock* official `dagster_dbt.DbtProjectComponent` — no custom subclass required.

| Approach | When it fires | Trigger source | Best for |
|---|---|---|---|
| **A. Asset-selection at CI time** | Once, right after the PR deploys | GH Action calls `dagster-cloud job launch --asset-selection [...]` | Slim CI validation (one shot per PR, deterministic set) |
| **B. `AutomationCondition.code_version_changed()`** | On the next automation tick after any deploy that changed a model's SQL | Dagster's automation-tick loop | Continuous production — auto-rebuild whenever SQL ships to prod |

They're complementary. Most teams use **A** for PR validation and **B** for the "production catches up if someone forgot to trigger a build" safety net.

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

## Approach A — Asset-selection at CI time (primary recommendation)

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

## Approach B — `AutomationCondition.code_version_changed()`

**How it works:** every dbt asset gets a `code_version` = SHA1 of its `raw_sql` (derived automatically by `dagster-dbt` — no config). When the SQL changes, the hash changes. `AutomationCondition.code_version_changed()` fires on the next automation tick, and Dagster kicks off a materialization for the changed assets.

**Why it's complementary:** works without any CI plumbing. As long as the deploy successfully reloads the code location (Dagster reads the new manifest), the automation-tick loop picks up changed models and materializes them. Slower than approach A (fires on the next tick, not immediately post-deploy) but no CI dependency.

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

**Comparing the two:**

- **Approach A** fires ONCE per PR deploy, immediately, at CI's command. Deterministic set. Best for "validate this PR built the models it claims to change."
- **Approach B** fires on the NEXT automation tick after deploy. Also fires if a model changes for any other reason (rerun, backfill, direct code-location update). Best for "production always catches up automatically."

Most teams want both. A is the sharp tool for CI validation; B is the safety net.

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
