# Agentic Router — Plain Dagster (No Components, No YAML)
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

The **same asset graph** as [`agentic_router.md`](agentic_router.md), rebuilt in **raw Dagster Python** — no components, no YAML, no framework layer. Same runtime behavior, same UI, same partition scheme, same sensor, same gate.

The only differences are **authoring surface** and **cost of adding a second one**. This walkthrough is the reference for that conversation.

Three plain-Python shapes to compare:

- **Variant A — `@graph_asset` with `@op` steps.** The ReAct loop is 5 pre-declared `plan_step_1..plan_step_5` ops. Each step is a visible box in the run UI with its own logs. Steps serialize between each other through the IO manager.
- **Variant B — plain `@asset` with in-body `for` loop.** The ReAct loop is a normal Python loop inside the asset's compute function. Iteration count is dynamic (loop breaks when the planner says done). One box in the run UI; the loop shows up as sequential lines in that step's logs.
- **Variant C — the minimum: unpartitioned, sequential, closest to Prefect.** No per-case partitions. No fan-out. No ops. One `@dg.asset` that loops through every case sequentially. Three unpartitioned filter assets per branch. **This is the shape a Prefect user would recognize most naturally** — Prefect doesn't have partitions as a first-class concept, so this is the closest 1:1 comparison to `@flow` + `@task` in a for-loop.

Variants A and B are per-case partitioned (each case a separate materialization). Variant C is a single batch materialization with all cases inside. Different tradeoffs for different tastes.

## Run

**Variant A** (ops-based):
```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_router_plain_demo.sh \
  -o setup_agentic_router_plain_demo.sh
bash setup_agentic_router_plain_demo.sh
```

**Variant B** (plain @asset, no ops):
```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_router_plain_simple_demo.sh \
  -o setup_agentic_router_plain_simple_demo.sh
bash setup_agentic_router_plain_simple_demo.sh
```

**Variant C** (minimum — unpartitioned, sequential, closest to Prefect):
```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_router_plain_minimal_demo.sh \
  -o setup_agentic_router_plain_minimal_demo.sh
bash setup_agentic_router_plain_minimal_demo.sh
```

Requirements: `uv`, `OPENAI_API_KEY`. ~1 min first run each.

## Variant C — the Prefect-comparable minimum

Prefect (2 and 3) doesn't have partitions as a first-class concept — you'd pass parameters to flow runs or use `.map()` on tasks. So this Dagster shape is the closest 1:1:

- **No partitions.** One materialization handles all cases.
- **No fan-out.** Sequential Python `for` loop inside a single `@dg.asset`.
- **No ops.** Just chained `@dg.asset`s.
- **No gate.** No sensor. No human-in-the-loop.
- **Router asset** loops over the source DataFrame, calls a plain Python `triage_one_case()` per row, returns a batch DataFrame.
- **Branch assets** are unpartitioned filter `@dg.asset`s that select rows where their branch was picked.
- **Sink asset** aggregates all three branches and appends to a DuckDB audit table.

Prefect analog:

| Prefect | Dagster equivalent (Variant C) |
|---|---|
| `@flow` | `@dg.asset` (chained, unpartitioned) |
| `@task` called in a loop | Python for-loop inside the `@dg.asset` compute |
| Prefect result store | Dagster IO manager (default filesystem) |
| Flow run history | Materialization history (per asset) |
| Flow parameters | would be Dagster config or partition — omitted here |

**What you still get from Dagster in this stripped-down shape:**
- Every stage in the asset catalog with lineage
- Materialization history queryable per asset
- Downstream fan-out via normal `@asset` deps (each branch is a real graph node)
- `dg dev` UI graphs the pipeline out of the box
- Every primitive here is a stepping stone to add features on top

**What you're intentionally not using:**
- Per-case partitions (add later for per-case addressability)
- DynamicOut fan-out (add later for parallelism — see [`agentic_batch_triage.md`](agentic_batch_triage.md))
- Ops + graph_asset (add later for step-level visibility — Variant A above)
- Human gate + sensor (add later for approval flows — see [`agentic_router.md`](agentic_router.md))

The point of Variant C is: **this is what Dagster looks like before you turn on the features**. Every subsequent variant adds one thing on top:

