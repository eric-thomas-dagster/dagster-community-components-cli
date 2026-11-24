"""Map a RunMyJobs Model onto a Dagster project and a migration report."""
from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from .cron import element_to_cron
from .integrations import detect, mirrored_asset_key
from .model import Call, Chain, Model, Param, ProcessDef, Step
from .questions import chain_questions, render_questions
from .runtime import COMPONENTS_SOURCE, RUNTIME_SOURCE

CONVERTED, REVIEW, UNMAPPED = "converted", "needs review", "unmapped"


def slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return f"_{s}" if s[:1].isdigit() else (s or "unnamed")


@dataclass
class Item:
    kind: str       # process_definition | chain | schedule | queue
    source: str
    artifact: str
    status: str
    notes: list[str] = field(default_factory=list)
    expected: dict = field(default_factory=dict)   # what the generated project should contain (checked by `validate`)
    caveat: str = ""                               # "stub" (fails loudly with RMJ-CAVEAT) | "degraded" (translated with a known difference) | ""


@dataclass
class Result:
    code: str                       # python output: defs/runmyjobs.py; components output: the python fallback module (stubs, SQL with a pool)
    items: list[Item]
    pools: dict[str, int | None]
    output: str = "python"          # python | components
    files: dict[str, str] = field(default_factory=dict)   # components output: path under src/<pkg>/ -> content
    questions: dict[str, list[str]] = field(default_factory=dict)   # per chain: what only the customer can answer (see questions.py)


def _literal(value) -> str:
    return repr(value)


def _is_error(h: dict) -> bool:
    return str(h.get("status", "")).lower() in ("error", "failed")


def _retries_from_handlers(step: Step, items_notes: list[str]) -> int | None:
    """Retries from a restart handler; the other handler actions seen in real exports are reported (and mark_completed is applied by the caller)."""
    retries = None
    for h in step.status_handlers:
        act = h.get("action")
        if act == "restart_step" and _is_error(h):
            retries = int(h.get("max", 1))
            if h.get("delay_seconds") and h["delay_seconds"] != 60:
                items_notes.append(f"the restart waits {h['delay_seconds']}s in RunMyJobs; the retry delay here is 60s (set retry_policy_delay_seconds to match)")
        elif act == "raise_error" and _is_error(h):
            items_notes.append(f"on Error the step raises {h.get('message', '')!r}" + (f" with return code {h['return_code']}" if h.get("return_code") else "")
                               + ": a failing step already fails this way; the text is only in the log")
        elif act == "mark_completed":
            pass            # applied by the caller (ignore_failure)
        else:
            items_notes.append(f"status handler {h!r} has no automatic mapping (see MIGRATION.md notes)")
    return retries


def _ignores_failure(step: Step) -> bool:
    return any(h.get("action") == "mark_completed" and _is_error(h) for h in step.status_handlers)


_REVIEW_MARKERS = ("not enforced", "no automatic", "mandatory", "not set here", "is ignored", "not applied")


def _apply_mark_completed(step: Step, plan: "Plan", notes: list[str]) -> None:
    """RunMyJobs 'mark completed' on Error: for shell and Python steps the failure is logged and ignored; other steps still fail (reported)."""
    if not _ignores_failure(step):
        return
    if plan.kind in ("os", "python", "hvr_refresh"):
        plan.ignore_failure = True
        notes.append("on Error RunMyJobs marks this step completed: the failure is ignored (only logged) here, so the chain continues")
    else:
        notes.append(f"on Error RunMyJobs marks this step completed; not applied to a {plan.kind} step (a failure still fails it)")


def _wrap_ignore(command: str) -> str:
    """Run a shell command so its failure is logged and ignored (RunMyJobs 'mark completed' on error)."""
    return f'( {command}\n) || echo "RMJ: command failed (exit $?); RunMyJobs marks this step completed on error, so the chain continues"'


@dataclass
class Plan:
    """What one call (or standalone process) becomes, independent of the output form."""
    asset: str
    kind: str                       # os | python | sql | dbt_cloud | hvr_refresh | stub
    group: str
    command: str = ""
    params: dict = field(default_factory=dict)
    timeout: int | None = None
    run_as: str = ""
    conn_env: str = ""
    retries: int | None = None
    pool: str = ""
    deps: list = field(default_factory=list)
    description: str = ""
    meta: dict = field(default_factory=dict)
    status: str = CONVERTED
    stub_reason: str = ""
    stub_source: str = ""           # RedwoodScript source kept with the stub
    source: str = ""
    integ: dict = field(default_factory=dict)   # recognised integration details (dbt Cloud job id, HVR channel)
    exit_codes: list = field(default_factory=list)   # extra exit codes counted as success
    run_window: str = ""            # skip, not fail, outside this window (components output only)
    ignore_failure: bool = False    # RunMyJobs 'mark completed' on error: log the failure and carry on
    config_params: list = field(default_factory=list)   # parameters with no value in the export: python output takes them as run config


_UNMAPPED_REASONS = {
    "sap": "SAP connector job (a RunMyJobs SAP built-in): it needs an SAP integration, in Dagster or through the RunMyJobs connector",
}


