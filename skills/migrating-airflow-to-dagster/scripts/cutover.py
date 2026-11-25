#!/usr/bin/env python3
"""Write one cutover checklist per converted DAG: `cutover-<dag_id>.md`, plus `cutover-index.md`.

    python3 cutover.py manifest.json report.json [--ir ir.json] [--parity-state parity_state/] [--out-dir cutover]
                       [--airflow-cmd "airflow"] [--dagster-cmd "dagster"] [--airflow-owner-note TEXT]

THIS SCRIPT NEVER PAUSES, STARTS, STOPS, DELETES OR CHANGES ANYTHING. It reads the manifest, the converter's report and, when given, the IR and the
parity state, and writes markdown. The commands in the checklists are printed for a person to run, one DAG at a time, in a change window; with a hosted
Airflow (MWAA, Composer, Astro) they go through the service's own CLI or API (`reference/hosted-airflow.md`): set `--airflow-cmd` accordingly.

What each checklist holds: pause plan, catchup and backfill decision, secrets and connections moved (names only, never values), schedule and timezone
check, retries / pools / concurrency to apply, what was NOT converted and needs a manual replacement (alerts, callbacks, SLAs), freshness expectations,
parity evidence, on-call handoff, rollback, decommission. A DAG with a deferred unit or a stubbed task gets a BLOCKERS section first.

The IR is optional but makes the checklist much more precise (catchup, start date, timezone, owners, callbacks, Variable names).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "parity"))
import compare as parity_compare  # noqa: E402  (its logic is reused so "identical" means what it means everywhere else)

VAR_RX = [re.compile(r"var\.(?:value|json)\.([A-Za-z0-9_\-]+)"), re.compile(r"Variable\.get\(\s*['\"]([^'\"]+)['\"]"), re.compile(r"Variable\.(?:get|set)\(\s*['\"]([^'\"]+)['\"]")]


def safe(name: str) -> str:
    s = re.sub(r"\W", "_", name)
    return f"_{s}" if s[:1].isdigit() else s


def load(p: Path | None) -> Any:
    return json.loads(p.read_text()) if p else None


def strings(x: Any):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from strings(v)


def variable_names(ir_dag: dict[str, Any] | None) -> list[str]:
    """Names of Airflow Variables the DAG reads (templates `var.value.X`, `var.json.X`, `Variable.get("X")` in copied callables). Names only."""
    found: set[str] = set()
    for t in (ir_dag or {}).get("tasks", []):
        texts = list(strings([t.get("template_fields"), t.get("op_args"), t.get("op_kwargs"), (t.get("callable") or {}).get("source")]))
        for text in texts:
            for rx in VAR_RX:
                found |= set(rx.findall(text))
    return sorted(found)


def cadence(cron: str | None) -> str:
    """A freshness expectation in words, from a cron expression. A guess at the intent, labelled as such in the checklist."""
    if not cron:
        return "no schedule: freshness is whatever triggers the assets"
    f = cron.split()
    if len(f) != 5:
        return f"cron `{cron}`"
    mi, hr, dom, mon, dow = f
    if hr.startswith("*/") and dom == mon == dow == "*":
        return f"every {hr[2:]} hours"
    if mi.startswith("*/") and hr == dom == mon == dow == "*":
        return f"every {mi[2:]} minutes"
    if hr == "*" and dom == mon == dow == "*":
        return "hourly"
    if dom == mon == "*" and dow == "*":
        return "daily"
    if dom == mon == "*" and dow != "*":
        return "weekly (or on the listed weekdays)"
    if dom != "*" and mon == "*":
        return "monthly (or on the listed days)"
    return f"cron `{cron}`"


def dag_units(manifest: dict[str, Any], dag_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    dag = next((u for u in manifest["units"] if u["kind"] in ("dag", "cosmos", "dag_factory", "blueprint_dag") and u["dag_id"] == dag_id and not u.get("factory")), None)
    return dag, [u for u in manifest["units"] if u["kind"] == "task" and u["dag_id"] == dag_id]


def parity_for(state_dir: Path | None, dag_id: str, rep_dag: dict[str, Any]) -> tuple[str, list[str]]:
    """(verdict, notes) for the DAG from a parity state directory (parity_incremental.py): af_*.json, dg_*.json, reports.json."""
    if state_dir is None:
        return "not given", ["no `--parity-state` was passed, so parity evidence is not known to this checklist: attach it by hand"]
    af = {}
    dg = {}
    for p in sorted(state_dir.glob("af_*.json")):
        af.update(json.loads(p.read_text()))
    for p in sorted(state_dir.glob("dg_*.json")):
        dg.update(json.loads(p.read_text()))
    if dag_id not in af and dag_id not in dg:
        return "not in a parity run", ["the DAG was not part of a parity run (parity runs cover DAGs without external systems or long waits): a side-by-side run on the same logical window is the evidence to collect"]
    a, d = af.get(dag_id, {"error": "no Airflow result"}), dg.get(dag_id, {"error": "no Dagster result"})
    if "error" in a or "error" in d:
        return "no comparison", [f"airflow: {a.get('error', 'ok')}; dagster: {d.get('error', 'ok')}"]
    issues = parity_compare.dag_issues(a, d, rep_dag)
    return ("identical outcome", []) if not issues else ("differs", issues)


def build_checklist(dag_id: str, manifest: dict[str, Any], rep: dict[str, Any], report: dict[str, Any], ir_dag: dict[str, Any] | None,
                    parity_dir: Path | None, af_cmd: str, dg_cmd: str) -> tuple[str, list[str]]:
    """(markdown, the commands it prints). Pure: no file or process is touched."""
    dag_unit, tasks = dag_units(manifest, dag_id)
    job = safe(dag_id)
    sched_name = f"{job}_schedule"
    sched = rep.get("schedule")
    tz = rep.get("timezone")
    notes = rep.get("notes") or []
    caveats = rep.get("caveats") or []
    stubs = [c for c in caveats if c.get("kind") == "stub"]
    degraded = [c for c in caveats if c.get("kind") == "degraded"]
    deferred_units = [u for u in ([dag_unit] if dag_unit else []) + tasks if u["status"] == "deferred"]
    pending_units = [u for u in ([dag_unit] if dag_unit else []) + tasks if u["status"] == "pending"]
    cmds: list[str] = []
    L: list[str] = [f"# Cutover checklist: `{dag_id}`", "",
                    "Generated by `scripts/cutover.py`. **This file and the script changed nothing**: every command below is for a person to run, in a change window, after the earlier boxes are ticked. "
                    "Nothing here is a statement that the cutover is safe; it is the list of what to settle.", "",
                    f"- Airflow file: `{(ir_dag or {}).get('file') or (dag_unit or {}).get('file') or 'unknown'}`",
                    f"- Dagster job: `{job}`" + (f", schedule `{sched_name}` (`{sched}`{', ' + tz if tz else ''})" if sched else " (no schedule: triggered by assets or by hand)"),
                    f"- Converter output: {rep.get('output', 'python')}; {rep.get('assets', 0)} asset(s)" + (f", {len(rep.get('cosmos') or [])} dbt component(s)" if rep.get("cosmos") else ""), ""]
    blockers = [f"unit {u['id']} ({u.get('task_id') or 'the DAG'}) is deferred: {u.get('reason') or 'no reason recorded'}" for u in deferred_units] \
        + [f"unit {u['id']} ({u.get('task_id') or 'the DAG'}) is still pending" for u in pending_units] \
        + [f"task `{c['task']}` is a loud stub (`AIRFLOW-CAVEAT`): {c['reason']}" for c in stubs]
    if blockers:
        L += ["## BLOCKERS: resolve before any cutover", ""] + [f"- [ ] {b}" for b in blockers] + [""]

    # 1 pause plan
    L += ["## 1. Pause plan", "",
          "Order inside the change window, one DAG (or one domain) at a time. Airflow stays authoritative until the last step is done.", "",
          "- [ ] Confirm no Airflow run of this DAG is in flight, and note the last successful logical date: ______",
          "- [ ] Confirm the Dagster schedule is **off** and the converted code is deployed (`dg check defs`, then the deployment's code location is healthy)",
          "- [ ] Pause the Airflow DAG (`dags pause` stops new runs; it does not stop a running one)",
          "- [ ] Start the Dagster schedule" + ("" if sched else " (there is none: enable the automation condition or trigger the job instead)"),
          "- [ ] Watch the first Dagster run to completion and compare with section 8", ""]
    c_pause = f"{af_cmd} dags pause {dag_id}"
    c_next = f"{af_cmd} dags next-execution {dag_id}"
    c_start = f"{dg_cmd} schedule start {sched_name}"
    c_stop = f"{dg_cmd} schedule stop {sched_name}"
    c_unpause = f"{af_cmd} dags unpause {dag_id}"
    cmds += [c_pause, c_next] + ([c_start] if sched else [])
    L += ["```bash", "# for a person to run; the script ran none of these (add your workspace or deployment flags to the dagster commands)", c_pause, c_start if sched else f"# no schedule to start; launch the job `{job}` by hand once to check", "```", ""]

    # 2 catchup / backfill
    catchup = (ir_dag or {}).get("catchup")
    start = ((ir_dag or {}).get("start_date") or "")[:10] or "unknown"
    catchup_note = next((n for n in notes if n.startswith("catchup=True")), None)
    catchup_cav = next((c["reason"] for c in degraded if c["reason"].startswith("catchup=True")), None)
    L += ["## 2. Catchup and backfill decision", ""]
    if catchup or catchup_note or catchup_cav:
        L += [f"- Airflow had `catchup=True` (start date {start}): it replayed every interval it had missed.",
              "- The converted schedule **runs from now on and does not replay history**: " + (catchup_note or catchup_cav or "the converter's `--catchup` handling (`ignore`, or the default caveat)") + ".",
              "- [ ] Decide and record: no backfill / backfill from ______ to ______ (a Dagster backfill; the assets need partitions for that, see `reference/partitions.md`)",
              "- [ ] If the first Dagster tick would leave a gap since the last Airflow run (note in section 1), say who fills it", ""]
    elif catchup is False:
        L += [f"- Airflow had `catchup=False` (start date {start}): it did not replay missed intervals, so none is owed.",
              "- [ ] Check there is no gap between the last Airflow run and the first Dagster tick that downstream consumers would notice", ""]
    else:
        L += ["- Catchup is unknown (no `--ir` was given). Check the DAG's `catchup` and `start_date` in the Airflow source.",
              "- [ ] Decide and record: no backfill / backfill from ______ to ______", ""]

    # 3 secrets
    vars_ = variable_names(ir_dag)
    conns = {c: v for c, v in (report.get("connections") or {}).items() if c in (rep.get("provider_connections") or {})}
    cosm_env = sorted({e for cl in (rep.get("cosmos") or []) + (rep.get("cosmos_steps") or []) for e in (cl.get("environment") or cl.get("env") or [])})
    L += ["## 3. Secrets and connections moved (names only; no value is in this file)", ""]
    rows = []
    for c in sorted(rep.get("resources") or []):
        rows.append((f"Connection `{c}` (AWS)", f"resource `{c}` in `resources.py`; credentials from the default AWS credential chain", "grant the Dagster deployment the same IAM permissions"))
    for c in sorted(rep.get("snowflake_resources") or []):
        rows.append((f"Connection `{c}` (Snowflake)", f"resource `{c}`", "`SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`"))
    for c, v in sorted(conns.items()):
        rows.append((f"Connection `{c}` ({v['kind']})", v["read_by"], ", ".join(f"`{e}`" for e in v["environment"])))
    for name in vars_:
        env_name = "AIRFLOW_VAR_" + re.sub(r"\W", "_", name).upper()
        rows.append((f"Variable `{name}`", "read from the environment by the converted code", f"`{env_name}`"))
    if cosm_env:
        rows.append(("dbt profile and project environment", "dbt reads them when it runs", ", ".join(f"`{e}`" for e in cosm_env)))
    if rows:
        L += ["| What Airflow had | Where it is read in Dagster | Set in the Dagster deployment |", "|---|---|---|"] + [f"| {a} | {b} | {c} |" for a, b, c in rows] + [""]
        L += ["- [ ] Every name above exists as a secret or environment variable in the Dagster deployment (`dg plus create env`, or your secret store), with its value set by its owner; none is committed to the repo",
              "- [ ] Values were NOT copied out of Airflow by this tooling: rotate them if they were pasted anywhere on the way", ""]
    else:
        L += ["- The converter found no connection or Variable use in this DAG." + ("" if ir_dag else " (Without `--ir`, Variables read inside callables are not detected: check by hand.)"), ""]

    # 4 schedule / timezone
    L += ["## 4. Schedule and timezone check", ""]
    ir_tt = (ir_dag or {}).get("timetable") or {}
    L += [f"| | Airflow | Dagster |", "|---|---|---|",
          f"| schedule | {ir_tt.get('cron') or ir_tt.get('summary') or 'see the DAG'} ({ir_tt.get('kind', 'unknown')}) | {sched or 'none'} |",
          f"| timezone | {ir_tt.get('timezone') or 'unknown'} | {tz or 'n/a'} |", ""]
    L += ["- [ ] The two crons and timezones agree (a daylight-saving change moves a local-time cron by an hour in both; check the next two transitions)",
          "- [ ] Data-interval versus trigger semantics: Airflow 2's default cron timetable runs for the interval that just ended, Airflow 3's for the tick; the converted code is exact only if no task uses the logical date or data interval (see the NOTE at the top of the generated module and `reference/mapping.md`)",
          f"- [ ] The next Dagster tick equals the next Airflow run: `{c_next}` against the schedule's next tick in the Dagster UI", ""]

    # 5 limits
    pools = rep.get("pools") or {}
    L += ["## 5. Retries, pools and concurrency to apply", "",
          "`concurrency_limits.yaml` (written by the converter) holds the values; **they take effect only when applied to the deployment**.", ""]
    ret = sorted({(t["task_id"], t["retries"]) for t in (ir_dag or {}).get("tasks", []) if t.get("retries") and t["task_id"] not in (rep.get("cosmos_tasks") or {})})
    run_retries = max((cl.get("retries") or 0 for cl in rep.get("cosmos") or []), default=0)
    L += [f"- Retries: {', '.join(f'`{t}` x{n}' for t, n in ret)} (`RetryPolicy` on the assets)" if ret else "- Retries: none set (or not known without `--ir`)"]
    if run_retries:
        L.append(f"- [ ] The Cosmos tasks' retries (x{run_retries}) became the job tag `dagster/max_retries={run_retries}`: enable run retries on the deployment (`run_retries.enabled` in dagster.yaml), "
                 "or the tag does nothing. One `dbt build` is the unit that is retried; `dbt retry` re-runs only the failed models (a behaviour change, and an improvement)")
    if pools:
        for p, n in sorted(pools.items()):
            cmd = f"{dg_cmd} instance concurrency set {p} {n if n is not None else '<slot count of the Airflow pool>'}"
            L.append(f"- [ ] Pool `{p}`: limit {n if n is not None else 'from the Airflow pool (slots live in the Airflow metadata database)'}")
            cmds.append(cmd)
        L += ["", "```bash", *[f"{dg_cmd} instance concurrency set {p} {n if n is not None else '<slot count of the Airflow pool>'}" for p, n in sorted(pools.items())], "```"]
    else:
        L.append("- Pools: none")
    if rep.get("max_active_runs"):
        L.append(f"- [ ] `max_active_runs={rep['max_active_runs']}`: merge the `tag_concurrency_limits` entry (`airflow/dag_id` = `{dag_id}`) into the run coordinator settings")
    if rep.get("max_active_tasks"):
        L.append(f"- [ ] `max_active_tasks={rep['max_active_tasks']}`: a per-run executor limit, exact only while runs are limited to one at a time")
    if rep.get("run_timeout_s"):
        L.append(f"- [ ] `dagrun_timeout` ({rep['run_timeout_s']} s) is a `dagster/max_runtime` tag, enforced only with run monitoring enabled on the deployment")
    L.append("")

    # 6 not converted
    L += ["## 6. NOT converted: needs a manual replacement", ""]
    manual: list[str] = []
    if (ir_dag or {}).get("has_callbacks"):
        manual.append("DAG-level callbacks (`on_failure_callback`, `on_success_callback`, `sla_miss_callback`): attach a run failure sensor or an alert policy")
    for t in (ir_dag or {}).get("tasks", []):
        if t.get("callbacks") and not rep.get("cosmos_tasks", {}).get(t["task_id"]):
            manual.append(f"task `{t['task_id']}` callbacks/hooks {t['callbacks']}: check the report row; those not translated need an `@on_hooks` or alert policy")
        if t.get("sla_s"):
            manual.append(f"task `{t['task_id']}` SLA {int(t['sla_s'])} s: the converter records an `sla_breach` observation on the asset, which **alerts nobody**: add an alert policy or a freshness check")
    da = (ir_dag or {}).get("default_args") or {}
    if da.get("email") or da.get("email_on_failure") or da.get("email_on_retry"):
        manual.append("Airflow `email` / `email_on_failure` / `email_on_retry` defaults: no failure email is sent by the converted code; add an alert policy")
    for c in degraded:
        manual.append(f"{('task `' + c['task'] + '`: ') if c['task'] else ''}{c['reason']}")
    seen: set[str] = set()
    L += [f"- [ ] {m}" for m in manual if not (m in seen or seen.add(m))] or ["- Nothing the converter reported as unconverted (still read the report's `caveats` for this DAG)."]
    L.append("")

    # 7 freshness
    L += ["## 7. Freshness expectations", "",
          f"- Cadence read from the schedule: **{cadence(sched)}** (a guess at the intent, not a requirement).",
          "- [ ] Write down the freshness each consumer expects (for example \"orders table updated by 07:00 UTC\") and set an alert or freshness check for it before cutover: the Airflow SLA, if any, is not carried over (section 6)",
          "- [ ] Assets: " + (", ".join(f"`{k}`" for k in ["/".join(x) if isinstance(x, list) else x for x in (rep.get('keys') or [])][:12]) or "see the report") + ("" if len(rep.get("keys") or []) <= 12 else f" and {len(rep['keys']) - 12} more"), ""]

    # 8 parity
    verdict, pnotes = parity_for(parity_dir, dag_id, rep)
    L += ["## 8. Parity evidence", "", f"- Parity run: **{verdict}**"] + [f"  - {n}" for n in pnotes[:8]]
    L += ["- [ ] For anything a parity run cannot cover (external systems, sensors on them, durations, failure behaviour, data volume): a side-by-side run on the same logical window, with row counts or checksums compared: ______", ""]

    # 9 on-call
    owners = sorted({t.get("owner") for t in (ir_dag or {}).get("tasks", []) if t.get("owner")} | ({da["owner"]} if da.get("owner") else set()))
    L += ["## 9. On-call handoff", "",
          f"- Airflow owner(s): {', '.join(f'`{o}`' for o in owners) or 'unknown'}" + (f"; tags: {', '.join(f'`{t}`' for t in (ir_dag or {}).get('tags', []))}" if (ir_dag or {}).get("tags") else ""),
          "- [ ] Dagster owner and escalation path: ______", "- [ ] Alerts for this job route to: ______ (and the Airflow alert route is removed in section 11, not before)",
          "- [ ] Runbook updated: where to look (Dagster run page, asset page), how to re-run (`dagster asset materialize` or the UI), what a `AIRFLOW-CAVEAT` failure means",
          "- [ ] The on-call person has run the job once in the target environment", ""]

    # 10 rollback
    L += ["## 10. Rollback", "",
          "Until section 11 is done, rollback is the reverse of section 1, in this order:", "",
          "- [ ] Stop the Dagster schedule", f"- [ ] Unpause the Airflow DAG (and, if the Airflow `catchup` is on, check what it will replay on unpausing: `catchup=True` runs every interval missed while it was paused)",
          "- [ ] If Airlift proxying was used for this DAG: set `proxied: false` for its tasks in `proxied_state/<dag_id>.yaml` (the DAG then runs its own operators again on the next parse; see `reference/airlift.md`)",
          "- [ ] Check for a double run: a run started by both sides in the same window writes the same outputs twice", "",
          "```bash", "# for a person to run; the script ran none of these", c_stop, c_unpause, "```", ""]
    cmds += [c_stop, c_unpause]

    # 11 decommission
    L += ["## 11. Decommission (after the agreed number of clean Dagster runs; a person decides how many)", "",
          "- [ ] Dagster runs have matched expectations for ______ consecutive scheduled runs, with the alerts in section 6 live",
          "- [ ] Airflow run history that must be kept is exported or readable (it does not migrate)",
          "- [ ] The Airflow DAG file is removed from the Airflow deployment's DAG folder in a reviewed change (deleting the DAG in the Airflow UI only removes metadata, the file would re-create it)",
          "- [ ] Connections, Variables and pools only this DAG used are removed from Airflow (names in section 3 and 5; check no other DAG uses them first)",
          "- [ ] The Airflow-side alert route for this DAG is removed; the manifest unit is advanced with evidence (`status.py advance`)", "",
          "```bash", "# only after everything above; for a person to run", f"{af_cmd} dags delete {dag_id}", "```", ""]
    cmds.append(f"{af_cmd} dags delete {dag_id}")
    return "\n".join(L), cmds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("report", type=Path)
    ap.add_argument("--ir", type=Path)
    ap.add_argument("--parity-state", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("cutover"))
    ap.add_argument("--airflow-cmd", default="airflow", help="how a person runs the Airflow CLI (for MWAA, Composer or Astro see reference/hosted-airflow.md)")
    ap.add_argument("--dagster-cmd", default="dagster")
    a = ap.parse_args(argv)
    manifest, report, ir = load(a.manifest), load(a.report), load(a.ir)
    ir_dags = {d["dag_id"]: d for d in (ir or {}).get("dags", [])}
    a.out_dir.mkdir(parents=True, exist_ok=True)
    index = ["# Cutover index", "", "One checklist per converted DAG. A DAG that was not converted has no checklist: it is listed below with the reason.", "",
             "| DAG | Checklist | Blockers |", "|---|---|---|"]
    printed: list[str] = []
    for dag_id in sorted(report["dags"]):
        rep = report["dags"][dag_id]
        if rep["status"] != "converted":
            index.append(f"| `{dag_id}` | none (deferred by the converter) | {str(rep.get('reason'))[:200]} |")
            continue
        text, cmds = build_checklist(dag_id, manifest, rep, report, ir_dags.get(dag_id), a.parity_state, a.airflow_cmd, a.dagster_cmd)
        name = f"cutover-{dag_id}.md"
        (a.out_dir / name).write_text(text + "\n")
        nblock = text.count("\n- [ ] ", text.find("## BLOCKERS"), text.find("## 1. Pause plan")) if "## BLOCKERS" in text else 0
        index.append(f"| `{dag_id}` | [{name}]({name}) | {nblock if nblock else 'none'} |")
        printed.append(f"# {dag_id}\n" + "\n".join(cmds))
    (a.out_dir / "cutover-index.md").write_text("\n".join(index) + "\n")
    print(f"wrote {len(printed)} checklist(s) to {a.out_dir}. NOTHING was paused, started, stopped or deleted. Commands for a person to run, per DAG:\n")
    print("\n\n".join(printed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