```
Variant C (minimum)  +DynamicOut fanout        = agentic_batch_triage plain
Variant C (minimum)  +partitions per case      = Variant B
Variant C (minimum)  +ops + graph_asset        = Variant A
Variant C (minimum)  +human gate + sensor      = agentic_router.md
```

Show Variant C to a Prefect user first. Then show what each Dagster feature costs to add and what it buys.

## Project layout

```
agentic-router-plain-demo/
├── data/
│   ├── baggage.duckdb           ← the "legacy" baggage tracking DB
│   └── baggage_reports.csv
├── approvals/                   ← human token drop dir
├── courier_bookings/            ← delivery_request → CSV per case
├── compensations/               ← voucher_issued → CSV per case
├── audit/case_audit.duckdb      ← escalation audit warehouse
└── src/agentic_router_plain_demo/
    ├── definitions.py           ← 12 lines: autoloader + DuckDBResource
    └── defs/
        └── assets.py            ← EVERYTHING — 250 lines
```

`definitions.py` (all 12 lines):

```python
from pathlib import Path
from dagster import Definitions, definitions, load_from_defs_folder
from dagster_duckdb import DuckDBResource
from .defs import assets as A

@definitions
def defs():
    return Definitions.merge(
        load_from_defs_folder(path_within_project=Path(__file__).parent),
        Definitions(resources={"baggage_db": DuckDBResource(database=A.BAGGAGE_DB)}),
    )
```

Everything else is in `defs/assets.py` — the autoloader picks it up.

## What lives in `defs/assets.py`

Ordered by how they appear:

1. **Config constants** — paths, model name, MAX_ITER, CASES, BRANCHES.
2. **Partition schemes** — `StaticPartitionsDefinition(["c1","c2","c3"])` for the router; a `DynamicPartitionsDefinition(name="<branch>_cases")` per branch (registered by the router at runtime).
3. **Tool dispatcher** — `_query_baggage_db()` executes real SQL against `context.resources.baggage_db`. The other 5 tools live in a `TOOL_ROLEPLAY_PROMPTS` dict and are dispatched through the LLM with each tool's system prompt. `_call_tool(name, args, resource)` picks the right one.
4. **5 ReAct step ops** — `_make_step_op(i)` factory returns a `@dg.op` for each iteration. Each takes `task_str` + `prior_step`, calls the planner LLM, dispatches the picked tool, returns a trajectory-updated dict. If `prior_step.done` is set, short-circuits.
5. **`build_task` op** — reads the source CSV, filters to the current partition's row, formats a task template with the row's fields.
6. **`classify_and_register` op** — asks the classifier LLM which branches apply + fills in per-branch structured payloads, then calls `context.instance.add_dynamic_partitions("<branch>_cases", [case_id])` to register the partition on picked branches.
7. **`baggage_reports` `@dg.asset`** — CSV loader (unpartitioned source).
8. **`baggage_triage_agent` `@dg.graph_asset`** — the router. Wires `build_task → 5 plan_step_N → classify_and_register` into a graph. Every op is visible in the run view.
9. **3 branch `@dg.asset`** — via a factory: each has its own `DynamicPartitionsDefinition`, loads the router's value for its `case_id` via `dg.AssetDep(partition_mapping=dg.AllPartitionMapping())`, returns a single-row DataFrame from the router's `emit_payloads[bname]`.
10. **`courier_booked` / `compensation_paid` `@dg.asset`** — DataFrame → CSV per partition. `automation_condition=dg.AutomationCondition.eager()` so they only fire when their branch actually emitted for a partition.
11. **`human_review` `@dg.asset`** — reads `approvals/<case_id>.json`. Emits `dg.Output` for all states (materializes green) + `dg.AssetCheckResult` with severity WARN (pending) / ERROR (rejected) / passing (approved) as the actual gate signal.
12. **`escalation_audited` `@dg.asset`** — writes the human_review output to `audit/case_audit.duckdb` (create-if-missing + insert).
13. **`approve_and_process_job` = `dg.define_asset_job`** — the job the sensor launches on token drop, over `[human_review, escalation_audited]`.
14. **`approval_watcher` `@dg.sensor`** — polls the approvals dir, yields `RunRequest(partition_key=<file_stem>)` per new token.