def _plan_call(*, asset: str, proc: ProcessDef, call_params: dict, group: str,
               deps: list[str], retries: int | None, notes: list[str], source: str) -> Plan:
    params = {p.name: p.default for p in proc.parameters if p.direction in ("in", "in_out")}
    params.update(call_params)
    for p in proc.parameters:
        if p.direction in ("in", "in_out") and p.mandatory and params.get(p.name) is None:
            notes.append(f"mandatory parameter {p.name!r} has no value; it is passed as an empty string")
    params = {k: ("" if v is None else v) for k, v in params.items()}
    plan = Plan(asset=asset, kind="stub", group=group, params=params, deps=list(deps), description=proc.description,
                timeout=proc.timeout_seconds, run_as=proc.run_as, pool=slug(proc.queue) if proc.queue else "", source=source)
    kind = proc.type.lower()
    if kind in ("os", "python", "sql"):
        plan.kind, plan.command = kind, proc.command
        hit = detect(proc.command, proc.name, params) if kind in ("os", "python") else None
        if hit and hit["kind"] == "dbt_cloud" and hit["job_id"]:
            plan.kind, plan.integ, plan.status = "dbt_cloud", hit, REVIEW
            notes.append(f"recognised as dbt Cloud job {hit['job_id']} from the command text (heuristic, unverified); "
                         "set DBT_CLOUD_ACCOUNT_ID and DBT_CLOUD_TOKEN")
        elif hit:
            plan.integ = hit
            plan.status = REVIEW
            what = f"HVR refresh of channel {hit.get('channel')!r}" if hit["kind"] == "hvr_refresh" else "dbt Cloud (job id not found)"
            if hit["kind"] == "hvr_refresh":
                plan.kind = "hvr_refresh"
            notes.append(f"recognised as {what} from the command text (heuristic, unverified); converted as the original command")
        if kind == "sql":
            plan.conn_env = "RMJ_CONN_" + (slug(proc.connection or proc.queue or "default")).upper()
            notes.append(f"SQL step: set env var {plan.conn_env} to a SQLAlchemy URL for the target database "
                         f"(components output: define a resource named {slug(proc.connection or proc.queue or 'default')!r})")
    elif kind == "mail":
        plan.kind, plan.status = "mail", REVIEW
        notes.append("email step: sent over SMTP (set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM); attachments (JobFile, RelativeJob) are not sent; "
                     "check the recipients, which the export may leave to the caller")
    elif kind == "redwoodscript":
        plan.status = REVIEW
        plan.stub_reason = "RedwoodScript is not auto-translated; port the source (kept in the asset body) by hand or with the planned LLM pass"
        plan.stub_source = proc.command
        notes.append(plan.stub_reason)
    else:
        plan.status = UNMAPPED
        plan.stub_reason = _UNMAPPED_REASONS.get(proc.type, f"process type {proc.type!r} has no mapping (needs a dedicated integration)")
        pv = {k.upper(): str(v) for k, v in params.items() if v not in ("", None)}
        if proc.name == "SAP_AbapRun" and pv.get("JOBNAME"):
            plan.integ = {"sap": {"target_type": "background_job", "job_name": pv["JOBNAME"], "program_name": pv.get("ABAP_PROGRAM_NAME")}}
        elif proc.name == "SAP_BW_ProcessChain" and pv.get("NAME"):
            plan.integ = {"sap": {"target_type": "process_chain", "process_chain_id": pv["NAME"]}}
        shown = {k: (str(v)[:60] + ("..." if len(str(v)) > 60 else "")) for k, v in params.items() if v not in ("", None)}
        if shown:
            plan.stub_reason += "; call parameters: " + ", ".join(f"{k}={v!r}" for k, v in shown.items())
        notes.append(plan.stub_reason)
    review_notes = [n for n in proc.extraction_notes if not n.startswith("source: ")]
    if review_notes:
        notes.extend(review_notes)
        if plan.status == CONVERTED:
            plan.status = REVIEW
    if proc.max_starts > 1 and retries is None:
        retries = proc.max_starts - 1
        notes.append("'Maximum Starts' interpreted as total attempts; verify against how the source team uses it")
    plan.retries = retries
    plan.exit_codes = list(proc.success_exit_codes)
    plan.run_window = proc.run_window
    plan.meta = {"runmyjobs_source": source, "runmyjobs_application": proc.application}
    if proc.outputs:
        plan.meta["produces"] = ", ".join(proc.outputs)
    return plan


def _stub_plan(*, asset: str, group: str, deps: list[str], reason: str, source: str) -> Plan:
    return Plan(asset=asset, kind="stub", group=group, deps=list(deps), status=UNMAPPED, stub_reason=reason, source=source,
                meta={"runmyjobs_source": source})


def _emit_python(plan: Plan) -> str:
    kinds = {"os": '{"shell"}', "python": '{"python"}', "sql": '{"sql"}', "stub": '{"python"}',
             "dbt_cloud": '{"dbt"}', "hvr_refresh": '{"hvr"}', "mail": '{"email"}'}[plan.kind]
    if plan.kind in ("os", "hvr_refresh"):
        body = f"run_shell(context, {_literal(plan.command)}, {_literal(plan.params)}, timeout={_literal(plan.timeout)}, run_as={_literal(plan.run_as)}" + (f", ok_codes={tuple(plan.exit_codes)!r}" if plan.exit_codes else "") + (", ignore_failure=True" if plan.ignore_failure else "") + ")"
    elif plan.kind == "mail":
        body = f"run_mail(context, {_literal(plan.params)})"
    elif plan.kind == "dbt_cloud":
        body = f"run_dbt_cloud(context, {plan.integ['job_id']}, {_literal(plan.params)}, timeout={_literal(plan.timeout)})"
    elif plan.kind == "python":
        body = f"run_python(context, {_literal(plan.command)}, {_literal(plan.params)}, timeout={_literal(plan.timeout)}" + (", ignore_failure=True" if plan.ignore_failure else "") + ")"
    elif plan.kind == "sql":
        body = f"run_sql(context, {_literal(plan.command)}, {_literal(plan.params)}, {_literal(plan.conn_env)})"
    else:
        body = f"rmj_caveat({_literal(plan.stub_reason)})"
        if plan.stub_source:
            body += "\n    # --- original RedwoodScript ---\n" + textwrap.indent(plan.stub_source.rstrip(), "    # ")
    args = [f"name={_literal(plan.asset)}", f"group_name={_literal(plan.group)}"]
    if plan.description:
        args.append(f"description={_literal(plan.description)}")
    if plan.pool:
        args.append(f"pool={_literal(plan.pool)}")
    if plan.retries and plan.kind != "stub":
        args.append(f"retry_policy=dg.RetryPolicy(max_retries={plan.retries}, delay=60)")
    if plan.deps:
        args.append("deps=[" + ", ".join(_literal(d) for d in plan.deps) + "]")
    args.append(f"kinds={kinds}")
    args.append(f"metadata={_literal(plan.meta)}")
    code = "@dg.asset(\n    " + ",\n    ".join(args) + ",\n)\n"
    if plan.config_params and plan.kind in ("os", "python", "sql", "mail", "hvr_refresh"):
        cls = "".join(w.capitalize() for w in plan.asset.split("_")) + "Config"
        fields = "".join(f"    {n}: str = \"\"\n" for n in plan.config_params)
        body = body.replace(_literal(plan.params), f"rmj_config({_literal(plan.params)}, config)", 1)
        doc = f'    """Run config: {", ".join(plan.config_params)} (no value in the export; set at launch, blank keeps the default)."""\n'
        return (f"class {cls}(dg.Config):\n{doc}{fields}\n\n\n" + code
                + f"def {plan.asset}(context: dg.AssetExecutionContext, config: {cls}) -> None:\n    {body}\n")
    return code + f"def {plan.asset}(context: dg.AssetExecutionContext) -> None:\n    {body}\n"


def _coerces(value) -> bool:
    """True when Dagster's YAML resolver would turn this string into a number, boolean, list or other non-string (so a string-typed field rejects it):
    '5000', 'False', '1.5', '0x10', '1e3', '[1]'. Checked on the registry components in 0.10.173 to 0.10.176; '007', 'true', 'null' and 'yes' stay strings."""
    import ast
    try:
        return not isinstance(ast.literal_eval(str(value).strip()), str)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return False


def _env_split(command: str, params: dict) -> tuple[str, dict | None]:
    """(command, env_vars) for a component: parameters that would be coerced to non-strings cannot go in `env_vars`, so they are exported at the top of the command."""
    env = {f"RMJ_PARAM_{k.upper()}": str(v) for k, v in params.items()}
    safe = {k: v for k, v in env.items() if not _coerces(v)}
    exports = "".join(f"export {k}={_sh_quote(v)}\n" for k, v in env.items() if k not in safe)
    return exports + command, safe or None


def _sh_quote(v: str) -> str:
    import shlex
    return shlex.quote(v)


def _tag_value(v: str) -> str:
    """A tag value: only alphanumerics, _ - . and at most 63 characters, and never a number or boolean (those would be coerced)."""
    out = re.sub(r"[^0-9A-Za-z_.-]+", ".", v).strip(".")[:63]
    return f"v{out}"[:63] if _coerces(out) else out


def _render_params(text: str, params: dict) -> str:
    import string
    return string.Template(text).safe_substitute({k: v for k, v in params.items()})


def _component_doc(typ: str, attrs: dict, module: str = "dagster_community_components") -> str:
    import json
    return (f"type: {module}.{typ}\nattributes:\n"
            + "\n".join(f"  {k}: {json.dumps(v)}" for k, v in attrs.items() if v is not None))


def _emit_component(plan: Plan, pkg: str, files: dict[str, str]) -> str | None:
    """The registry-component document for a plan, or None when the plan has no component form (the caller then emits Python)."""
    if plan.kind == "stub":
        return None
    tag = _tag_value
    src_note = f"RunMyJobs source: {plan.meta.get('runmyjobs_source', '')}" + (f"; produces {plan.meta['produces']}" if plan.meta.get("produces") else "")
    common: dict = {"group_name": plan.group, "description": (plan.description + " " if plan.description else "") + f"[{src_note}]", "deps": plan.deps or None,
                    "asset_tags": {"runmyjobs_source": tag(plan.meta.get("runmyjobs_source", "")),
                                   **({"runmyjobs_application": tag(plan.meta["runmyjobs_application"])} if plan.meta.get("runmyjobs_application") else {})}}
    retry = {"retry_policy_max_retries": plan.retries, "retry_policy_delay_seconds": 60, "retry_policy_backoff": "linear"} if plan.retries else {}
    if plan.kind == "dbt_cloud":
        return None          # the registry's dbt Cloud trigger is an op-job, not an asset: generated Python (see component-wishlist)
    if plan.kind in ("os", "hvr_refresh"):
        cmd, env = _env_split(_wrap_ignore(_render_params(plan.command, plan.params)) if plan.ignore_failure else _render_params(plan.command, plan.params), plan.params)
        return _component_doc("RmjShellCommandAssetComponent", {
            "asset_name": plan.asset, "command": cmd,
            "env_vars": env,
            "timeout_seconds": plan.timeout, "pool": plan.pool or None, "kinds": ["hvr" if plan.kind == "hvr_refresh" else "shell"],
            "success_exit_codes": plan.exit_codes or None, "run_window": plan.run_window or None, **retry, **common}, f"{pkg}.components")
    if plan.kind == "python":
        files[f"scripts/{plan.asset}.py"] = _render_params(plan.command, plan.params)
        cmd, env = _env_split(_wrap_ignore(f"python src/{pkg}/scripts/{plan.asset}.py") if plan.ignore_failure else f"python src/{pkg}/scripts/{plan.asset}.py", plan.params)
        return _component_doc("RmjShellCommandAssetComponent", {
            "asset_name": plan.asset, "command": cmd,
            "env_vars": env,
            "timeout_seconds": plan.timeout, "pool": plan.pool or None, "kinds": ["python"],
            "success_exit_codes": plan.exit_codes or None, "run_window": plan.run_window or None, **retry, **common}, f"{pkg}.components")
    if plan.kind == "sap":
        return _component_doc("RmjSapJobTriggerAssetComponent", {
            "asset_key": plan.asset, "sap_rfc_resource_key": "sap_rfc", **plan.integ["sap"], "kinds": ["sap"],
            # XBP's BAPI_XBP_JOB_STATUS_GET returns one character (SAP's XBP 6.10 documentation): F finished, A cancelled, R active, Y ready, S released, P scheduled
            **({"success_statuses": ["F", "FINISHED"], "failure_statuses": ["A", "ABORTED", "CANCELLED"]} if plan.integ["sap"]["target_type"] == "background_job" else {}),
            "timeout_seconds": plan.timeout or None, "pool": plan.pool or None, "run_window": plan.run_window or None, **retry, **common}, f"{pkg}.components")
    if plan.kind == "mail":
        p = {k.lower(): ("" if v is None else str(v)) for k, v in plan.params.items()}
        to = [a.strip() for a in p.get("to", "").replace(";", ",").split(",") if a.strip()]
        if not to or "To" in plan.config_params or any("$" in a for a in to):
            return None                      # the recipient is supplied at run time: the generated step takes it as run config
        text = _render_params(p.get("text", ""), plan.params)
        html = text if text.lstrip().lower().startswith("<html") else None
        cc = [a.strip() for a in p.get("cc", "").replace(";", ",").split(",") if a.strip()]
        bcc = [a.strip() for a in p.get("bcc", "").replace(";", ",").split(",") if a.strip()]
        return _component_doc("RmjEmailSendAssetComponent", {
            "asset_key": plan.asset, "provider": "smtp", "resource_key": "smtp_resource", "to": to, "cc": cc or None, "bcc": bcc or None,
            "subject": _render_params(p.get("subject", ""), plan.params), "body": "This message is HTML." if html else text, "html_body": html,
            "kinds": ["email"], "pool": plan.pool or None, "run_window": plan.run_window or None, **retry, **common}, f"{pkg}.components")
    if plan.kind == "sql":
        return _component_doc("RmjSqlAssetComponent", {
            "asset_key": plan.asset, "connection_env": plan.conn_env,
            "sql": _render_params(plan.command, plan.params), "kinds": ["sql"],
            "pool": plan.pool or None, "timeout_seconds": plan.timeout, "run_window": plan.run_window or None, **retry, **common}, f"{pkg}.components")
    return None


def caveat_of(plan: Plan, status: str) -> str:
    return "stub" if plan.kind == "stub" else ("degraded" if status == REVIEW else "")