## Variant A vs Variant B — the router body

Both variants have identical branch assets, sinks, gate, sensor, and definitions.py. The only thing that changes is the router itself.

**Variant A — `@graph_asset` + 5 `@op` ReAct steps**:

```python
def _make_step_op(iteration: int):
    @dg.op(
        name=f"plan_step_{iteration}",
        ins={
            "task_str": dg.In(str),
            "prior_step": dg.In(dict, default_value={"done": False, "trajectory": []}),
        },
        out={"step": dg.Out(dict)},
        required_resource_keys={"baggage_db"},
    )
    def _step(context, task_str, prior_step):
        if prior_step.get("done"):
            yield dg.Output({**prior_step, "iteration": iteration}, "step")
            return
        # ... 20+ lines: build planner prompt, LLM call, JSON parse, tool dispatch, update trajectory ...
        yield dg.Output({**new, "trajectory": trajectory}, "step")
    return _step

STEP_OPS = [_make_step_op(i) for i in range(1, MAX_ITER + 1)]

@dg.op(name="build_task", ins={"upstream": dg.In(dg.Nothing)}, out={"task_str": dg.Out(str)})
def _build_task(context):
    # ... 5 lines: load CSV, filter to partition, format task template ...

@dg.op(
    name="classify_and_register",
    ins={f"step_{i+1}": dg.In(dict, default_value={"done": False, "trajectory": []}) for i in range(MAX_ITER)},
    out={"classification": dg.Out(dict)},
)
def _classify(context, **kwargs):
    # ... 30 lines: extract latest trajectory, classifier LLM call, extract payloads, register partitions ...

@dg.graph_asset(
    name="baggage_triage_agent",
    partitions_def=router_partitions,
    ins={"upstream": dg.AssetIn(key=dg.AssetKey("baggage_reports"))},
)
def baggage_triage_agent(upstream):
    task = _build_task(upstream)
    steps, prior = [], None
    for op_fn in STEP_OPS:
        step = op_fn(task_str=task) if prior is None else op_fn(task_str=task, prior_step=prior)
        steps.append(step); prior = step
    return _classify(**{f"step_{i+1}": s for i, s in enumerate(steps)})
```

~130 lines. Each ReAct step is a separate op box in the run UI.

**Variant B — plain `@asset` with in-body `for` loop**:

```python
@dg.asset(
    key=dg.AssetKey("baggage_triage_agent"),
    partitions_def=router_partitions,
    ins={"upstream": dg.AssetIn(key=dg.AssetKey("baggage_reports"))},
    required_resource_keys={"baggage_db"},
    kinds={"ai", "agent", "router"},
)
def baggage_triage_agent(context: AssetExecutionContext, upstream: pd.DataFrame) -> dict:
    case_id = context.partition_key
    row = upstream[upstream["case_id"] == case_id].iloc[0].to_dict()
    task = f"...{row['passenger']}...{row['flight']}...{row['baggage_id']}..."
    client = _llm()

    # ReAct loop — plain Python. Breaks when the planner says done.
    trajectory = []
    for i in range(1, MAX_ITER + 1):
        prior_txt = "\n".join(f"{t['iteration']}: {t.get('tool')}({t.get('args')}) → "
                              f"{t.get('tool_output','')[:200]}" for t in trajectory) or "(no prior work)"
        resp = client.chat.completions.create(model=MODEL, ...
            messages=[{"role": "system", "content": "..."},
                      {"role": "user", "content": f"Task:\n{task}\n\nPrior:\n{prior_txt}\n\n..."}])
        plan = json.loads(_strip_fences(resp.choices[0].message.content or ""))
        context.log.info(f"[step {i}] plan: {plan}")
        if plan.get("done"): break
        tool_output = _call_tool(plan["tool"], plan.get("args",""), context.resources.baggage_db)
        trajectory.append({"iteration": i, "tool": plan["tool"], "args": plan.get("args"),
                           "reasoning": plan.get("reasoning",""), "tool_output": tool_output})

    # Classifier — one more LLM call, in-line
    traj_txt = "\n".join(f"{t['iteration']}: {t.get('tool')}({t.get('args')}) → "
                         f"{t.get('tool_output','')[:250]}" for t in trajectory) or "(no tools)"
    resp = client.chat.completions.create(model=MODEL, ...
        messages=[{"role": "system", "content": "Classify + extract payloads. Reply JSON: ..."},
                  {"role": "user", "content": f"Trajectory:\n{traj_txt}\n\nBranches:\n{schema_txt}"}])
    cls = json.loads(_strip_fences(resp.choices[0].message.content or ""))
    payloads = {k: v for k, v in cls.get("emit", {}).items() if k in BRANCHES}
    for branch in payloads:
        context.instance.add_dynamic_partitions(f"{branch}_cases", [case_id])

    return {"picked": list(payloads), "emit_payloads": payloads,
            "summary": cls.get("summary",""), "trajectory": trajectory}
```