def _collapse_chain(chain: Chain, c_slug: str, model: Model, pkg: str, files: dict[str, str], status: str, notes: list[str]):
    """One TaskChainAssetComponent document for a chain, or the reason it cannot be one (a string).

    A chain collapses only when nothing is lost: every step has exactly one call, every call is a shell, SQL or Python process, and the
    settings the component applies to the whole chain (queue pool, retries, time window) agree across steps. The component stops at the first
    failed step, like a RunMyJobs chain does. Per-step timeouts become one chain timeout (their sum).
    """
    if not chain.steps:
        return "the chain has no steps"
    plans: list[tuple[Step, Plan, list[str]]] = []
    for step in chain.steps:
        if len(step.calls) != 1:
            return f"step {step.name!r} runs {len(step.calls)} calls in parallel, which one chain asset cannot express"
        call = step.calls[0]
        if call.process in model.chains:
            return f"step {step.name!r} calls the chain {call.process!r}"
        if call.process not in model.processes:
            return f"step {step.name!r} calls {call.process!r}, which is not in the export"
        if call.wait_events or call.param_refs or call.precondition:
            return f"step {step.name!r} waits for an event, has a precondition or takes a parameter from the chain, which one chain asset cannot express"
        proc = model.processes[call.process]
        step_notes: list[str] = []
        retries = _retries_from_handlers(step, step_notes)
        if step.precondition:
            step_notes.append(f"step {step.name!r} precondition {step.precondition!r} not enforced")
        plan = _plan_call(asset=f"{c_slug}__{slug(call.name)}", proc=proc, call_params=call.params, group=c_slug, deps=[], retries=retries,
                          notes=step_notes, source=f"chain:{chain.name}/step:{step.name}/call:{call.name}")
        if plan.kind not in ("os", "python", "sql"):
            return f"step {step.name!r} is a {plan.kind} step (it stays its own asset so it keeps its own lineage)"
        _apply_mark_completed(step, plan, step_notes)
        plans.append((step, plan, step_notes))
    if len({p.retries or 0 for _, p, _ in plans}) > 1:
        return "steps have different retry counts (the component retries the whole chain)"
    if len({p.pool for _, p, _ in plans}) > 1:
        return "steps use different queues/pools"
    windows = {p.run_window for _, p, _ in plans} | ({chain.run_window} if chain.run_window else set())
    windows.discard("")
    if len(windows) > 1:
        return "steps have different run windows"
    ids: set[str] = set()
    steps_doc: list[dict] = []
    rows: list[Item] = []
    caveat = ""
    total_timeout = 0
    for step, plan, step_notes in plans:
        sid = slug(step.calls[0].name)
        while sid in ids:
            sid += "_"
        ids.add(sid)
        if plan.kind == "sql":
            sd = {"id": sid, "type": "sql", "connection_env": plan.conn_env, "sql": _render_params(plan.command, plan.params)}
            step_notes.append(f"SQL step: set env var {plan.conn_env} to a SQLAlchemy URL for the target database")
        else:
            if plan.kind == "python":
                files[f"scripts/{plan.asset}.py"] = _render_params(plan.command, plan.params)
                cmd = f"python src/{pkg}/scripts/{plan.asset}.py"
            else:
                cmd = _render_params(plan.command, plan.params)
            cmd, env = _env_split(_wrap_ignore(cmd) if plan.ignore_failure else cmd, plan.params)
            sd = {"id": sid, "type": "command", "command": cmd}
            if env:
                sd["env_vars"] = env
            if plan.exit_codes:
                sd["success_exit_codes"] = plan.exit_codes
        steps_doc.append(sd)
        total_timeout += plan.timeout or 0
        st = plan.status
        if step_notes and st == CONVERTED and any(m in n for n in step_notes for m in _REVIEW_MARKERS):
            st = REVIEW
        if st != CONVERTED:
            status, caveat = (REVIEW if status == CONVERTED else status), "degraded"
        rows.append(Item("process_definition", f"{chain.name}/{step.calls[0].name} ({step.calls[0].process})", f"step {sid} of asset {c_slug}", st,
                         step_notes, {}, caveat_of(plan, st)))
    first = plans[0][1]
    window = windows.pop() if windows else ""
    if any(p.timeout for _, p, _ in plans):
        notes.append(f"per-step timeouts become one chain timeout of {total_timeout}s (the sum of the steps that have one)")
        status, caveat = (REVIEW if status == CONVERTED else status), "degraded"
    retries = first.retries
    retry = {"retry_policy_max_retries": retries, "retry_policy_delay_seconds": 60, "retry_policy_backoff": "linear"} if retries else {}
    tag = _tag_value(f"chain:{chain.name}")
    doc = _component_doc("RmjTaskChainAssetComponent", {
        "asset_key": c_slug, "steps": steps_doc, "group_name": c_slug,
        "description": ((chain.description + " ") if chain.description else "") + f"[RunMyJobs source: chain:{chain.name}]",
        "run_window": window or None, "pool": first.pool or None, "timeout_seconds": total_timeout or None,
        "asset_tags": {"runmyjobs_source": tag, **({"runmyjobs_application": _tag_value(chain.application)} if chain.application else {})},
        **retry}, f"{pkg}.components")
    notes.append(f"{len(steps_doc)} steps run in order as one asset and stop at the first failure; use chains=assets for one asset per call")
    return doc, [c_slug], status, caveat, rows



def _builtin(name: str) -> ProcessDef | None:
    """What a call to a RunMyJobs built-in (never part of an export) becomes, or None when the name is not a built-in family."""
    if name == "System_DoNothing":
        return ProcessDef(name=name, type="os", command=f"echo {name}: RunMyJobs built-in, does nothing", description="RunMyJobs built-in that does nothing")
    if name == "System_Sleep":
        return ProcessDef(name=name, type="os", command='sleep "$(awk "BEGIN {print ${Milliseconds}/1000}")"', description="RunMyJobs built-in that waits",
                          parameters=[Param("Milliseconds", default="0")],
                          extraction_notes=["built-in System_Sleep is reproduced as a real wait of the call's Milliseconds (seen in a real export); timer padding like this is usually better as a sensor or an upstream dependency"])
    if name == "System_Mail_Send":
        return ProcessDef(name=name, type="mail", description="RunMyJobs built-in that sends an email")
    if name.startswith("SAP_"):
        return ProcessDef(name=name, type="sap", description=f"RunMyJobs SAP connector job {name}")
    if name.startswith("System_"):
        return ProcessDef(name=name, type="other", description=f"RunMyJobs built-in {name}")
    return None


def _with_builtins(model: Model) -> Model:
    """Add a definition for each RunMyJobs built-in a chain calls: the export does not contain built-ins. A no-op call stays a no-op step, a sleep is a
    reviewed no-op, SAP jobs and mail are loud stubs that keep their call parameters; any other missing definition stays a stub saying to export it."""
    import dataclasses
    called = {c.process for ch in model.chains.values() for st in ch.steps for c in st.calls}
    add = {}
    for n in sorted(called):
        if n not in model.processes and n not in model.chains and (b := _builtin(n)) is not None:
            add[n] = b
    return dataclasses.replace(model, processes={**model.processes, **add}) if add else model