~60 lines. One box in the run UI (`baggage_triage_agent`); the ReAct loop shows up in that step's logs as sequential `[step N] plan: ...` lines.

**Tradeoffs**:

| | Variant A (ops) | Variant B (plain @asset) |
|---|---|---|
| **Router LOC** | ~130 | ~60 |
| **ReAct visibility in UI** | 5 separate op boxes with per-step logs | 1 asset box; iterations in that step's log |
| **Iteration count** | 5 ops pre-declared; unused ones short-circuit as no-ops | dynamic — loop breaks when planner says done |
| **Cost per step** | 1 subprocess launch + IO manager serialize per op (multiprocess executor) | zero framework overhead per iteration |
| **Debugging** | click the specific op box → its logs | scroll one step's logs → find the right `[step N]` line |
| **Retries** | can retry a single step (has to re-run the whole loop above it though, since ops chain) | must retry the whole asset — but this is arguably what you want anyway |
| **Cost per case** | small overhead per op — a few hundred ms per iteration × 5 ops | ReAct is bounded by LLM latency (multi-second), overhead is noise |
| **When to reach for it** | you specifically want per-step lineage in the graph OR mixed retry semantics per step | almost always the simpler choice — reach for ops only when you need per-step visibility |

Our take: **Variant B is the honest starting point for a plain-Dagster router.** Variant A is what you'd write if you cared about surfacing every ReAct iteration as its own graph node. In practice the loop is bounded (max 5-10 iterations) and the LLM call dominates latency — the extra op-graph structure buys little.

Note the components version (`llm_multi_path_router` with `use_dynamic_partitions=true`) uses the Variant A shape internally because per-step visibility is often a demo-time ask. But nothing forces that — a hypothetical `llm_multi_path_router_simple` component could ship the Variant B shape with the same YAML surface.

## Side by side — the same building blocks (components vs plain)

**Router definition:**

Components:
```yaml
# defs/baggage_triage_agent/defs.yaml — 60 lines total
type: dagster_community_components.LlmMultiPathRouterComponent
attributes:
  asset_name: baggage_triage_agent
  upstream_asset_key: baggage_reports
  partition_type: static
  partition_values: "c1,c2,c3"
  partition_static_column: case_id
  max_iterations: 5
  task_template: |
    ...case task text with {passenger}/{flight}/{baggage_id}...
  tools:
    - name: query_baggage_db
      tool_type: sql
      resource: baggage_db
      sql_template: "SELECT ... WHERE baggage_id = '{args}'"
    - name: inquire_with_airport
      system_message: "You are an airport lookup..."
    # ... 4 more tools
  outputs:
    - name: delivery_request
      output_schema:
        baggage_id: "..."
        address: "..."
        delivery_id: "..."
        eta_hours: "..."
    - name: voucher_issued
      output_schema:
        voucher_id: "..."
        amount_usd: "..."
    - name: escalation
      output_schema:
        case_summary: "..."
        urgency: "..."
```

Plain Python (the equivalent — spread across the `assets.py` file):
```python
# ~130 lines: 5 step ops via factory, build_task op, classify op, graph_asset

# Partition definitions
router_partitions = dg.StaticPartitionsDefinition(["c1", "c2", "c3"])
branch_partitions = {b: dg.DynamicPartitionsDefinition(name=f"{b}_cases") for b in BRANCHES}

# Tools + dispatch (SQL + LLM-roleplay dict + _call_tool function) — 40 lines

def _make_step_op(iteration: int):
    @dg.op(
        name=f"plan_step_{iteration}",
        ins={"task_str": dg.In(str),
             "prior_step": dg.In(dict, default_value={"done": False, "trajectory": []})},
        out={"step": dg.Out(dict)},
        required_resource_keys={"baggage_db"},
    )
    def _step(context, task_str, prior_step):
        if prior_step.get("done"):
            yield dg.Output({**prior_step, "iteration": iteration}, "step"); return
        # ... planner LLM call, JSON parse, tool dispatch, trajectory update ...
        yield dg.Output({**new, "trajectory": trajectory}, "step")
    return _step
STEP_OPS = [_make_step_op(i) for i in range(1, MAX_ITER + 1)]

@dg.op(name="build_task", ins={"upstream": dg.In(dg.Nothing)}, out={"task_str": dg.Out(str)})
def _build_task(context):
    df = pd.read_csv(BAGGAGE_CSV)
    row = df[df["case_id"] == context.partition_key].iloc[0].to_dict()
    return f"...{row['passenger']}...{row['flight']}...{row['baggage_id']}..."

# classify_and_register op — 30 lines: prompt build, LLM call, JSON parse,
# extract per-branch payloads, register dynamic partitions

@dg.graph_asset(
    name="baggage_triage_agent",
    partitions_def=router_partitions,
    ins={"upstream": dg.AssetIn(key=dg.AssetKey("baggage_reports"))},
    kinds={"ai", "agent", "router"},
)
def baggage_triage_agent(upstream):
    task = _build_task(upstream)
    steps, prior = [], None
    for op_fn in STEP_OPS:
        step = op_fn(task_str=task) if prior is None else op_fn(task_str=task, prior_step=prior)
        steps.append(step); prior = step
    return _classify(**{f"step_{i+1}": s for i, s in enumerate(steps)})
```

**Branch assets:**

Components:
```yaml
# 3 files, ~5 lines each — the component handles the shape
type: dagster_community_components.LlmMultiPathRouterComponent
attributes:
  outputs:
    - name: delivery_request
      output_schema: {baggage_id: "...", address: "...", ...}
    - name: voucher_issued
      output_schema: {voucher_id: "...", amount_usd: "...", ...}
    - name: escalation
      output_schema: {case_summary: "...", urgency: "...", ...}
```

Plain Python:
```python
def _make_branch_asset(bname: str, schema: list[str]):
    @dg.asset(
        key=dg.AssetKey(bname),
        partitions_def=branch_partitions[bname],
        deps=[dg.AssetDep(asset=dg.AssetKey("baggage_triage_agent"),
                          partition_mapping=dg.AllPartitionMapping())],
    )
    def _branch(context: AssetExecutionContext):
        case_id = context.partition_key
        router_val = context.op_execution_context.load_asset_value(
            dg.AssetKey("baggage_triage_agent"), partition_key=case_id)
        payload = (router_val.get("emit_payloads") or {}).get(bname, {}) or {}
        return pd.DataFrame([{"case_id": case_id, **{f: payload.get(f) for f in schema}}])
    _branch.__name__ = bname
    return _branch

delivery_request = _make_branch_asset("delivery_request", ["baggage_id", "passenger", "address", "delivery_id", "eta_hours"])
voucher_issued = _make_branch_asset("voucher_issued", ["passenger", "amount_usd", "voucher_id", "reason"])
escalation = _make_branch_asset("escalation", ["case_summary", "needs_action", "urgency"])
```

**Human gate:**

Components:
```yaml
type: dagster_community_components.HumanApprovalGateComponent
attributes:
  asset_name: human_review
  upstream_asset_key: escalation
  approval_dir: /tmp/.../approvals
  partition_type: dynamic
  dynamic_partition_name: escalation_cases
```