def convert(model: Model, output: str = "python", pkg: str = "rmj_migrated", chains: str = "task", dbt_jobs: dict[int, str] | None = None,
            dbt_model_jobs: set[int] | None = None) -> Result:
    """Convert the model. `output` is "python" (one generated module) or "components" (registry-component YAML, with generated Python
    only for what has no component form: loud stubs and the dbt Cloud trigger). `chains` is "task" (components output: a linear chain whose steps
    are all shell, SQL or Python and whose settings agree becomes one TaskChainAssetComponent) or "assets" (every call is its own asset). `dbt_model_jobs` lists the dbt Cloud job ids that keep per-model assets (the default is one asset per job). `dbt_jobs` maps a dbt Cloud job id to its name (from `fetch-dbt-jobs`): a dbt Cloud step whose id is in it
    becomes the mirrored executable job asset (components output) instead of a generated Python step."""
    model = _with_builtins(model)
    comp = output == "components"
    items: list[Item] = []
    blocks: list[str] = []             # python output: the module body
    fallback: list[str] = []           # components output: Python for assets with no component form
    docs: dict[str, list[str]] = {}    # components output: group directory -> component documents
    dbt_steps: dict[int, str] = {}     # dbt Cloud job id -> the RunMyJobs step that triggers it
    dbt_mirrored: dict[str, set] = {}  # mirrored asset key -> upstream asset keys the chain steps give it (post_processing deps)
    dbt_ids: dict[str, int] = {}       # mirrored asset key -> job id
    dbt_plans: dict[str, list[Plan]] = {}
    files: dict[str, str] = {}
    jobs: dict[str, str] = {}          # target name -> job name
    job_assets: dict[str, list[str]] = {}
    target_dir: dict[str, str] = {}    # target name -> the group directory its schedules go in
    target_item: dict[str, Item] = {}
    in_chain: set[str] = set()
    pools: dict[str, int | None] = {}
    for q in model.queues.values():
        pools[slug(q.name)] = q.max_concurrency

    def mirror_dbt(plan: Plan, taken: set[str], notes: list[str]) -> str:
        """A dbt Cloud step whose job id is in --dbt-jobs becomes the component's mirrored executable asset (renamed to that asset's key);
        returns the step's asset name. A job already used earlier in the same chain keeps its generated Python step (one asset cannot be two steps)."""
        if not (comp and plan.kind == "dbt_cloud" and dbt_jobs and plan.integ.get("job_id") in dbt_jobs):
            return plan.asset
        key = mirrored_asset_key(dbt_jobs[plan.integ["job_id"]])
        if key in taken:
            notes.append(f"dbt Cloud job {plan.integ['job_id']} is called twice in this chain; the second call stays a generated Python step")
            return plan.asset
        plan.integ["mirrored"] = True
        plan.asset = key
        notes.append(f"runs as the mirrored dbt Cloud job asset {key!r} (job {plan.integ['job_id']}, from --dbt-jobs); upstream steps are attached with post_processing deps")
        return key

    def emit(plan: Plan, group_dir: str) -> str:
        """Emit one plan in the chosen form; returns a note when a component form was not available."""
        if comp and plan.kind == "dbt_cloud":
            dbt_steps.setdefault(plan.integ["job_id"], plan.source)
            if plan.integ.get("mirrored"):                # the component's own executable asset runs the job; the step is only its upstream deps
                dbt_mirrored.setdefault(plan.asset, set()).update(plan.deps)
                dbt_ids[plan.asset] = plan.integ["job_id"]
                dbt_plans.setdefault(plan.asset, []).append(plan)
                return ""
        if comp and plan.kind == "stub" and plan.integ.get("sap"):
            plan.kind, plan.status, plan.stub_reason = "sap", REVIEW, ""
            call_note = ("SAP job: SapJobTriggerAssetComponent starts it over XBP/RFC and polls to completion; set SAP_HOST, SAP_SYSNR, SAP_CLIENT, SAP_USER, SAP_PASSWORD; "
                         "not run against a live SAP system by anyone yet" + ("; the RunMyJobs CLIENT parameter belongs in the connection" if plan.integ["sap"]["target_type"] == "process_chain" else ""))
            plan.meta["sap_note"] = call_note
        if comp:
            doc = _emit_component(plan, pkg, files)
            if doc is not None:
                docs.setdefault(group_dir, []).append(doc)
                return ""
            fallback.append(_emit_python(plan))
            return "" if plan.kind == "stub" else ("generated Python trigger (this job id is not in --dbt-jobs, so the mirrored asset's key is unknown; run fetch-dbt-jobs and convert with --dbt-jobs)" if plan.kind == "dbt_cloud" else "no component form: generated Python")
        blocks.append(_emit_python(plan))
        if plan.run_window:
            return f"run_window {plan.run_window!r} is not enforced in python output (use --output components)"
        return ""

    # chains -> assets with ordered deps (+ a job in python output; schedule components create the job in components output)
    for chain in model.chains.values():
        if chain.inline:                         # a System_Internal_Chain wrapper: it only runs inlined in the chain that calls it
            continue
        c_slug = slug(chain.name)
        notes: list[str] = list(chain.notes)
        status = CONVERTED
        caveat = ""
        if chain.notes:
            status, caveat = REVIEW, "degraded"
        if chain.precondition:
            status, caveat = REVIEW, "degraded"
            notes.append(f"chain precondition {chain.precondition!r} not enforced; model as a schedule window or sensor")
        if comp and chains == "task":
            collapsed = _collapse_chain(chain, c_slug, model, pkg, files, status, notes)
            if isinstance(collapsed, tuple):
                doc, names, status, caveat, rows = collapsed
                docs.setdefault(c_slug, []).append(doc)
                items.extend(rows)
                jobs[chain.name], job_assets[chain.name], target_dir[chain.name] = c_slug + "_job", names, c_slug      # the job needs a name other than the chain asset's op
                for n_ in {p for p in model.processes if p in {c_.process for st_ in chain.steps for c_ in st_.calls}}:
                    in_chain.add(n_)
                if status == REVIEW and not notes:
                    notes.append("one or more steps need review (see the rows for this chain)")
                item = Item("chain", chain.name, f"asset {c_slug} (TaskChainAssetComponent)", status, notes,
                            {"assets": names, "asset": c_slug, "deps": []}, caveat)
                items.append(item)
                target_item[chain.name] = item
                continue
            if collapsed:
                notes.append(f"kept as one asset per call: {collapsed}")
        names: list[str] = []
        used: set[str] = set()

        def unique(asset: str) -> str:
            n, k = asset, 2
            while n in used:
                n, k = f"{asset}_{k}", k + 1
            used.add(n)
            return n

        def walk(ch: Chain, prefix: str, bound: dict, prev: list[str], stack: tuple[str, ...]) -> list[str]:
            """Add the assets of one chain's steps after `prev`; returns the assets the chain ends with. A call to another chain is inlined: its steps
            become assets of this chain (named with the call's prefix) and the next step waits for the last of them."""
            nonlocal status, caveat
            for step in ch.steps:
                step_notes: list[str] = []
                retries = _retries_from_handlers(step, step_notes)
                if step.precondition:
                    step_notes.append(f"step {step.name!r} precondition {step.precondition!r} not enforced")
                cur: list[str] = []
                for call in step.calls:
                    asset = unique(f"{c_slug}__{prefix}{slug(call.name)}")
                    where = f"chain:{chain.name}/step:{step.name}/call:{call.name}"
                    values = dict(call.params)
                    missing = []
                    missing_names = []
                    for local, src in call.param_refs.items():
                        if src in bound:
                            values[local] = bound[src]
                        else:
                            missing.append(f"{local!r} (from {src!r})")
                            missing_names.append(local)
                    if call.process in model.chains:
                        inner = model.chains[call.process]
                        if call.process in stack or ch.name == call.process:
                            plan = _stub_plan(asset=asset, group=c_slug, deps=prev, source=where,
                                              reason=f"chain {call.process!r} calls itself (directly or through another chain); it cannot be inlined")
                            emit(plan, c_slug)
                            names.append(asset)
                            cur.append(asset)
                            items.append(Item("process_definition", f"{chain.name}/{call.name}", asset, UNMAPPED, [plan.stub_reason],
                                              {"asset": asset, "deps": list(prev)}, "stub"))
                            status, caveat = (REVIEW if status == CONVERTED else status), caveat or "degraded"
                            continue
                        inner_bound = {p.name: p.default for p in inner.parameters if p.default is not None}
                        inner_bound.update(values)
                        notes.append(f"step {step.name!r} calls chain {call.process!r}: its steps are inlined here as assets named {c_slug}__{prefix}{slug(call.name)}__*"
                                      + ("; " + ", ".join(f"parameter {m} has no value here (the caller supplies it when the chain is submitted)" for m in missing) if missing else ""))
                        if call.wait_events:
                            status, caveat = (REVIEW if status == CONVERTED else status), caveat or "degraded"
                            notes.append(f"the call to {call.process!r} waits for event(s) {', '.join(w['event'] for w in call.wait_events)}: not enforced (see the first inlined step)")
                        out = walk(inner, f"{prefix}{slug(call.name)}__", inner_bound, prev, stack + (ch.name,))
                        cur.extend(out)
                        continue
                    if call.process not in model.processes:
                        plan = _stub_plan(asset=asset, group=c_slug, deps=prev, source=where,
                                          reason=f"definition {call.process!r} is not in this export: export it too (or it is a built-in with no mapping)")
                        emit(plan, c_slug)
                        names.append(asset)
                        cur.append(asset)
                        items.append(Item("process_definition", f"{chain.name}/{call.name} ({call.process})", asset, UNMAPPED, [plan.stub_reason],
                                          {"asset": asset, "deps": list(prev)}, "stub"))
                        status, caveat = (REVIEW if status == CONVERTED else status), caveat or "degraded"
                        continue
                    proc = model.processes[call.process]
                    in_chain.add(proc.name)
                    call_notes = list(step_notes)
                    if missing:
                        call_notes.append(f"parameter(s) {', '.join(missing)} are not set here: the caller supplies them when the chain is submitted (no value is available)")
                    if call.precondition:
                        pre = call.precondition
                        call_notes.append(f"runs only if {('time window ' + pre['time_window'] + ' is ' + pre['status'].replace('Is', '').lower()) if pre.get('time_window') else 'its precondition holds'}"
                                          + (f" ({pre['time_zone']})" if pre.get("time_zone") else "") + (f", expression {pre['expression']!r}" if pre.get("expression") else "")
                                          + ": not enforced (the time window is not in the export; model it as a schedule or a run_window)")
                    for w in call.wait_events:
                        call_notes.append(f"waits for event {w['event']!r}" + (" and clears it" if w["clears"] else "")
                                          + ": not enforced (Dagster has no named event flags; model it as a sensor or an upstream asset)")
                    plan = _plan_call(asset=asset, proc=proc, call_params=values, group=c_slug, deps=prev, retries=retries,
                                      notes=call_notes, source=where)
                    plan.config_params = [n for n in missing_names if re.fullmatch(r'[A-Za-z_]\w*', n)]
                    if plan.config_params and not comp:
                        call_notes.append(f"run config: {', '.join(plan.config_params)} can be set at launch (blank keeps the default)")
                    asset = mirror_dbt(plan, set(names), call_notes)
                    _apply_mark_completed(step, plan, call_notes)
                    st = plan.status
                    if call_notes and st == CONVERTED:
                        st = REVIEW if any(m in n for n in call_notes for m in _REVIEW_MARKERS) else CONVERTED
                    before = plan.status
                    extra = emit(plan, c_slug)
                    if extra:
                        call_notes.append(extra)
                    if plan.meta.get("sap_note"):
                        call_notes.append(plan.meta.pop("sap_note"))
                    if plan.status != before:               # emit() promoted a stub to a component (SAP)
                        st = plan.status
                    names.append(asset)
                    cur.append(asset)
                    items.append(Item("process_definition", f"{chain.name}/{call.name} ({proc.name})", asset, st, call_notes,
                                      {"asset": asset, "deps": list(prev)}, caveat_of(plan, st)))
                    if st != CONVERTED:
                        status = REVIEW if status == CONVERTED else status
                        caveat = caveat or "degraded"
                prev = cur or prev
            return prev

        walk(chain, "", {p.name: p.default for p in chain.parameters if p.default is not None}, [], (chain.name,))
        if names:
            jobs[chain.name] = c_slug
            job_assets[chain.name] = names
            target_dir[chain.name] = c_slug
            if not comp:
                sel = ", ".join(_literal(n) for n in names)
                blocks.append(f"{c_slug}_job = dg.define_asset_job({_literal(c_slug)}, selection=dg.AssetSelection.assets({sel}))\n")
        if status == REVIEW and not notes:
            notes.append("one or more calls need review (see the rows for this chain)")
        item = Item("chain", chain.name, f"job {c_slug}" if names else "(none)", status, notes,
                    ({"job": c_slug, "assets": names} if not comp else {"assets": names}) if names else {}, caveat)
        items.append(item)
        target_item[chain.name] = item

    # standalone processes: not in any chain, or directly scheduled
    scheduled = {s.target for s in model.schedules}
    for proc in model.processes.values():
        if proc.name in in_chain and proc.name not in scheduled:
            continue
        notes = []
        asset = slug(proc.name)
        group = slug(proc.application) or "runmyjobs"
        plan = _plan_call(asset=asset, proc=proc, call_params={}, group=group, deps=[], retries=None, notes=notes, source=f"process:{proc.name}")
        asset = mirror_dbt(plan, set(), notes)
        extra = emit(plan, group)
        if extra:
            notes.append(extra)
        if proc.name in scheduled:
            jobs[proc.name] = asset + "_job"
            job_assets[proc.name] = [asset]
            target_dir[proc.name] = group
            if not comp:
                blocks.append(f"{asset}_job = dg.define_asset_job({_literal(asset + '_job')}, selection=dg.AssetSelection.assets({_literal(asset)}))\n")
        items.append(Item("process_definition", proc.name, asset, plan.status, notes, {"asset": asset, "deps": []}, caveat_of(plan, plan.status)))

    # schedules
    sched_blocks: list[str] = []
    job_uses: dict[str, int] = {}
    for s in model.schedules:
        job = jobs.get(s.target)
        if job is None:
            items.append(Item("schedule", s.name, "(none)", UNMAPPED, [f"target {s.target!r} produced no job"], caveat="stub"))
            continue
        notes = []
        status = CONVERTED
        tz = s.time_zone or "UTC"
        if "/" not in tz and tz.upper() != "UTC":
            notes.append(f"time zone {tz!r} is not an IANA name; confirm the mapping")
            status = REVIEW
        if s.end and not comp:
            notes.append(f"submit frame end date {s.end} not enforced; remove the schedule at that date")
            status = REVIEW
        crons = []
        for el in s.elements:
            cron, n = element_to_cron(el)
            notes.extend(n)
            if cron is None:
                status = REVIEW
            else:
                crons.append(cron)
            if n:
                status = REVIEW if status == CONVERTED and any("approx" in x or "mapped" in x or "reset" in x for x in n) else status
        exp_scheds = []
        for i, cron in enumerate(crons, 1):
            name = slug(s.name) + (f"_{i}" if len(crons) > 1 else "")
            exp_scheds.append({"name": name, "cron": cron})
            if comp:
                n_used = job_uses.get(s.target, 0)
                job_uses[s.target] = n_used + 1
                job_name = job if n_used == 0 else f"{job}_{n_used + 1}"      # each schedule component defines its own job
                if n_used == 0 and s.target in target_item:
                    target_item[s.target].expected["job"] = job
                docs.setdefault(target_dir[s.target], []).append(_component_doc("CronScheduleComponent", {
                    "schedule_name": name, "cron_expression": cron, "asset_keys": job_assets[s.target], "job_name": job_name,
                    "execution_timezone": tz, "end_date": s.end or None, "default_status": "STOPPED"}))
            else:
                sched_blocks.append(
                    f"{slug(s.name)}_{i} = dg.ScheduleDefinition(name={_literal(name)}, cron_schedule={_literal(cron)}, "
                    f"job={job + ('_job' if s.target in model.chains else '')}, execution_timezone={_literal(tz)})\n")
        if len(s.elements) > 1:
            notes.append("multiple submit-frame elements become one Dagster schedule each (union of cycles)")
        items.append(Item("schedule", s.name, f"{len(crons)} schedule(s)" if crons else "(none)",
                          status if crons else UNMAPPED, notes, {"schedules": exp_scheds},
                          "" if crons and status == CONVERTED else ("degraded" if crons else "stub")))
    blocks.extend(sched_blocks)

    # file events -> a filesystem sensor that starts the target's job (components output). Components output defines jobs only through schedule
    # components, so each event gets its own job in defs/jobs.py (named <target job>_on_<event>), which cannot clash with a schedule's job.
    event_jobs: list[str] = []
    for fe in model.file_events:
        if not comp:
            items.append(Item("file_event", fe.name, "(none)", UNMAPPED, ["file events are converted in components output only (--output components)"], caveat="stub"))
            continue
        base = jobs.get(fe.target)
        if base is None:
            items.append(Item("file_event", fe.name, "(none)", UNMAPPED, [f"target {fe.target!r} produced no job"], caveat="stub"))
            continue
        sensor, job = slug(fe.name), f"{slug(base)}_on_{slug(fe.name)}"
        notes = []
        status = CONVERTED
        if not fe.min_size_bytes and not fe.min_age_seconds and not fe.stable:
            notes.append("no minimum size, age or stability check: a file still being written can start the job (RunMyJobs files events usually have one; check the source)")
            status = REVIEW
        notes.append("the sensor starts stopped; its cursor remembers files already seen, so turn it on once the parallel run starts")
        regex = "^" + "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in fe.pattern) + "$"
        docs.setdefault(target_dir[fe.target], []).append(_component_doc("FilesystemMonitorSensorComponent", {
            "sensor_name": sensor, "directory_path": fe.directory, "file_pattern": regex, "job_name": job,
            "minimum_interval_seconds": fe.poll_seconds, "recursive": fe.recursive or None, "batch_into_one_run": fe.one_run_per_poll or None,
            "default_status": "stopped", "min_age_seconds": fe.min_age_seconds, "min_size_bytes": fe.min_size_bytes,
            "require_stable_size": fe.stable or None}))
        sel = ", ".join(_literal(n) for n in job_assets[fe.target])
        event_jobs.append(f"dg.define_asset_job({_literal(job)}, selection=dg.AssetSelection.assets({sel}))")
        items.append(Item("file_event", fe.name, f"sensor {sensor} -> job {job}", status, notes, {"sensor": sensor, "job": job},
                          "" if status == CONVERTED else "degraded"))
    if event_jobs:
        files["defs/jobs.py"] = "import dagster as dg\n\n\n@dg.definitions\ndef defs() -> dg.Definitions:\n    return dg.Definitions(jobs=[\n        " + ",\n        ".join(event_jobs) + ",\n    ])\n"

    for q in model.queues.values():
        lim = f"limit {q.max_concurrency}" if q.max_concurrency else "no limit declared"
        items.append(Item("queue", q.name, f"pool {slug(q.name)}", CONVERTED if q.max_concurrency else REVIEW,
                          [f"configure Dagster pool {slug(q.name)!r} with {lim}"],
                          {"pool": slug(q.name), "limit": q.max_concurrency}, "" if q.max_concurrency else "degraded"))

    header = 'import dagster as dg\n\nfrom ..runtime import rmj_caveat, rmj_config, run_dbt_cloud, run_mail, run_python, run_shell, run_sql  # noqa: F401\n\n\n'
    if not comp:
        return Result(code=header + "\n\n".join(blocks), items=items, pools=pools, questions=chain_questions(model))
    if dbt_steps:
        workspace = {"account_id": "{{ env.DBT_CLOUD_ACCOUNT_ID }}", "project_id": "{{ env.DBT_CLOUD_PROJECT_ID }}",
                     "environment_id": "{{ env.DBT_CLOUD_ENVIRONMENT_ID }}", "token": "{{ env.DBT_CLOUD_TOKEN }}"}
        creds = ("set DBT_CLOUD_ACCOUNT_ID, DBT_CLOUD_PROJECT_ID, DBT_CLOUD_ENVIRONMENT_ID, DBT_CLOUD_TOKEN; the component lists the account's jobs from the "
                 "API when the project loads, so `validate` needs them too")
        with_models = set(dbt_model_jobs or ())
        cls = "RmjEnrichedDbtCloudWorkspaceComponent"

        def ws_doc(ids: set[int], mirror: str, models: bool, settings: dict) -> str:
            """One workspace component for a set of jobs. Without `models` the project's per-model assets are switched off (select matches nothing),
            so each Cloud job is priced and shown as one asset; with it the per-model lineage, checks and events stay on."""
            attrs = {"workspace": workspace, "mirror_jobs": mirror, "job_selection_include": " ".join(f"id:{i}" for i in sorted(ids)),
                     "select": None if models else "tag:__never_exists__", **settings}
            doc = _component_doc(cls, attrs, f"{pkg}.components")
            post = [(k, sorted(v)) for k, v in sorted(dbt_mirrored.items()) if v and dbt_ids[k] in ids]
            if post:
                doc += "\npost_processing:\n  assets:\n" + "\n".join(
                    f"    - target: {json.dumps('key:' + k)}\n      attributes:\n        deps: {json.dumps(v)}" for k, v in post)
            return doc

        if dbt_mirrored:
            # The mirrored job asset is executable (materializing it triggers the Cloud job, polls, fails with the run URL), so it IS the chain step:
            # the steps before it are attached as deps with post_processing. Only these jobs are mirrored; unmapped ones keep a generated Python step.
            plans = [p for ps in dbt_plans.values() for p in ps]
            settings: dict = {}
            if len({p.pool for p in plans}) == 1 and plans[0].pool:
                settings["pool"] = plans[0].pool
            if len({p.retries or 0 for p in plans}) == 1 and plans[0].retries:
                settings.update(retry_policy_max_retries=plans[0].retries, retry_policy_delay_seconds=60, retry_policy_backoff="linear")
            ids = set(dbt_ids.values())
            docs["dbt_cloud"] = [ws_doc(g, "executable", models, settings) for g, models in ((ids - with_models, False), (ids & with_models, True)) if g]
            for it in items:                                   # gate 3 checks the mirrored asset's deps: the union over every step that uses it
                if it.expected.get("asset") in dbt_mirrored:
                    it.expected["deps"] = sorted(dbt_mirrored[it.expected["asset"]])
            items.append(Item("integration", "dbt Cloud jobs " + ", ".join(map(str, sorted(ids))), "defs/dbt_cloud/defs.yaml", REVIEW,
                              ["RmjEnrichedDbtCloudWorkspaceComponent (mirror_jobs: executable) makes each job a materializable asset; " + creds
                               + (f". Per-model assets are on for job(s) {', '.join(map(str, sorted(ids & with_models)))} and off for the rest (one asset per job)"
                                  if ids & with_models else ". Per-model assets are off: each job is one asset (use --dbt-model-jobs to keep them for a job)")],
                              {}, "degraded"))
        else:
            # No job names (--dbt-jobs), so the asset key is unknown and the chain keeps its generated Python trigger steps. The component still
            # mirrors each referenced job as an asset and a launchable job, for the catalog.
            ids = set(dbt_steps)
            docs["dbt_cloud"] = [ws_doc(g, "both", models, {}) for g, models in ((ids - with_models, False), (ids & with_models, True)) if g]
            items.append(Item("integration", "dbt Cloud jobs " + ", ".join(map(str, sorted(ids))), "defs/dbt_cloud/defs.yaml", REVIEW,
                              ["RmjEnrichedDbtCloudWorkspaceComponent (mirror_jobs: both) mirrors each job as an asset and a job; " + creds
                               + ". To run the jobs through the mirrored asset instead of generated Python steps, run `fetch-dbt-jobs` and convert with --dbt-jobs"],
                              {}, "degraded"))
    alldocs = "\n".join(d for ds in docs.values() for d in ds)
    res_docs = []
    if "RmjSapJobTriggerAssetComponent" in alldocs:
        res_docs.append(_component_doc("SapRFCResourceComponent", {"resource_key": "sap_rfc", "ashost": "{{ env.SAP_HOST or 'SET_SAP_HOST' }}",
                        "sysnr": "{{ env.SAP_SYSNR or 'SET_SAP_SYSNR' }}", "client": "{{ env.SAP_CLIENT or 'SET_SAP_CLIENT' }}",
                        "user": "{{ env.SAP_USER or 'SET_SAP_USER' }}", "passwd_env_var": "SAP_PASSWORD"}))
        items.append(Item("integration", "SAP connection", "defs/integrations/defs.yaml", REVIEW,
                          ["SapRFCResourceComponent reads SAP_HOST, SAP_SYSNR, SAP_CLIENT, SAP_USER and the password from SAP_PASSWORD; needs the SAP NetWeaver RFC SDK and pyrfc on the code location; "
                           "use mshost/sysid/group for a load-balanced system (a placeholder SET_* value loads but fails at run time)"], {}, "degraded"))
    if "RmjEmailSendAssetComponent" in alldocs:
        res_docs.append(_component_doc("SMTPResourceComponent", {"resource_key": "smtp_resource", "host": "{{ env.SMTP_HOST or 'SET_SMTP_HOST' }}", "username": "{{ env.SMTP_USER or 'SET_SMTP_USER' }}",
                        "password_env_var": "SMTP_PASSWORD"}))
        items.append(Item("integration", "SMTP connection", "defs/integrations/defs.yaml", REVIEW,
                          ["SMTPResourceComponent reads SMTP_HOST, SMTP_USER and the password from SMTP_PASSWORD; attachments (JobFile) are not sent"], {}, "degraded"))
    if res_docs:
        docs["integrations"] = res_docs
    for group_dir, ds in docs.items():
        files[f"defs/{group_dir}/defs.yaml"] = "\n---\n".join(ds) + "\n"
    code = ""
    if fallback:
        files["defs/python_assets/__init__.py"] = ""
        code = header.replace("from ..runtime", "from ...runtime") + "\n\n".join(fallback)
    return Result(code=code, items=items, pools=pools, output="components", files=files, questions=chain_questions(model))