Plain Python — 30 lines:
```python
_approval_check = dg.AssetCheckSpec(name="approved", asset=dg.AssetKey("human_review"), ...)

@dg.asset(
    key=dg.AssetKey("human_review"),
    partitions_def=branch_partitions["escalation"],
    ins={"upstream": dg.AssetIn(key=dg.AssetKey("escalation"))},
    check_specs=[_approval_check],
)
def human_review(context: AssetExecutionContext, upstream: pd.DataFrame):
    case_id = context.partition_key
    token_file = APPROVAL_DIR / f"{case_id}.json"
    empty = upstream.iloc[0:0].copy()
    if not token_file.exists():
        yield dg.Output(empty, metadata={"status": "approval_pending", ...})
        yield dg.AssetCheckResult(check_name="approved", passed=False,
                                  severity=dg.AssetCheckSeverity.WARN, ...)
        return
    token = json.loads(token_file.read_text())
    if not token.get("approved"):
        yield dg.Output(empty, metadata={"status": "approval_rejected", ...})
        yield dg.AssetCheckResult(check_name="approved", passed=False,
                                  severity=dg.AssetCheckSeverity.ERROR, ...)
        return
    yield dg.Output(upstream, metadata={"status": "approved", ...})
    yield dg.AssetCheckResult(check_name="approved", passed=True, ...)
```

**Sensor:**

Components:
```yaml
type: dagster_community_components.FilesystemMonitorSensorComponent
attributes:
  sensor_name: approval_watcher
  directory_path: /tmp/.../approvals
  file_pattern: "^c[0-9]+\\.json$"
  job_name: approve_and_process_job
  partition_mode: dynamic_partition
  dynamic_partitions_name: escalation_cases
  partition_key_template: "{file_stem}"
```

Plain Python — 15 lines:
```python
@dg.sensor(name="approval_watcher", minimum_interval_seconds=5, job=approve_and_process_job)
def approval_watcher(context: SensorEvaluationContext):
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    cursor = float(context.cursor) if context.cursor else 0.0
    run_requests, latest = [], cursor
    for name in os.listdir(APPROVAL_DIR):
        if not name.endswith(".json"): continue
        path = APPROVAL_DIR / name
        mtime = path.stat().st_mtime
        if mtime <= cursor: continue
        case_id = Path(name).stem
        run_requests.append(dg.RunRequest(run_key=f"{case_id}-{mtime}", partition_key=case_id))
        latest = max(latest, mtime)
    return dg.SensorResult(run_requests=run_requests, cursor=str(latest)) if run_requests \
        else dg.SensorResult(skip_reason="no new tokens")
```

## What the component does that plain Python has to write by hand

For **every plain-Python router built like this**, you write:

- Prompt engineering (planner + classifier system prompts, JSON schema in the prompt, markdown-fence stripping)
- ReAct loop plumbing — step ops via factory, chaining, short-circuit-on-done
- Cross-partition mapping (static router → dynamic branches via `AllPartitionMapping`)
- Runtime dynamic-partition registration
- Tool dispatch (`if name == "query_baggage_db": run SQL; elif name in TOOL_ROLEPLAY_PROMPTS: run LLM; ...`)
- Args parsing (bare string vs JSON blob, quote stripping)
- Branch payload extraction (per-schema `pd.DataFrame([row])`)
- Human-gate materialization + asset check emission logic
- Sensor cursor tracking, partition_key derivation from filename

In the components version, all of that lives in [`llm_multi_path_router`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_multi_path_router) (~1000 lines) and [`human_approval_gate`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/human_approval_gate) (~350 lines). The user's YAML declares WHAT (the shape); the component's Python defines HOW (the plumbing).

## Adding a second router

**Components** — new YAML file, no Python:

```yaml
# defs/fraud_triage_agent/defs.yaml
type: dagster_community_components.LlmMultiPathRouterComponent
attributes:
  asset_name: fraud_triage_agent
  upstream_asset_key: fraud_reports
  partition_type: dynamic
  dynamic_partition_name: fraud_cases
  task_template: "You are a fraud triage agent. Case {case_id}: ..."
  tools:
    - name: query_transaction_history
      tool_type: sql
      resource: postgres_txn_db
      sql_template: "SELECT ... FROM transactions WHERE user_id = '{args}'"
    - name: check_device_fingerprint
      tool_type: http
      http_url: "https://internal-fraud/api/device/{args}"
    # 4 more tools...
  outputs:
    - name: block_account
      output_schema:
        user_id: "..."
        reason: "..."
    - name: require_step_up_auth
      output_schema:
        user_id: "..."
        auth_method: "..."
    - name: escalate_to_fraud_ops
      output_schema:
        case_summary: "..."
        urgency: "..."
```

~40 more lines of YAML. Same graph_asset generated behind the scenes, same partitioning story, same gate primitive available downstream.

**Plain Python** — duplicate `assets.py`, or factor into a reusable factory:

Option A: **duplicate assets.py, rename everything.** 250 more lines, mostly the same.

Option B: **factor into a Python factory function**. Something like:

```python
def build_router(
    router_name: str,
    upstream_key: str,
    task_template: str,
    tools: dict,
    output_schemas: dict,
    router_pdef,
    ...
):
    # Build STEP_OPS, build_task, classify, branch_partitions, graph_asset,
    # branch factories all parameterized. Return a list of assets.
    ...

baggage_assets = build_router("baggage_triage_agent", "baggage_reports", ..., BAGGAGE_TOOLS, BAGGAGE_SCHEMAS, ...)
fraud_assets = build_router("fraud_triage_agent", "fraud_reports", ..., FRAUD_TOOLS, FRAUD_SCHEMAS, ...)
```

At which point you've just built a component from scratch — congratulations, that's the [`llm_multi_path_router`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_multi_path_router) component's `_build_dynamic_shape` method. That's what components *are* — Python factories with a YAML front door.

## What's *identical* between the two versions

- **The asset graph.** Click into `baggage_triage_agent` on either UI — same shape, same partitions, same ops in the run view.
- **The runtime behavior.** Same tools called in the same order, same partitions registered, same downstream branches materialize.
- **The UI experience.** Same "click a partition, see the trajectory in materialization metadata" flow. Same red/yellow/green gate states.
- **The Dagster primitives used.** `@graph_asset`, `@op`, `@asset`, `DynamicPartitionsDefinition`, `AllPartitionMapping`, `AssetCheckResult`, `AutomationCondition.eager()`, `@sensor`, `RunRequest`. Not a single Dagster feature is different.

The plain version isn't "the honest one" and the component version isn't "cheating." Components are a **factory pattern**, no different than any other Python abstraction — just with a declarative front door for the fields the user actually needs to fill in.

## When each shape fits

**Plain Dagster fits when:**
- You have exactly one router and don't expect to add another.
- You need custom behavior the component doesn't expose (a novel step type, unusual partition mapping, non-standard tool protocol).
- You're evaluating Dagster and want to see the raw framework surface first.
- You already have engineering ownership of every asset in the project — analysts don't author here.

**Components fit when:**
- You have (or will have) multiple router instances — per team, per case type, per environment.
- You want analysts / domain experts to author routers in YAML while engineering owns the primitive.
- You want the ~957-component catalog of ready-made primitives (chunkers, embedders, sinks, sensors, resources) to compose from.
- You want the router's shape to be an audit artifact (a YAML in git) rather than buried in Python.

## Runtime demo — what each partition does

Both versions:

- **c1** → agent picks `delivery_request` → registers c1 on `delivery_request_cases` → delivery_request materializes → `courier_booked/courier_booked_c1.csv` written.
- **c2** → agent picks `voucher_issued` → registers c2 on `voucher_issued_cases` → compensation_paid CSV.
- **Escalation cases** → register on `escalation_cases` → `human_review` pends (WARN check) until a token drops → sensor sees the token, fires `approve_and_process_job` → cascade completes → audit trail in DuckDB.

## Files worth reading

- [`setup_agentic_router_plain_demo.sh`](setup_agentic_router_plain_demo.sh) — the setup script itself. It literally cats out `assets.py` and `definitions.py` — no magic.
- The generated project's `src/<pkg>/defs/assets.py` — after running the setup, this is the whole thing in one file.

Send this walkthrough to anyone who says "your framework does too much magic." The magic is a Python file. Every line is Dagster you'd write yourself.

## See also

<!-- TODO: link related walkthroughs -->