def write_project(result: Result, out_dir: Path, pkg: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    comp = result.output == "components"
    registry = f'registry_modules = ["{pkg}.components"]' if comp else ""
    deps = '"dagster>=1.11", "sqlalchemy>=2"' + (', "dagster-community-components>=0.10.177"' if comp else "") + (', "dagster-dbt"' if "defs/dbt_cloud/defs.yaml" in result.files else "")
    root = out_dir / "src" / pkg
    (root / "defs").mkdir(parents=True, exist_ok=True)
    (out_dir / "pyproject.toml").write_text(textwrap.dedent(f'''\
        [project]
        name = "{pkg}"
        version = "0.1.0"
        requires-python = ">=3.10"
        dependencies = [{deps}]

        [dependency-groups]
        dev = ["dagster-webserver", "dagster-dg-cli"]

        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [tool.hatch.build.targets.wheel]
        packages = ["src/{pkg}"]

        [tool.dg]
        directory_type = "project"

        [tool.dg.project]
        root_module = "{pkg}"
        {registry}
        '''))
    (root / "__init__.py").write_text("")
    (root / "defs" / "__init__.py").write_text("")
    (root / "runtime.py").write_text(RUNTIME_SOURCE)
    (root / "definitions.py").write_text(textwrap.dedent('''\
        from pathlib import Path

        from dagster import definitions, load_from_defs_folder


        @definitions
        def defs():
            return load_from_defs_folder(path_within_project=Path(__file__).parent)
        '''))
    if comp:
        (root / "components.py").write_text(COMPONENTS_SOURCE)
        for rel, content in result.files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        if result.code:
            (root / "defs" / "python_assets" / "assets.py").write_text(result.code)
    else:
        (root / "defs" / "runmyjobs.py").write_text(result.code)
    (out_dir / "MIGRATION.md").write_text(render_report(result))


def render_report(result: Result) -> str:
    counts = {CONVERTED: 0, REVIEW: 0, UNMAPPED: 0}
    for i in result.items:
        counts[i.status] += 1
    total = sum(counts.values())
    total = sum(counts.values())
    rate = 100 * counts[CONVERTED] / total if total else 100
    stubs = sum(1 for i in result.items if i.caveat == "stub")
    degraded = sum(1 for i in result.items if i.caveat == "degraded")
    lines = [
        "# Migration report", "",
        f"{total} source objects: **{counts[CONVERTED]} converted**, **{counts[REVIEW]} need review**, "
        f"**{counts[UNMAPPED]} unmapped** ({rate:.0f}% converted without review).", "",
        f"Caveats: **{stubs} stub(s)** (the asset exists in the right place and fails with `RMJ-CAVEAT: <reason>` until it is ported) and "
        f"**{degraded} converted with a known difference** (listed in the Notes column). Nothing is silently dropped.", "",
        "Converted means the generated code loads and encodes the same structure. It does not mean behavior was verified "
        "against RunMyJobs; run both in parallel before cutover.", "",
        "| Kind | Source | Dagster artifact | Status | Notes |", "|---|---|---|---|---|",
    ]
    order = {UNMAPPED: 0, REVIEW: 1, CONVERTED: 2}
    for i in sorted(result.items, key=lambda x: (order[x.status], x.kind, x.source)):
        lines.append(f"| {i.kind} | {i.source} | {i.artifact} | {i.status} | {'; '.join(i.notes).replace('|', '/')} |")
    if result.pools:
        lines += ["", "## Pools (RunMyJobs queues)", ""]
        for p, lim in result.pools.items():
            lines.append(f"- `{p}`: {'limit ' + str(lim) if lim else 'no limit declared'}")
        limited = {p: lim for p, lim in result.pools.items() if lim}
        lines += ["", "A pool's limit caps the steps running at once across all runs (the default granularity is `op`). "
                  "Pools have no limit until one is set. Apply the limits to the deployment:", "", "```bash"]
        lines += [f"dagster instance concurrency set {p} {lim}" for p, lim in limited.items()] or ["# no queue declared a limit"]
        lines += ["```", "", "Or in `dagster.yaml` (open source) / deployment settings (Dagster+), and enable run monitoring so a cancelled "
                  "or failed run releases its slots (without it a pool can deadlock):", "", "```yaml", "run_monitoring:",
                  "  enabled: true", "  free_slots_after_run_end_seconds: 300", "```"]
    lines += render_questions(result.questions)
    return "\n".join(lines) + "\n"
