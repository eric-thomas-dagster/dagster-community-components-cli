"""Convert a Factory into a Dagster project (generated Python over runtime.py).

One asset per pipeline: the activity graph becomes generated functions run in dependency order, with ADF's condition semantics (a dependency
on Failed or Completed handles a failure, Skipped propagates). Every activity is translated, translated with a stated caveat, or a stub that
fails with `ADF-CAVEAT: <reason>`; nothing is dropped silently.
"""
from __future__ import annotations

import json
import re
import textwrap
from datetime import datetime, timezone

import yaml
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .expr import Untranslatable, is_dynamic, translate
from .tzmap import WINDOWS_TZ
from .families import MSSQL_SOURCES, SQL_LS, SQL_SINKS, SQL_SOURCES, source_query
from .model import Activity, Factory, Pipeline, Trigger, parse_activities
from .components_src import COMPONENTS_SOURCE
from .questions import pipeline_questions, render_questions
from .split import plan as plan_split

CONVERTED, REVIEW, UNMAPPED = "converted", "review", "unmapped"

RUNTIME_SOURCE = (resources.files("adf_to_dagster") / "runtime.py").read_text()
DFLOW_SOURCE = (resources.files("adf_to_dagster") / "dflow.py").read_text()

_ADF_TYPES = {"int": "int_", "integer": "int_", "float": "float_", "double": "float_", "bool": "bool_", "boolean": "bool_"}
FILE_FAMILY = {"Binary": "Binary", "DelimitedText": "DelimitedText", "Json": "Json", "Parquet": "Parquet", "Avro": "Avro", "Orc": "Orc", "Xml": "Xml"}
FS_LS = {"AzureBlobStorage": "abfs", "AzureBlobFS": "abfs", "AzureStorage": "abfs", "AmazonS3": "s3", "FileServer": "file"}
_FMT_DEFAULTS = {"columnDelimiter": ",", "rowDelimiter": None, "quoteChar": '"', "escapeChar": "\\", "firstRowAsHeader": False, "encodingName": "UTF-8",
                 "compressionCodec": None, "compressionLevel": None, "nullValue": ""}
STUB_HINT = {
    "ExecuteDataFlow": "a Mapping Data Flow is a visual Spark graph with no mechanical translation: rewrite it in SQL or Spark, or keep running it in ADF",
    "ExecuteWranglingDataflow": "a Power Query data flow has no mechanical translation",
    "ExecuteSSISPackage": "SsisWorkspaceComponent (registry) runs a package deployed to an SSISDB catalog; it needs the catalog reachable",
    "SynapseNotebook": "a Synapse notebook run has no component yet",
    "PBISemanticModelRefresh": "SemanticModelRefreshAssetComponent (registry) refreshes a semantic model; it needs a literal operation type and literal options, and a pipeline that can be split",
    "RefreshDataflow": "a Fabric dataflow refresh: the export's typeProperties are empty here, so the dataflow is not known (fabric_workspace can run Dataflow items)",
    "TridentNotebook": "a Fabric notebook needs its notebookId and workspace in the export",
    "SparkJob": "a Synapse Spark job definition run has no component yet",
    "HDInsightSpark": "an HDInsight Spark step has no component yet",
    "DatabricksNotebook": "needs an AzureDatabricks linked service with an existing cluster id or basic new-cluster settings in the export",
    "DatabricksSparkJar": "databricks_resource (registry) can run a job once the workspace is known",
    "DatabricksSparkPython": "databricks_resource (registry) can run a job once the workspace is known",
    "Copy": "Copy is translated for same-format file copies (blob, ADLS Gen2, S3, file server), REST to file, delimited text to SQL and SQL to delimited text; other pairs have no translation yet",
    "Delete": "a Delete is translated over blob, ADLS Gen2, S3 and file-server datasets",
    "GetMetadata": "GetMetadata is translated over blob, ADLS Gen2, S3 and file-server datasets for exists, itemName, itemType, size, lastModified and childItems",
    "Validation": "Validation is translated over blob, ADLS Gen2, S3 and file-server datasets",
}


def slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower() or "x"
    return ("_" + s) if s[0].isdigit() else s


@dataclass
class Item:
    kind: str
    source: str
    artifact: str
    status: str
    notes: list[str] = field(default_factory=list)
    caveat: str = ""               # "" | "stub" | "degraded"
    expected: dict = field(default_factory=dict)


@dataclass
class Result:
    code: str
    items: list[Item]
    questions: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)       # relative path under defs/ -> content (component YAML)


def _dur_seconds(v, default: int = 0) -> int:
    """ADF timespan 'd.hh:mm:ss' or 'hh:mm:ss'."""
    m = re.fullmatch(r"(?:(\d+)\.)?(\d+):(\d+):(\d+)(?:\.(\d+))?", str(v or ""))
    if not m:
        return default
    d, h, mi, s = (int(x or 0) for x in m.groups()[:4])
    return d * 86400 + h * 3600 + mi * 60 + s


def _pyid(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s))


def ls_env(name: str) -> str:
    return "ADF_CONN_" + re.sub(r"\W", "_", name).upper()


# ----------------------------------------------------------------------------------------------------------------------------------------------
# schedules
# ----------------------------------------------------------------------------------------------------------------------------------------------

def _start(rec: dict):
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(str(rec.get("startTime", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


_DOW = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6}


def recurrence_to_cron(rec: dict) -> tuple[str | None, list[str]]:
    """A ScheduleTrigger recurrence as one cron expression, with notes about anything approximated. None when it cannot be expressed.

    Follows the examples in Microsoft's trigger reference (tests/data/adf-triggers.md): hours or minutes that are not listed come from the start time, except that
    minutes without hours mean every hour. Cron here is Dagster's: `L` is the last day of the month, `d#n` the nth weekday, `Ld` the last weekday."""
    notes: list[str] = []
    freq, n = str(rec.get("frequency", "")).lower(), int(rec.get("interval") or 1)
    if freq == "hour" and n == 24:
        freq, n = "day", 1
    sch, st = rec.get("schedule") or {}, _start(rec)
    mins = sch.get("minutes") or ([st.minute] if st else [0])
    if sch.get("hours"):
        h = ",".join(str(x) for x in sorted(sch["hours"]))
    elif sch.get("minutes") and freq in ("day", "week", "month"):
        h = "*"                                           # minutes without hours: every hour at those minutes
    else:
        h = str(st.hour if st else 0)
    m = ",".join(str(x) for x in sorted(mins))
    if freq == "minute":
        if 60 % n:
            return None, [f"every {n} minutes does not divide the hour"]
        return ("* * * * *" if n == 1 else f"*/{n} * * * *"), notes
    if freq == "hour":
        if 24 % n:
            return None, [f"every {n} hours does not divide the day"]
        notes.append("hour cadence is aligned to midnight, ADF aligns it to the start time" if n > 1 else "")
        return (f"{m} * * * *" if n == 1 else f"{m} */{n} * * *"), [x for x in notes if x]
    if freq == "day":
        if n > 1:
            notes.append(f"every {n} days becomes day-of-month */{n}, which restarts each month")
        return (f"{m} {h} * * *" if n == 1 else f"{m} {h} */{n} * *"), notes
    if freq == "week":
        if n > 1:
            return None, [f"every {n} weeks cannot be expressed as cron"]
        days = sch.get("weekDays") or ([(st.weekday() + 1) % 7] if st else [0])
        d = ",".join(str(_DOW.get(str(x).lower(), x)) for x in days)
        return f"{m} {h} * * {d}", notes
    if freq == "month":
        occ = sch.get("monthlyOccurrences")
        if occ:
            parts = []
            for o in occ:
                day, k = _DOW.get(str(o.get("day", "")).lower()), o.get("occurrence")
                if day is None or not isinstance(k, int):
                    return None, [f"monthly occurrence {o!r} is not understood"]
                if k >= 1:
                    parts.append(f"{day}#{k}")
                elif k == -1:
                    parts.append(f"L{day}")
                else:
                    return None, [f"occurrence {k} (counted from the end of the month, other than the last) cannot be expressed as cron"]
            if n > 1:
                notes.append(f"every {n} months becomes month */{n}, aligned to January")
            return (f"{m} {h} * * {','.join(parts)}" if n == 1 else f"{m} {h} * */{n} {','.join(parts)}"), notes
        days = sch.get("monthDays") or ([st.day] if st else [1])
        bad = [x for x in days if int(x) < -1 or int(x) == 0]
        if bad:
            return None, [f"monthDays {bad} (counted from the end of the month, other than the last day) cannot be expressed as cron"]
        d = ",".join("L" if int(x) == -1 else str(x) for x in sorted(days, key=lambda v: (int(v) < 0, int(v))))
        if n > 1:
            notes.append(f"every {n} months becomes month */{n}, aligned to January")
        return (f"{m} {h} {d} * *" if n == 1 else f"{m} {h} {d} */{n} *"), notes
    return None, [f"frequency {rec.get('frequency')!r} is not supported"]


def window_cron(props: dict) -> tuple[str | None, list[str]]:
    """The partition cadence of a tumbling window trigger. Windows are anchored to startTime (the first one is startTime to startTime + interval), so the cron is
    built from its minute and hour, not from midnight. ADF's frequencies here are Minute, Hour and Month; windows that drift against the calendar (every 5 hours,
    every 7 minutes) cannot be a cron schedule and are refused. startTime's seconds are dropped."""
    freq, n = str(props.get("frequency", "")).lower(), int(props.get("interval") or 1)
    st = _start({"startTime": props.get("startTime")}) or datetime(2024, 1, 1, tzinfo=timezone.utc)
    notes = ["the seconds of startTime are ignored (cron has minute resolution)"] if st.second or st.microsecond else []
    if freq == "hour" and n % 24 == 0:
        freq, n = "day", n // 24
    if freq == "minute":
        if n < 1 or 60 % n:
            return None, [f"a tumbling window of {n} minutes drifts against the hour and cannot be a cron schedule"]
        return ("* * * * *" if n == 1 else f"{st.minute % n}-59/{n} * * * *"), notes
    if freq == "hour":
        if 24 % n:
            return None, [f"a tumbling window of {n} hours drifts against the calendar day and cannot be a cron schedule"]
        hours = ",".join(str(h) for h in sorted({(st.hour % n) + k * n for k in range(24 // n)}))
        return (f"{st.minute} * * * *" if n == 1 else f"{st.minute} {hours} * * *"), notes
    if freq == "day":
        if n > 1:
            return None, [f"a tumbling window of {n} days drifts against the calendar month and cannot be a cron schedule"]
        return f"{st.minute} {st.hour} * * *", notes
    if freq == "month":
        if st.day > 28:
            return None, [f"a monthly window starting on day {st.day} has no such day in every month"]
        months = ",".join(str(m) for m in sorted({((st.month - 1) % n) + 1 + k * n for k in range(12 // n + 1) if ((st.month - 1) % n) + 1 + k * n <= 12}))
        return f"{st.minute} {st.hour} {st.day} {'*' if n == 1 else months} *", notes
    return None, [f"a tumbling window of {n} {props.get('frequency')} is not translated"]


# ----------------------------------------------------------------------------------------------------------------------------------------------
# pipeline code generation
# ----------------------------------------------------------------------------------------------------------------------------------------------

class _Stub(Exception):
    pass


class PipeGen:
    def __init__(self, f: Factory, p: Pipeline):
        self.f, self.p = f, p
        self.slug = slug(p.name)
        self.defs: list[str] = []
        self.notes: list[str] = []
        self.stubs: list[tuple[str, str, str]] = []      # (type, name, reason)
        self.total = 0
        self.used_ls: set[str] = set()
        self.web_auth: list[str] = []
        self.used_dbx: set[str] = set()
        self.used_flows: set[str] = set()
        self.flow_components: set[str] = set()
        self.uses_identity = False
        self.uses_fabric = False
        self.uses_pandas = self.uses_parquet = self.uses_excel = False
        self.uses_mail = False
        self.used_fn: dict[str, str] = {}      # AzureFunction linked service -> its Key Vault secret name
        self.used_fs: dict[str, str] = {}      # linked service -> fsspec protocol
        self.hints: dict[str, str] = {}        # activity name -> why a component-backed activity stays a stub
        self.counter = 0

    # -- helpers ------------------------------------------------------------------------------------------------------------------------------
    def _n(self, kind: str, name: str) -> str:
        self.counter += 1
        return f"_{self.slug}_{kind}{self.counter}_{slug(name)}"

    def _stub(self, a: Activity, reason: str) -> list[str]:
        hint = STUB_HINT.get(a.type)
        self.stubs.append((a.type, a.name, reason + (f" ({hint})" if hint and hint not in reason else "")))
        msg = f"{a.type} activity '{a.name}': {reason}" + (f" ({hint})" if hint else "")
        return [f"adf_caveat({msg!r})"]

    def _tr(self, v, ds: str = "") -> str:
        return translate(v, ds)

    def _conn(self, ls_name: str, a: Activity) -> str:
        if not ls_name:
            raise _Stub("the activity names no linked service")
        ls = self.f.linked_services.get(ls_name)
        m = re.fullmatch(r"\[parameters\('([^']+)'\)\]", ls_name)
        if ls is None and m:
            # a template's linked service is a deploy-time parameter; a SQL activity says what kind it is, so name the connection after the parameter
            self.used_ls.add(m.group(1))
            return ls_env(m.group(1))
        if ls is None:
            self.used_ls.add(ls_name)
            self.notes.append(f"linked service {ls_name!r} is not in the export; the activity is SQL, so the connection is read from {ls_env(ls_name)}")
            return ls_env(ls_name)
        if ls.type not in SQL_LS:
            raise _Stub(f"linked service {ls_name!r} is {ls.type}, only SQL linked services are connected")
        self.used_ls.add(ls_name)
        return ls_env(ls_name)

    def _scope(self, acts_json, label: str) -> str:
        """Generate a scope function for a list of child activities; returns its name."""
        acts = parse_activities(acts_json)
        return self._scope_acts(acts, label)

    def _order(self, acts: list[Activity]) -> list[Activity]:
        names = {a.name for a in acts}
        placed, out, rest = set(), [], list(acts)
        while rest:
            progressed = False
            for a in list(rest):
                if all(d in placed or d not in names for d, _ in a.depends_on):
                    out.append(a)
                    placed.add(a.name)
                    rest.remove(a)
                    progressed = True
            if not progressed:
                self.notes.append(f"activities {', '.join(a.name for a in rest)} depend on each other in a cycle; kept in file order")
                out += rest
                break
        return out

    def _scope_acts(self, acts: list[Activity], label: str) -> str:
        fname = self._n("scope", label)
        entries = []
        local = {a.name for a in acts}
        for a in self._order(acts):
            fn = self._activity(a)
            pol = {k: a.policy[k] for k in ("retry", "retryIntervalInSeconds") if isinstance(a.policy.get(k), int)}
            entries.append(f"        ({a.name!r}, {[(d, c) for d, c in a.depends_on if d in local]!r}, {fn}, {pol!r}),")
        body = "\n".join(entries) if entries else ""
        self.defs.append(f"def {fname}(ctx, item):\n    run_activities(ctx, item, [\n{body}\n    ])\n")
        return fname

    # -- activities ---------------------------------------------------------------------------------------------------------------------------
    def _activity(self, a: Activity) -> str:
        self.total += 1
        fname = self._n("act", a.name)
        try:
            lines = self._body(a)
        except Untranslatable as e:
            lines = self._stub(a, f"expression not translated: {e}")
        except _Stub as e:
            lines = self._stub(a, str(e))
        body = "\n".join("    " + ln for ln in lines)
        self.defs.append(f"def {fname}(ctx, item):\n{body}\n")
        return fname

    def _body(self, a: Activity) -> list[str]:
        t, tp = a.type, a.props
        if a.name in self.hints:
            raise _Stub(self.hints[a.name])
        if not isinstance(tp, dict):
            raise _Stub("its properties are a deploy-time value that could not be resolved")
        if t == "SetVariable":
            if tp.get("setSystemVariable"):
                if tp.get("variableName") != "pipelineReturnValue" or not isinstance(tp.get("value"), list):
                    raise _Stub(f"system variable {tp.get('variableName')!r} is not supported")
                parts = []
                for kv in tp["value"]:
                    v = (kv or {}).get("value")
                    if isinstance(v, dict) and "content" in v:
                        content = v["content"]
                        code = self._tr({"type": "Expression", "value": content} if v.get("type") == "Expression" else content)
                    else:
                        code = self._tr(v)
                    parts.append(f"{kv['key']!r}: {code}")
                return ["ctx.set_return({" + ", ".join(parts) + "})"]
            return [f"ctx.set_var({tp['variableName']!r}, {self._tr(tp.get('value'))})"]
        if t == "AppendVariable":
            return [f"ctx.append_var({tp['variableName']!r}, {self._tr(tp.get('value'))})"]
        if t == "Wait":
            return [f"return adf_wait(ctx, {self._tr(tp.get('waitTimeInSeconds', 0))})"]
        if t == "Fail":
            return [f"adf_fail({self._tr(tp.get('message', ''))}, {self._tr(tp.get('errorCode', ''))})"]
        if t == "Filter":
            return [f"return adf_filter({self._tr(tp['items'])}, lambda item: {self._tr(tp['condition'])})"]
        if t == "IfCondition":
            cond = self._tr(tp["expression"])
            yes = self._scope(tp.get("ifTrueActivities") or [], a.name + "_true")
            no = self._scope(tp.get("ifFalseActivities") or [], a.name + "_false")
            return [f"result = bool({cond})", f"({yes} if result else {no})(ctx, item)", "return Out({'ifConditionResult': result})"]
        if t == "Switch":
            lines = [f"v = fx.string({self._tr(tp['on'])})"]
            for i, c in enumerate(tp.get("cases") or []):
                fn = self._scope(c.get("activities") or [], f"{a.name}_case{i}")
                lines += [f"{'if' if i == 0 else 'elif'} v == {str(c.get('value'))!r}:", f"    {fn}(ctx, item)"]
            dfn = self._scope(tp.get("defaultActivities") or [], a.name + "_default")
            lines += ["else:" if tp.get("cases") else "if True:", f"    {dfn}(ctx, item)", "return Out({'matched': v})"]
            return lines
        if t == "ForEach":
            fn = self._scope(tp.get("activities") or [], a.name + "_body")
            seq = bool(tp.get("isSequential", False))
            return [f"return adf_foreach(ctx, {self._tr(tp['items'])}, {fn}, {seq}, {tp.get('batchCount')!r})"]
        if t == "Until":
            fn = self._scope(tp.get("activities") or [], a.name + "_body")
            timeout = _dur_seconds(tp.get("timeout"), 7 * 86400)
            return [f"return adf_until(ctx, item, {fn}, lambda ctx, item: bool({self._tr(tp['expression'])}), {timeout})"]
        if t == "ExecutePipeline":
            ref = (tp.get("pipeline") or {}).get("referenceName", "")
            if ref not in self.f.pipelines:
                raise _Stub(f"pipeline {ref!r} is not in the export")
            params = self._tr(tp.get("parameters") or {})
            if tp.get("waitOnCompletion", True):                       # ADF's default is to wait
                return [f"child = run_{slug(ref)}({params}, log=ctx.log, parent=(ctx.pipeline, ctx.run_id))",
                        "return Out({'pipelineRunId': child.run_id, 'pipelineName': " + repr(ref) + ", 'pipelineReturnValue': child.return_value})"]
            self.notes.append(f"{a.name}: ADF starts '{ref}' without waiting; this runs it to completion first and ignores its failure (waitOnCompletion is false)")
            return ["try:", f"    child = run_{slug(ref)}({params}, log=ctx.log, parent=(ctx.pipeline, ctx.run_id))", "    rid = child.run_id", "except Exception as e:",
                    "    ctx.log(f'child pipeline failed (not waited for in ADF): {e}')", "    rid = ''",
                    "return Out({'pipelineRunId': rid, 'pipelineName': " + repr(ref) + "})"]
        if t == "WebActivity":
            auth = ""
            if tp.get("authentication"):
                at = (tp["authentication"] or {}).get("type", "?")
                if at == "MSI" and (tp["authentication"] or {}).get("resource"):
                    auth = f", auth={{'type': 'MSI', 'resource': {self._tr(tp['authentication']['resource'])}}}"
                    self.uses_identity = True
                    self.notes.append(f"{a.name}: managed-identity authentication becomes azure-identity's DefaultAzureCredential (the managed identity on Azure, else AZURE_CLIENT_ID, AZURE_TENANT_ID and AZURE_CLIENT_SECRET, else the Azure CLI login)")
                else:
                    self.web_auth.append(a.name)
                    raise _Stub(f"authentication type {at} is not carried over (credentials are never copied); add them in generated code or pass headers")
            if not isinstance(tp.get("url"), (str, dict)):
                raise _Stub("no url")
            extra = ""
            if tp.get("httpRequestTimeout"):
                secs = _dur_seconds(tp["httpRequestTimeout"], 0)
                if secs:
                    extra += f", timeout_s={min(secs, 600)}"
            if tp.get("turnOffAsync"):
                extra += ", async_off=True"
            return [f"return adf_web(ctx, {a.name!r}, {self._tr(tp['url'])}, {str(tp.get('method', 'GET'))!r}, "
                    f"{self._tr(tp.get('headers') or {})}, {self._tr(tp.get('body')) if tp.get('body') is not None else 'None'}{auth}{extra})"]
        if t == "TridentNotebook":
            nid = tp.get("notebookId")
            if not nid:
                raise _Stub("the activity names no notebookId")
            ws = tp.get("workspaceId")
            params = tp.get("parameters") or {}
            items = []
            for k, v in params.items():
                val, typ = (v.get("value"), str(v.get("type", "string")).lower()) if isinstance(v, dict) and "value" in v else (v, "string")
                code = self._tr(val)
                fn = _ADF_TYPES.get(typ)
                items.append(f"{k!r}: fx.{fn}({code})" if fn else f"{k!r}: {code}")
            self.uses_fabric = True
            self.notes.append(f"{a.name}: the notebook runs through the registry's FabricWorkspaceResource and the run's exit value is not available from the job API, so an expression reading output.result.exitValue fails on a real run")
            timeout = _dur_seconds((a.policy or {}).get("timeout"), 1800)
            return [f"return adf_fabric_run(ctx, {a.name!r}, 'Notebook', {self._tr(nid)}, {self._tr(ws) if ws else "ctx.sys('datafactory')"}, {{{', '.join(items)}}}, {min(timeout, 86400)})"]
        if t == "Office365Outlook":
            inp = tp.get("inputs") if isinstance(tp.get("inputs"), dict) else {}
            if str(inp.get("path", "")) != "/v2/Mail" or not isinstance(inp.get("body"), dict):
                raise _Stub(f"only a send-mail call (path /v2/Mail) is translated, not {inp.get('path')!r}")
            b = inp["body"]
            if not b.get("To"):
                raise _Stub("the mail has no recipient")
            self.uses_mail = True
            self.notes.append(f"{a.name}: the mail goes through the SMTP server named by SMTP_HOST (ADF used an Office 365 connection, which is not copied)")
            return [f"return adf_mail(ctx, {a.name!r}, {self._tr(b['To'])}, {self._tr(b.get('Subject', ''))}, {self._tr(b.get('Body', ''))}, "
                    f"{self._tr(b['From']) if b.get('From') else repr('')}, {self._tr(b['Cc']) if b.get('Cc') else repr('')}, {self._tr(b['Bcc']) if b.get('Bcc') else repr('')})"]
        if t == "AzureFunctionActivity":
            ls = self.f.linked_services.get(a.linked_service)
            if ls is None or ls.type != "AzureFunction":
                raise _Stub(f"linked service {a.linked_service!r} is not an AzureFunction linked service in the export")
            url = ls.props.get("functionAppUrl")
            urlenv = ""
            if not isinstance(url, str):
                raise _Stub("the linked service names no function app url")
            if not url or url.startswith("["):
                urlenv = "ADF_FUNCTIONAPP_" + re.sub(r"\W", "_", ls.name).upper()      # the template left it to deploy time
                url = ""
            if not tp.get("functionName"):
                raise _Stub("the activity names no function")
            if str(tp.get("method", "POST")).upper() not in ("GET", "POST", "PUT"):
                raise _Stub(f"method {tp.get('method')!r}: the Azure Function activity supports GET, POST and PUT")
            if str(tp.get("method", "POST")).upper() in ("POST", "PUT") and tp.get("body") is None:
                self.notes.append(f"{a.name}: the activity page requires a body for POST and PUT; none is set, so none is sent")
            keyenv = "ADF_FUNCTIONKEY_" + re.sub(r"\W", "_", ls.name).upper()
            kv = ls.props.get("functionKey")
            self.used_fn[ls.name] = (kv or {}).get("secretName", "") if isinstance(kv, dict) else ""
            return [f"return adf_function(ctx, {a.name!r}, {url!r}, {self._tr(tp['functionName'])}, {str(tp.get('method', 'POST'))!r}, "
                    f"{self._tr(tp.get('headers') or {})}, {self._tr(tp.get('body')) if tp.get('body') is not None else 'None'}, {keyenv!r}, {urlenv!r})"]
        if t == "Lookup":
            return self._lookup(a, tp)
        if t in ("Delete", "GetMetadata", "Validation"):
            return self._storage(a, tp)
        if t == "Script":
            env = self._conn(a.linked_service, a)
            blocks = []
            for sc in tp.get("scripts") or []:
                params = {}
                for prm in sc.get("parameters") or []:
                    if str(prm.get("direction", "Input")).lower() != "input":
                        raise _Stub(f"script parameter {prm.get('name')!r} has direction {prm.get('direction')}: output parameters are not supported")
                    nm = str(prm.get("name", "")).lstrip("@")
                    if not _pyid(nm):
                        raise _Stub(f"script parameter name {prm.get('name')!r} is not a plain identifier")
                    pv = prm.get("value")
                    params[nm] = self._tr({"type": "Expression", "value": pv["value"]} if isinstance(pv, dict) and pv.get("type") == "Expression" else pv)
                blocks.append("{'type': " + repr(str(sc.get("type", "Query"))) + ", 'text': " + self._tr(sc["text"]) + ", 'params': {" + ", ".join(f"{k!r}: {v}" for k, v in params.items()) + "}}")
            if tp.get("scriptBlockExecutionTimeout"):
                self.notes.append(f"{a.name}: scriptBlockExecutionTimeout is not enforced")
            if tp.get("logSettings"):
                self.notes.append(f"{a.name}: script logging (logSettings) is not reproduced")
            self.notes.append(f"{a.name}: only the first result set of each script block is read")
            return [f"return adf_script(ctx, {a.name!r}, {env!r}, [{', '.join(blocks)}])"]
        if t == "SqlServerStoredProcedure":
            env = self._conn(a.linked_service, a)
            name = tp.get("storedProcedureName")
            if not isinstance(name, str) or not re.fullmatch(r"[\w.\[\]]+", name):
                raise _Stub("the stored procedure name is not a plain literal")
            ps = tp.get("storedProcedureParameters") or {}
            if not all(_pyid(k) for k in ps):
                raise _Stub("a stored procedure parameter name is not a plain identifier")
            call = f"EXEC {name} " + ", ".join(f"@{k} = :{k}" for k in ps)
            vals = "{" + ", ".join(f"{k!r}: {self._tr((v or {}).get('value'))}" for k, v in ps.items()) + "}"
            return [f"return adf_sql(ctx, {a.name!r}, {env!r}, {call.strip()!r}, {vals}, 'none')"]
        if t == "DatabricksNotebook":
            return self._databricks(a, tp)
        if t == "ExecuteDataFlow":
            return self._dataflow(a, tp)
        if t == "Copy":
            try:
                return self._copy(a, tp)
            except _Stub as e:
                pair = f"{(tp.get('source') or {}).get('type', '?') if isinstance(tp.get('source'), dict) else '?'} -> {(tp.get('sink') or {}).get('type', '?') if isinstance(tp.get('sink'), dict) else '?'}"
                raise _Stub(str(e) if str(e).startswith(pair) else f"{pair}: {e}")
        # everything else
        raise _Stub(self._stub_reason(a))


    # -- Mapping Data Flow ----------------------------------------------------------------------------------------------------------------
    def _flow_side(self, a: Activity, entry: dict, node, kind: str, idx: int, lines: list[str]) -> str:
        """Python source for the spec of one source or sink of a data flow: a SQL table or query, or a delimited text, Parquet or JSON file."""
        from . import dflow
        ref = (entry or {}).get("dataset")
        if not ref:
            return self._flow_inline(a, entry or {}, node, kind)
        dsvar = f"ds_{idx}"
        ds = self._ds(ref, dsvar, lines)
        ls = self.f.linked_services.get(ds.linked_service)
        sqlish = (ls is not None and ls.type in SQL_LS) or (ls is None and ds.type.endswith("Table"))
        if sqlish:
            env = self._conn(ds.linked_service, a)
            if kind == "source":
                fmt = dflow.dfs_string(node.opts.get("format", "'table'")) or "table"
                if fmt == "query":
                    q = dflow.dfs_string(node.opts.get("query", ""))
                    if q is None:
                        raise _Stub(f"source {node.name}: the query is not a plain literal")
                    code = repr(q)
                elif fmt == "table":
                    tb = self._tbl(ds, dsvar)
                    if not tb:
                        raise _Stub(f"source {node.name}: the dataset names no table")
                    code = f"fx.concat('SELECT * FROM ', {tb})"
                else:
                    raise _Stub(f"source {node.name}: SQL source format {fmt!r} is not translated (table and query are)")
                iso = dflow.dfs_string(node.opts.get("isolationLevel", "''")) or ""
                return "{'kind': 'sql', 'env': " + repr(env) + ", 'query': " + code + (f", 'isolation': {iso.replace('_', '').lower()!r}" if iso else "") + "}"
            tb = self._tbl(ds, dsvar)
            if not tb:
                raise _Stub(f"sink {node.name}: the dataset names no table")
            return self._flow_sql_sink(env, tb, node)
        if ds.type not in ("DelimitedText", "Parquet", "Json"):
            raise _Stub(f"{kind} {node.name}: dataset type {ds.type} is not translated (SQL tables, delimited text, Parquet and JSON are)")
        if ds.type == "DelimitedText" and not ds.props.get("firstRowAsHeader") and kind == "source":
            raise _Stub(f"source {node.name}: delimited text without a header row is not translated")
        url, env, fname = self._location(ds, dsvar)
        self.uses_pandas = True
        self.uses_parquet = self.uses_parquet or ds.type == "Parquet"
        if kind == "source":
            return "{'kind': 'file', 'url': " + url + f", 'env': {env!r}, 'fmt': {ds.type!r}, 'opts': {self._opts(ds)}, 'file': {self._tr(fname) if fname else repr('')}" + "}"
        names = dflow.string_list(node.opts.get("partitionFileNames", "[]")) or []
        if len(names) != 1 and not fname:
            self.notes.append(f"{a.name}: sink {node.name} names no output file; it writes one Spark-style part file (part-00000-<guid>-c000.<ext>) into the folder")
        return ("{'kind': 'file', 'url': " + url + f", 'env': {env!r}, 'fmt': {ds.type!r}, 'opts': {self._opts(ds)}, 'file': "
                + (repr(names[0]) if len(names) == 1 else (self._tr(fname) if fname else "''")) + f", 'truncate': {str(node.opts.get('truncate', 'false')).lower() == 'true'}" + "}")

    def _flow_sql_sink(self, env: str, table_code: str, node, pre: list | None = None) -> str:
        """Spec of a SQL sink: truncate, the row policies it allows (alterRow marks each row; default is insert) and the key columns for updates, deletes and upserts."""
        from . import dflow
        flag = lambda k, d="false": str(node.opts.get(k, d)).strip().lower() == "true"
        keys = dflow.string_list(node.opts.get("keys", "[]")) or []
        allow = {"insert": flag("insertable", "true"), "update": flag("updateable"), "delete": flag("deletable"), "upsert": flag("upsertable")}
        if any(allow[k] for k in ("update", "delete", "upsert")) and not keys:
            raise _Stub(f"sink {node.name}: updates, deletes and upserts need literal key columns")
        return ("{'kind': 'sql', 'env': " + repr(env) + ", 'table': " + table_code + f", 'truncate': {flag('truncate')}, 'pre_sql': {(pre or [])!r}, 'allow': {allow!r}, 'keys': {keys!r}" + "}")

    def _flow_inline(self, a: Activity, entry: dict, node, kind: str) -> str:
        """An inline dataset: the format, store and location are written in the script itself and the activity's entry names only the linked service."""
        from . import dflow
        lsref = (entry.get("linkedService") or {}).get("referenceName")
        if not lsref:
            raise _Stub(f"{kind} {node.name}: a flowlet or an entry with no dataset and no linked service is not translated")
        fmt = dflow.dfs_string(node.opts.get("format", "''")) or ""
        store = dflow.dfs_string(node.opts.get("store", "''")) or ""
        true = lambda key, default="false": str(node.opts.get(key, default)).strip().lower() == "true"
        if store == "sqlserver":
            env = self._conn(lsref, a)
            tbl = dflow.dfs_string(node.opts.get("tableName", "''")) or ""
            sch = dflow.dfs_string(node.opts.get("schemaName", "''")) or ""
            table = f"{sch}.{tbl}" if sch and tbl and "." not in tbl else tbl
            if kind == "source":
                if fmt == "query":
                    q = dflow.dfs_string(node.opts.get("query", ""))
                    if q is None:
                        raise _Stub(f"source {node.name}: the query is not a plain literal")
                    code = repr(q)
                elif fmt == "table" and table:
                    code = repr(f"SELECT * FROM {table}")
                elif fmt == "procedure":
                    pn = dflow.dfs_string(node.opts.get("procedureName", "''")) or ""
                    if "inputs" in node.opts or not re.fullmatch(r"[\w.\[\]]+", pn):
                        raise _Stub(f"source {node.name}: a stored procedure with input parameters (or an odd name) is not translated")
                    code = repr(f"EXEC {sch + '.' if sch else ''}{pn}")
                else:
                    raise _Stub(f"source {node.name}: SQL source format {fmt!r} is not translated (table, query and procedure without inputs are)")
                iso = (dflow.dfs_string(node.opts.get("isolationLevel", "''")) or "").replace("_", "").lower()
                return "{'kind': 'sql', 'env': " + repr(env) + ", 'query': " + code + (f", 'isolation': {iso!r}" if iso else "") + "}"
            if fmt != "table" or not table:
                raise _Stub(f"sink {node.name}: SQL sink format {fmt!r} is not translated (a table is)")
            pre = dflow.string_list(node.opts.get("preSQLs", "[]"))
            if pre is None:
                raise _Stub(f"sink {node.name}: preSQLs is not a list of plain strings")
            return self._flow_sql_sink(env, repr(table), node, pre)
        if store:
            raise _Stub(f"{kind} {node.name}: the inline store {store!r} is not translated (sqlserver is)")
        fmt_ds = {"delimited": "DelimitedText", "parquet": "Parquet", "json": "Json", "excel": "Excel"}.get(fmt)
        if fmt_ds == "Excel" and kind == "sink":
            raise _Stub(f"sink {node.name}: an Excel sink is not translated")
        if fmt_ds is None:
            raise _Stub(f"{kind} {node.name}: inline format {fmt!r} is not translated (sqlserver, delimited, parquet and json are)")
        ls = self.f.linked_services.get(lsref)
        proto = FS_LS.get(ls.type) if ls is not None else None
        if proto is None:
            raise _Stub(f"{kind} {node.name}: linked service {lsref!r} is {ls.type if ls else 'not in the export'}; file stores reached are blob, ADLS Gen2, S3 and file server")
        container = dflow.dfs_string(node.opts.get("container", "''")) or dflow.dfs_string(node.opts.get("fileSystem", "''")) or dflow.dfs_string(node.opts.get("bucketName", "''")) or ""
        folder = dflow.dfs_string(node.opts.get("folderPath", "''")) or ""
        fname = dflow.dfs_string(node.opts.get("fileName", "''")) or ""
        if proto == "file":
            url, env = folder, ""
        else:
            url, env = f"{proto}://{container}/{folder}", "ADF_FS_" + re.sub(r"\W", "_", lsref).upper()
            self.used_fs[lsref] = proto
        self.uses_pandas = True
        self.uses_parquet = self.uses_parquet or fmt_ds == "Parquet"
        o: dict = {}
        if fmt_ds == "DelimitedText":
            o = {"delimiter": dflow.dfs_string(node.opts.get("columnDelimiter", "','")) or ",", "quote": dflow.dfs_string(node.opts.get("quoteChar", "'\"'")) or "",
                 "header": true("firstRowAsHeader") if kind == "source" else true("columnNamesAsHeader"), "encoding": "utf-8", "null_value": dflow.dfs_string(node.opts.get("nullValue", "''")) or "",
                 "row_delimiter": "\n"}
            if kind == "source" and not o["header"] and not dflow.source_schema(node):
                raise _Stub(f"source {node.name}: delimited text without a header row needs the projection (output(...)) to name its columns")
            if kind == "source" and not o["header"]:
                self.notes.append(f"{a.name}: source {node.name} has no header row; its columns are named by position from the script's projection")
            if len(o["delimiter"]) != 1:
                raise _Stub(f"{kind} {node.name}: a multi-character column delimiter is not translated")
        if fmt_ds == "Excel":
            sheet = dflow.dfs_string(node.opts.get("sheetName", "''")) or None
            o = {"sheet": sheet, "header": true("firstRowAsHeader"), "null_value": ""}
            if dflow.dfs_string(node.opts.get("range", "''")):
                raise _Stub(f"source {node.name}: an Excel range is not translated")
            self.uses_excel = True
        if fmt_ds == "Json" and kind == "sink":
            o["array"] = dflow.dfs_string(node.opts.get("documentForm", "'documentPerLine'")) == "arrayOfDocuments"
        if kind == "source":
            return "{'kind': 'file', 'url': " + repr(url) + f", 'env': {env!r}, 'fmt': {fmt_ds!r}, 'opts': {o!r}, 'file': {fname!r}" + "}"
        names = dflow.string_list(node.opts.get("partitionFileNames", "[]")) or []
        if len(names) != 1 and not fname:
            self.notes.append(f"{a.name}: sink {node.name} names no output file; it writes one Spark-style part file (part-00000-<guid>-c000.<ext>) into the folder")
        return ("{'kind': 'file', 'url': " + repr(url) + f", 'env': {env!r}, 'fmt': {fmt_ds!r}, 'opts': {o!r}, 'file': {(names[0] if len(names) == 1 else fname)!r}, 'truncate': {true('truncate')}" + "}")

    def _flow_sink_is_sql(self, d: dict, node) -> bool:
        from . import dflow
        entry = next((x for x in d.get("sinks") or [] if x.get("name") == node.name), {})
        ref = (entry.get("dataset") or {}).get("referenceName")
        if not ref:
            return dflow.dfs_string(node.opts.get("store", "''")) == "sqlserver"
        ds = self.f.datasets.get(ref)
        ls = self.f.linked_services.get(ds.linked_service) if ds else None
        return ds is not None and ((ls is not None and ls.type in SQL_LS) or ds.type.endswith("Table"))

    def _dataflow(self, a: Activity, tp: dict) -> list[str]:
        from . import dflow
        ref = tp.get("dataflow") or {}
        fname = ref.get("referenceName")
        d = self.f.data_flow_defs.get(fname)
        if d is None:
            raise _Stub(f"data flow {fname!r} is not in the export")
        script = d.get("script") or "\n".join(d.get("scriptLines") or [])
        if not script.strip():
            raise _Stub(f"data flow {fname!r} has no script")
        try:
            declared, nodes = dflow.parse_script(script)
            why = dflow.check_flow(nodes)
        except dflow.Unsupported as e:
            why = [str(e)]
        if why:
            more = f" (and {len(why) - 3} more)" if len(why) > 3 else ""
            raise _Stub(f"data flow {fname!r} is not translated: " + "; ".join(sorted(dict.fromkeys(why))[:3]) + more)
        if any(n.kind == "alterrow" for n in nodes):
            for n in nodes:
                if n.kind == "sink" and not self._flow_sink_is_sql(d, n):
                    raise _Stub(f"sink {n.name}: a flow with alterRow row policies needs SQL sinks (the policies insert, update, delete and upsert rows in a database)")
        lines: list[str] = []
        by_name = {s.get("name"): s for s in d.get("sources") or []}
        by_sink = {s.get("name"): s for s in d.get("sinks") or []}
        sources, sinks = [], []
        for i, n in enumerate(x for x in nodes if x.kind in ("source", "sink")):
            if n.kind == "source":
                sources.append(f"{n.name!r}: " + self._flow_side(a, by_name.get(n.name), n, "source", i, lines))
            else:
                sinks.append(f"{n.name!r}: " + self._flow_side(a, by_sink.get(n.name), n, "sink", i, lines))
        params = []
        for k, v in (ref.get("parameters") or {}).items():
            val = v.get("value") if isinstance(v, dict) and "value" in v else v
            if isinstance(val, str) and val.startswith("@"):
                params.append(f"{k!r}: {self._tr({'type': 'Expression', 'value': val})}")
            elif isinstance(val, str):
                try:
                    dflow.compile_expr(val)
                except dflow.Unsupported as e:
                    raise _Stub(f"data flow parameter {k!r}: {e}")
                params.append(f"{k!r}: df_param({val!r})")
            else:
                params.append(f"{k!r}: {val!r}")
        ignored = [k for k in ("compute", "staging", "runConcurrently", "traceLevel", "continueOnError", "integrationRuntime") if tp.get(k)]
        if ignored:
            self.notes.append(f"{a.name}: data flow setting(s) not applied: {', '.join(ignored)} (the flow runs in this process, row by row)")
        self.notes.append(f"{a.name}: data flow {fname!r} runs in this Python process over lists of rows, not on Spark: it is exact but not fast, and it reads and writes whole tables and files in memory. "
                          "Each derived column sees the incoming row, not the columns derived earlier in the same transformation. Output files are named by partitionFileNames.")
        self.used_flows.add(fname)
        return lines + [f"return adf_dataflow(ctx, {a.name!r}, {fname!r}, {script!r}, {{{', '.join(sources)}}}, {{{', '.join(sinks)}}}, {{{', '.join(params)}}})"]

    # -- Copy -----------------------------------------------------------------------------------------------------------------------------
    def _tbl(self, ds, dsvar: str):
        """Python source for a SQL dataset's table (tableName, or schema and table: each may be an expression), or None."""
        t = ds.props.get("tableName")
        if t:
            return self._tr(t, dsvar)
        sch, tab = ds.props.get("schema"), ds.props.get("table")
        if tab:
            return f"fx.concat({self._tr(sch, dsvar)}, '.', {self._tr(tab, dsvar)})" if sch else self._tr(tab, dsvar)
        return None

    def _ds(self, ref: dict, dsvar: str, lines: list[str]):
        ds = self.f.datasets.get((ref or {}).get("referenceName", ""))
        if ds is None:
            raise _Stub(f"dataset {(ref or {}).get('referenceName')!r} is not in the export")
        if (ref or {}).get("parameters"):
            lines.append(f"{dsvar} = " + self._tr(ref["parameters"]))
        elif ds.parameters:
            lines.append(f"{dsvar} = {{}}")
        return ds

    def _location(self, ds, dsvar: str, root_only: bool = False):
        """(url code, env var, file name code or None) for a file dataset's location."""
        loc = ds.props.get("location")
        if not isinstance(loc, dict):
            raise _Stub(f"dataset {ds.name!r} has no file location")
        keys = {"AzureBlobStorageLocation": ("container", "abfs"), "AzureBlobFSLocation": ("fileSystem", "abfs"), "AmazonS3Location": ("bucketName", "s3"),
                "FileServerLocation": (None, "file")}.get(loc.get("type"))
        if keys is None:
            raise _Stub(f"location type {loc.get('type')!r} is not translated")
        ls = self.f.linked_services.get(ds.linked_service)
        m = re.fullmatch(r"\[parameters\('([^']+)'\)\]", ds.linked_service or "")
        if ls is None and m:
            # the template leaves the linked service to deploy time; the dataset's location type says which store it is
            lsname, proto = m.group(1), keys[1]
        elif ls is None:
            if not ds.linked_service:
                raise _Stub(f"dataset {ds.name!r} names no linked service")
            lsname, proto = ds.linked_service, keys[1]
            self.notes.append(f"linked service {ds.linked_service!r} is not in the export; the dataset's location type says it is a {proto}:// store, so its credentials are read from the environment")
        else:
            lsname, proto = ls.name, FS_LS.get(ls.type)
            if proto is None:
                raise _Stub(f"linked service {ls.name!r} is {ls.type}, which Copy does not reach yet")
            if keys[1] != proto:
                raise _Stub(f"location type {loc.get('type')!r} on a {ls.type} linked service is not translated")
        top, _ = keys
        if top and not loc.get(top):
            raise _Stub(f"the dataset location names no {top}")
        folder = self._tr(loc["folderPath"], dsvar) if loc.get("folderPath") and not (root_only and proto != "file") else "''"
        if proto == "file":
            url = folder
            env = ""
        else:
            url = f"fx.concat({proto + '://'!r}, {self._tr(loc[top], dsvar)}, '/', {folder})"
            env = "ADF_FS_" + re.sub(r"\W", "_", lsname).upper()
            self.used_fs[lsname] = proto
        fname = self._tr(loc["fileName"], dsvar) if loc.get("fileName") else None
        return url, env, fname

    def _opts(self, ds, sink=None) -> str:
        """A dict literal of the read or write options a file dataset carries (delimiters, header, encoding, compression, sheet)."""
        pr = ds.props
        codec = pr.get("compressionCodec")
        codec = str(codec).lower() if codec else None
        if ds.type in ("DelimitedText", "Json"):
            if codec not in (None, "none", "gzip", "bzip2"):
                raise _Stub(f"compression {pr.get('compressionCodec')!r} is not translated (none, gzip and bzip2 are)")
        elif ds.type == "Parquet":
            if codec not in (None, "none", "snappy", "gzip"):
                raise _Stub(f"Parquet compression {pr.get('compressionCodec')!r} is not translated (snappy and gzip are)")
        o: dict = {}
        if ds.type == "DelimitedText":
            rd = pr.get("rowDelimiter")
            if rd not in (None, "\n", "\r\n", "\r"):
                raise _Stub(f"row delimiter {rd!r} is not translated")
            if pr.get("columnDelimiter", ",") == "":
                raise _Stub("an empty column delimiter is not supported by the Copy activity (nor here)")
            esc = pr.get("escapeChar", "\\")
            if esc not in ("\\", "") and esc != pr.get("quoteChar", '"'):
                self.notes.append(f"escapeChar {esc!r} is not applied: quotes inside values are doubled, as in RFC 4180")
            o = {"delimiter": pr.get("columnDelimiter", ","), "quote": pr.get("quoteChar", '"'), "header": bool(pr.get("firstRowAsHeader", False)),
                 "encoding": self._encoding(pr.get("encodingName", "UTF-8")), "compression": codec if codec != "none" else None,
                 "null_value": pr.get("nullValue", ""), "row_delimiter": rd or "\r\n"}      # Copy writes \r\n unless the dataset says otherwise
        elif ds.type == "Json":
            o = {"encoding": self._encoding(pr.get("encodingName", "UTF-8")), "compression": codec if codec != "none" else None}
            if sink is not None and str((sink.get("formatSettings") or {}).get("filePattern", "setOfObjects")).lower() == "arrayofobjects":
                o["array"] = True
        elif ds.type == "Parquet":
            o = {"compression": codec if codec not in (None, "none") else None}
        elif ds.type == "Excel":
            if pr.get("range"):
                raise _Stub("an Excel range is not translated")
            sheet = pr.get("sheetName", pr.get("sheetIndex"))
            if isinstance(sheet, (dict, list)):
                raise _Stub("the Excel sheet is an expression")
            o = {"sheet": sheet, "header": bool(pr.get("firstRowAsHeader", False)), "null_value": pr.get("nullValue", "")}
        return repr(o)

    def _encoding(self, name) -> str:
        """The Python codec for an ADF encodingName ('UTF-8 without BOM' is utf-8; the others resolve through the codec registry), or a stub."""
        import codecs
        n = str(name or "UTF-8").strip().lower().replace(" without bom", "")
        try:
            return codecs.lookup(n).name
        except LookupError:
            raise _Stub(f"encoding {name!r} is not known to Python")

    def _fmt_signature(self, ds) -> tuple:
        return (ds.type,) + tuple(ds.props.get(k, d) if k in _FMT_DEFAULTS else None for k, d in _FMT_DEFAULTS.items()) if ds.type == "DelimitedText" else (ds.type, ds.props.get("compressionCodec"))

    def _store_settings(self, side: dict, what: str) -> dict:
        """The store settings of a file source or sink. Everything the connector pages define is translated (see _store_kwargs); nothing is silently dropped."""
        return side.get("storeSettings") if isinstance(side.get("storeSettings"), dict) else {}

    def _store_kwargs(self, st: dict, *, binary_copy: bool = False, partitions: bool = False) -> tuple[str, bool]:
        """Keyword arguments for a file read from its store settings, and whether the source location must be the store root (a prefix is a key prefix from the
        container or bucket). `filters` carries fileListPath, prefix and modifiedDatetimeStart / End; deleteFilesAfterCompletion is for binary copies only; with
        enablePartitionDiscovery, key=value folders become columns (partitionRootPath names where they start)."""
        f = {}
        for key, name in (("modifiedDatetimeStart", "modified_start"), ("modifiedDatetimeEnd", "modified_end"), ("fileListPath", "file_list"), ("prefix", "prefix")):
            if st.get(key):
                f[name] = self._tr(st[key])
        kw = ""
        if f:
            kw += ", filters={" + ", ".join(f"{k!r}: {v}" for k, v in f.items()) + "}"
        if st.get("deleteFilesAfterCompletion"):
            if not binary_copy:
                raise _Stub("deleteFilesAfterCompletion is only valid for a binary file copy (the connector page says so)")
            kw += ", delete_source=True"
            self.notes.append("deleteFilesAfterCompletion removes each source file after it was copied; a failure part-way leaves the rest in place, as documented")
        if st.get("enablePartitionDiscovery"):
            if not partitions:
                self.notes.append("enablePartitionDiscovery adds nothing to a byte-for-byte copy")
            else:
                kw += f", partition_root={self._tr(st['partitionRootPath']) if st.get('partitionRootPath') else repr('')}"
                self.notes.append("enablePartitionDiscovery: key=value folders in the file path become extra string columns")
        return kw, bool(st.get("prefix"))


    def _sql_sink_checks(self, snk: dict) -> str:
        """Check a SQL sink and return the keyword arguments it adds to the copy call: upsert keys (writeBehavior upsert needs upsertSettings.keys) and auto_create
        (tableOption autoCreate). A stored-procedure sink is a stub: it needs a table-valued parameter."""
        if snk.get("sqlWriterStoredProcedureName"):
            raise _Stub("a stored-procedure sink (sqlWriterStoredProcedureName with a table type) is not translated: it needs a table-valued parameter")
        out = ""
        wb = str(snk.get("writeBehavior", "insert")).lower()
        if wb == "upsert":
            keys = (snk.get("upsertSettings") or {}).get("keys")
            if not (isinstance(keys, list) and keys and all(isinstance(k, str) and not is_dynamic(k) for k in keys)):
                raise _Stub("an upsert needs literal key columns in upsertSettings.keys")
            out += f", keys={keys!r}"
            if (snk.get("upsertSettings") or {}).get("useTempDB") is False:
                self.notes.append("upsert through a physical interim table (useTempDB false) is not used: rows are updated by key and the rest inserted")
        elif wb != "insert":
            raise _Stub(f"writeBehavior {snk.get('writeBehavior')!r} is not translated (insert and upsert are)")
        if str(snk.get("tableOption", "")).lower() == "autocreate":
            out += ", auto_create=True"
            self.notes.append("an auto-created table takes its column types from the values copied (text for delimited files), not from the source schema")
        if snk.get("writeBatchSize"):
            out += f", batch={int(snk['writeBatchSize'])}" if isinstance(snk["writeBatchSize"], int) else ""
        return out

    def _sql_source(self, src: dict, ds, dsvar: str, family_mssql: bool):
        """(query code, params code, isolation kwarg) for a SQL source: sqlReaderQuery or query, a stored procedure with its parameters (SQL Server family only), or
        the dataset's table."""
        iso = ""
        if src.get("isolationLevel"):
            iso = f", isolation={str(src['isolationLevel'])!r}"
        if src.get("partitionOption") or src.get("partitionOptions"):
            self.notes.append("partitionOption is not applied (it only parallelises the read; the rows are the same)")
        if source_query(src):
            return self._tr(source_query(src), dsvar), "None", iso
        if src.get("sqlReaderStoredProcedureName"):
            name = src["sqlReaderStoredProcedureName"]
            if not family_mssql:
                raise _Stub("a stored-procedure source is translated for the SQL Server family only (EXEC syntax)")
            if not isinstance(name, str) or not re.fullmatch(r"[\w.\[\]]+", name):
                raise _Stub("the stored procedure name is not a plain literal")
            ps = src.get("storedProcedureParameters") or {}
            if not isinstance(ps, dict) or not all(_pyid(k) for k in ps):
                raise _Stub("a stored procedure parameter name is not a plain identifier")
            call = f"EXEC {name} " + ", ".join(f"@{k} = :{k}" for k in ps)
            vals = "{" + ", ".join(f"{k!r}: {self._tr(v.get('value') if isinstance(v, dict) and 'value' in v else v, dsvar)}" for k, v in ps.items()) + "}"
            return repr(call.strip()), vals, iso
        tb = self._tbl(ds, dsvar)
        if not tb:
            raise _Stub("the source dataset names no table and the Copy has no query")
        return f"fx.concat('SELECT * FROM ', {tb})", "None", iso

    def _mapping(self, tp: dict) -> str:
        """Python source for the column mapping of a tabular copy: 'None' (the default mapping, by column name), a {source: sink} dict, or a run-time
        call for a translator that is an expression (metadata-driven pipelines). Sources by name or by 1-based ordinal (Prop_<n-1> for delimited text without
        a header); anything else is a stub."""
        tr = tp.get("translator")
        if not tr:
            return "None"
        if isinstance(tr, str) or (isinstance(tr, dict) and tr.get("type") == "Expression"):
            self.notes.append("the column mapping is an expression and is read when the copy runs (names and ordinals only)")
            return f"fx.translator_mapping({self._tr(tr)})"
        if not (isinstance(tr, dict) and tr.get("type") == "TabularTranslator"):
            raise _Stub("only a TabularTranslator is translated")
        if tr.get("typeConversion") or tr.get("mapComplexValuesToString") is not None:
            self.notes.append("typeConversion settings are not applied: delimited text and Excel are read as text, and a database applies its own column types")
        ms = tr.get("mappings")
        if not ms:
            return "None"
        out = {}
        for m in ms:
            src, snk = (m or {}).get("source") or {}, (m or {}).get("sink") or {}
            if "path" in src or "path" in snk:
                raise _Stub("a path mapping needs a hierarchical source (REST), not this pair")
            if "ordinal" in snk or not snk.get("name"):
                raise _Stub("a mapping to a sink column by ordinal is not translated")
            key = src.get("name") or (f"Prop_{int(src['ordinal']) - 1}" if "ordinal" in src else None)
            if key is None:
                raise _Stub("a mapping names no source column")
            out[key] = snk["name"]
        return repr(out)

    def _path_mapping(self, tp: dict, src: dict):
        """([(source path, sink name)], collection reference) for a hierarchical source (REST) with path mappings, else (None, collection)."""
        tr = tp.get("translator")
        coll = (tr.get("collectionReference") if isinstance(tr, dict) else None) or src.get("collectionReference") or ""
        if not isinstance(tr, dict) or not tr.get("mappings"):
            return None, coll
        pairs = []
        for m in tr["mappings"]:
            s_, k_ = (m or {}).get("source") or {}, (m or {}).get("sink") or {}
            if "path" not in s_:
                raise _Stub("a REST copy mixes named and path mappings")
            sink = k_.get("name") or (re.findall(r"\['([^']+)'\]", k_["path"])[-1] if re.findall(r"\['([^']+)'\]", str(k_.get("path", ""))) else k_.get("path"))
            if not sink:
                raise _Stub("a path mapping names no sink column")
            pairs.append((s_["path"], sink))
        return pairs, coll

    def _copy(self, a: Activity, tp: dict) -> list[str]:
        src, snk = tp.get("source"), tp.get("sink")
        if not isinstance(src, dict) or not isinstance(snk, dict):
            raise _Stub("the source or sink is a deploy-time value that could not be resolved")
        if not a.inputs or not a.outputs:
            raise _Stub(f"{src.get('type', '?')} -> {snk.get('type', '?')} with no input or output dataset (legacy format)")
        stype, ktype = str(src.get("type")), str(snk.get("type"))
        ignored = [k for k in ("enableSkipIncompatibleRow", "validateDataConsistency", "redirectIncompatibleRowSettings", "logSettings", "preserve")
                   if tp.get(k)]
        if ignored:
            self.notes.append(f"{a.name}: Copy setting(s) not enforced: {', '.join(ignored)}")
        lines: list[str] = []
        fam_src = stype[:-6] if stype.endswith("Source") else ""
        fam_snk = ktype[:-4] if ktype.endswith("Sink") else ""

        # file to file: same format is a byte copy; delimited text, JSON and Parquet (and Excel as a source) convert through pandas
        if (fam_src in FILE_FAMILY or fam_src == "Excel") and fam_snk in FILE_FAMILY:
            dsi, dso = self._ds(a.inputs[0], "ds_in", lines), self._ds(a.outputs[0], "ds_out", lines)
            if dsi.type != fam_src or dso.type != fam_snk:
                raise _Stub(f"dataset types {dsi.type} -> {dso.type} do not match the {fam_src} -> {fam_snk} copy")
            if (src.get("formatSettings") or {}).get("skipLineCount"):
                raise _Stub("skipLineCount is not translated")
            ss, ks = self._store_settings(src, "source"), self._store_settings(snk, "sink")
            surl, senv, sfile = self._location(dsi, "ds_in", root_only=bool(ss.get("wildcardFolderPath") or ss.get("prefix")))
            durl, denv, dfile = self._location(dso, "ds_out")
            wild = self._tr(ss.get("wildcardFileName")) if ss.get("wildcardFileName") else (sfile or "''")
            wf = self._tr(ss["wildcardFolderPath"]) if ss.get("wildcardFolderPath") else "''"
            behavior = str(ks.get("copyBehavior", "PreserveHierarchy"))
            if fam_src == fam_snk and self._fmt_signature(dsi) == self._fmt_signature(dso):
                kw, _ = self._store_kwargs(ss, binary_copy=fam_src == "Binary")
                self.notes.append(f"{a.name}: files are copied byte for byte; ADF's own file-level checks and logging are not reproduced")
                if behavior == "MergeFiles":
                    kw += ", merge=True"
                    self.notes.append(f"{a.name}: MergeFiles joins the files' bytes in name order into one file (the sink's file name, else data_<guid>)")
                return lines + [f"return adf_copy_files(ctx, {a.name!r}, {surl}, {senv!r}, {durl}, {denv!r}, {dfile or chr(39)*2}, "
                                f"{bool(ss.get('recursive', True))}, {wild}, {behavior == 'FlattenHierarchy'}, {wf}{kw})"]
            if behavior == "MergeFiles":
                raise _Stub("MergeFiles is translated for same-format byte copies only")
            if fam_src not in ("DelimitedText", "Json", "Parquet", "Excel") or fam_snk not in ("DelimitedText", "Json", "Parquet"):
                raise _Stub(f"a {fam_src} to {fam_snk} reformat is not translated (delimited text, JSON and Parquet, and Excel as a source, are)")
            conv_kw, _ = self._store_kwargs(ss, partitions=True)
            self.uses_pandas, self.uses_parquet = True, self.uses_parquet or "Parquet" in (fam_src, fam_snk)
            self.uses_excel = self.uses_excel or fam_src == "Excel"
            self.notes.append(f"{a.name}: converted through pandas: delimited text and Excel are read as text (no schema is applied), each input file becomes one output file named like it "
                              f"with the new extension (merged into the sink's file name when it has one and there are several inputs)")
            return lines + [f"return adf_copy_convert(ctx, {a.name!r}, {surl}, {senv!r}, {fam_src!r}, {self._opts(dsi)}, {durl}, {denv!r}, {fam_snk!r}, "
                            f"{self._opts(dso, snk)}, {dfile or chr(39)*2}, {bool(ss.get('recursive', True))}, {wild}, {wf}, mapping={self._mapping(tp)}{conv_kw})"]

        # REST to a JSON file
        if stype == "RestSource" and ktype in ("JsonSink", "BinarySink", "DelimitedTextSink"):
            rules = src.get("paginationRules")
            interval = 1                                          # the documented default is 00:00:01
            if src.get("requestInterval"):
                ri = src["requestInterval"]
                if not isinstance(ri, str) or is_dynamic(ri) or _dur_seconds(ri, -1) < 0:
                    raise _Stub("the REST request interval is not a plain timespan")
                interval = _dur_seconds(ri)
            if rules:
                ok = isinstance(rules, dict) and all(
                    isinstance(v, (str, int)) and not is_dynamic(v) and re.fullmatch(
                        r"AbsoluteUrl(\.\{\w+\})?|QueryParameters(\.[\w-]+|\.\{\w+\}|\['[^']+'\])|Headers(\.[\w-]+|\.\{\w+\}|\['[^']+'\])|EndCondition:.+|MaxRequestNumber|SupportRFC5988", k)
                    for k, v in rules.items())
                if not ok:
                    raise _Stub(f"REST pagination rules {sorted(rules) if isinstance(rules, dict) else rules!r} use keys outside the documented vocabulary "
                                "(AbsoluteUrl, QueryParameters.x, Headers.x, their {variable} RANGE forms, EndCondition:, MaxRequestNumber, SupportRFC5988)")
            dsi, dso = self._ds(a.inputs[0], "ds_in", lines), self._ds(a.outputs[0], "ds_out", lines)
            ls = self.f.linked_services.get(dsi.linked_service)
            if ls is not None and ls.type != "RestService":
                raise _Stub(f"linked service {dsi.linked_service!r} is a {ls.type}, not a RestService")
            base = ls.props.get("url") if ls is not None else None
            if ls is not None and str(ls.props.get("authenticationType", "Anonymous")) != "Anonymous":
                self.web_auth.append(a.name)
                raise _Stub(f"REST authentication {ls.props.get('authenticationType')} is not carried over (credentials are never copied)")
            if ls is None or not isinstance(base, str) or not base or base.startswith("["):
                lsn = re.sub(r"\W", "_", (re.fullmatch(r"\[parameters\('([^']+)'\)\]", dsi.linked_service or "") or [None, dsi.linked_service or "rest"])[1]).upper()
                env = f"ADF_REST_URL_{lsn}"
                self.notes.append(f"{a.name}: the REST base url is not in the export, so it is read from {env}; the service's authentication is unknown, so none is sent (pass headers if it needs a key)")
                url = f"fx.concat(fx.envvar({env!r}), {self._tr(dsi.props.get('relativeUrl', ''), 'ds_in')})"
            else:
                url = f"fx.concat({base!r}, {self._tr(dsi.props.get('relativeUrl', ''), 'ds_in')})"
            durl, denv, dfile = self._location(dso, "ds_out")
            rest_fmt = "DelimitedText" if ktype == "DelimitedTextSink" else "Json"
            if rest_fmt == "DelimitedText" and dso.type != "DelimitedText":
                raise _Stub(f"sink dataset type {dso.type} is not translated")
            if not dfile:
                ext = ".csv" if rest_fmt == "DelimitedText" else ".json"
                dfile = f"fx.concat('data_', fx.guid(), {ext!r})"
                self.notes.append(f"{a.name}: the sink names no file, so the file is called data_<guid>{ext} (ADF also generates a name; the pattern may differ)")
            if rest_fmt == "DelimitedText":
                self.uses_pandas = True
            path_pairs, coll = self._path_mapping(tp, src)
            if path_pairs:
                self.uses_pandas = self.uses_pandas or rest_fmt == "DelimitedText"
                self.notes.append(f"{a.name}: rows are built from the path mappings (paths starting with $ read the document, others the current element of the collection reference, one row per element)")
            elif tp.get("translator") and isinstance(tp["translator"], dict) and tp["translator"].get("mappings") is not None and not path_pairs:
                pass
            if path_pairs:
                pass
            elif rest_fmt == "DelimitedText":
                self.notes.append(f"{a.name}: the rows (the items at the collection reference, else each response) are flattened with pandas' json_normalize into delimited text; check the columns against what ADF writes")
            elif rules or coll:
                self.notes.append(f"{a.name}: pages are followed by the AbsoluteUrl rule and the rows (the items at the collection reference, else each page) are written one JSON object per line; check the layout against what ADF writes")
            else:
                self.notes.append(f"{a.name}: the response body is written as received; ADF re-serialises REST results as JSON, so layout may differ")
            return lines + [f"return adf_copy_rest(ctx, {a.name!r}, {url}, {str(src.get('requestMethod', 'GET'))!r}, "
                            f"{self._tr(src.get('additionalHeaders') or {})}, {self._tr(src['requestBody']) if src.get('requestBody') is not None else 'None'}, "
                            f"{durl}, {denv!r}, {dfile}" + (f", pagination={rules!r}" if rules else "") + (f", collection={coll!r}" if coll else "") + (f", interval_s={interval}" if rules or src.get("paginationRules") else "")
                            + (f", timeout_s={_dur_seconds(src['httpRequestTimeout'], 100)}" if src.get("httpRequestTimeout") else "")
                            + (f", dst_fmt='DelimitedText', dst_opts={self._opts(dso)}" if rest_fmt == "DelimitedText" else "")
                            + (f", path_mappings={path_pairs!r}" if path_pairs else "") + ")"]

        # Parquet, JSON or Excel files to a SQL table
        if stype in ("ParquetSource", "JsonSource", "ExcelSource") and ktype in SQL_SINKS:
            dsi, dso = self._ds(a.inputs[0], "ds_in", lines), self._ds(a.outputs[0], "ds_out", lines)
            fmt = stype[:-6]
            if dsi.type != fmt:
                raise _Stub(f"dataset type {dsi.type} does not match the {fmt} source")
            sink_kw = self._sql_sink_checks(snk)
            mapping = self._mapping(tp)
            ss = self._store_settings(src, "source")
            src_kw, _ = self._store_kwargs(ss, partitions=True)
            surl, senv, sfile = self._location(dsi, "ds_in", root_only=bool(ss.get("wildcardFolderPath") or ss.get("prefix")))
            env = self._conn(dso.linked_service, a)
            tbl = self._tbl(dso, "ds_out")
            if not tbl:
                raise _Stub("the sink dataset names no table")
            wild = self._tr(ss["wildcardFileName"]) if ss.get("wildcardFileName") else (sfile or "''")
            pre = self._tr(snk["preCopyScript"]) if snk.get("preCopyScript") else "''"
            self.uses_pandas, self.uses_parquet, self.uses_excel = True, self.uses_parquet or fmt == "Parquet", self.uses_excel or fmt == "Excel"
            self.notes.append(f"{a.name}: rows are inserted with the destination table's own column types (the table must exist unless tableOption is autoCreate)")
            return lines + [f"return adf_copy_frames_to_sql(ctx, {a.name!r}, {surl}, {senv!r}, {fmt!r}, {self._opts(dsi)}, {env!r}, {tbl}, recursive={bool(ss.get('recursive', True))}, "
                            f"wildcard={wild}, wildcard_folder={self._tr(ss['wildcardFolderPath']) if ss.get('wildcardFolderPath') else chr(39) * 2}, pre_script={pre}, mapping={mapping}{sink_kw}{src_kw})"]

        # delimited text files to a SQL table
        if stype == "DelimitedTextSource" and ktype in SQL_SINKS:
            dsi, dso = self._ds(a.inputs[0], "ds_in", lines), self._ds(a.outputs[0], "ds_out", lines)
            if dsi.type != "DelimitedText":
                raise _Stub(f"dataset type {dsi.type} does not match the DelimitedText source")
            sink_kw = self._sql_sink_checks(snk)
            mapping = self._mapping(tp)
            header = bool(dsi.props.get("firstRowAsHeader", False))
            if not header and mapping == "None":
                raise _Stub("delimited text without a header row needs an explicit column mapping by ordinal (the documentation says the Copy activity requires one)")
            ss = self._store_settings(src, "source")
            src_kw, _ = self._store_kwargs(ss, partitions=True)
            if (src.get("formatSettings") or {}).get("skipLineCount"):
                raise _Stub("skipLineCount is not translated")
            surl, senv, sfile = self._location(dsi, "ds_in", root_only=bool(ss.get("wildcardFolderPath") or ss.get("prefix")))
            env = self._conn(dso.linked_service, a)
            tbl = self._tbl(dso, "ds_out")
            if not tbl:
                raise _Stub("the sink dataset names no table")
            wild = self._tr(ss.get("wildcardFileName")) if ss.get("wildcardFileName") else (sfile or "''")
            pre = self._tr(snk["preCopyScript"]) if snk.get("preCopyScript") else "''"
            o = eval(self._opts(dsi))
            self.notes.append(f"{a.name}: values are inserted as text and the null value ({o['null_value']!r}) as NULL; the database converts types (ADF converts by the dataset schema)")
            return lines + [f"return adf_copy_csv_to_sql(ctx, {a.name!r}, {surl}, {senv!r}, {env!r}, {tbl}, "
                            f"delimiter={o['delimiter']!r}, quote={o['quote']!r}, encoding={o['encoding']!r}, recursive={bool(ss.get('recursive', True))}, "
                            f"wildcard={wild}, pre_script={pre}, mapping={mapping}, header={header}, null_value={o['null_value']!r}{sink_kw}{src_kw}, "
                            f"wildcard_folder={self._tr(ss['wildcardFolderPath']) if ss.get('wildcardFolderPath') else chr(39) * 2})"]

        # SQL table or query to a delimited text, JSON or Parquet file
        if stype in SQL_SOURCES and ktype in ("DelimitedTextSink", "JsonSink", "ParquetSink"):
            dsi, dso = self._ds(a.inputs[0], "ds_in", lines), self._ds(a.outputs[0], "ds_out", lines)
            want = {"DelimitedTextSink": "DelimitedText", "JsonSink": "Json", "ParquetSink": "Parquet"}[ktype]
            if dso.type != want:
                raise _Stub(f"sink dataset type {dso.type} is not translated")
            env = self._conn(dsi.linked_service, a)
            q, qparams, iso = self._sql_source(src, dsi, "ds_in", stype in MSSQL_SOURCES)
            read_kw = (f", params={qparams}" if qparams != "None" else "") + iso
            durl, denv, dfile = self._location(dso, "ds_out")
            if not dfile:
                ext = {"DelimitedText": ".txt", "Json": ".json", "Parquet": ".parquet"}[want]
                dfile = f"fx.concat('data_', fx.guid(), {ext!r})"
                self.notes.append(f"{a.name}: the sink names no file, so the file is called data_<guid>{ext} (ADF also generates a name; the pattern may differ)")
            if want == "Parquet":
                self.uses_pandas = self.uses_parquet = True
                comp = self._opts(dso)
                return lines + [f"return adf_copy_sql_to_parquet(ctx, {a.name!r}, {env!r}, {q}, {durl}, {denv!r}, {dfile}, compression={(eval(comp).get('compression') or 'snappy')!r}, mapping={self._mapping(tp)}{read_kw})"]
            if want == "Json":
                arr = str((snk.get("formatSettings") or {}).get("filePattern", "setOfObjects")).lower() == "arrayofobjects"
                return lines + [f"return adf_copy_sql_to_json(ctx, {a.name!r}, {env!r}, {q}, {durl}, {denv!r}, {dfile}, array={arr}, "
                                f"encoding={self._encoding(dso.props.get('encodingName', 'UTF-8'))!r}, mapping={self._mapping(tp)}{read_kw})"]
            o = eval(self._opts(dso))
            return lines + [f"return adf_copy_sql_to_csv(ctx, {a.name!r}, {env!r}, {q}, {durl}, {denv!r}, {dfile}, "
                            f"delimiter={o['delimiter']!r}, quote={o['quote']!r}, header={o['header']}, encoding={o['encoding']!r}, "
                            f"row_delimiter={o['row_delimiter']!r}, null_value={o['null_value']!r}, mapping={self._mapping(tp)}{read_kw})"]

        # SQL to SQL (any pair of SQL-family databases), inline
        if stype in SQL_SOURCES and ktype in SQL_SINKS:
            dsi, dso = self._ds(a.inputs[0], "ds_in", lines), self._ds(a.outputs[0], "ds_out", lines)
            sink_kw = self._sql_sink_checks(snk)
            mapping = self._mapping(tp)
            senv, denv = self._conn(dsi.linked_service, a), self._conn(dso.linked_service, a)
            q, qparams, iso = self._sql_source(src, dsi, "ds_in", stype in MSSQL_SOURCES)
            read_kw = (f", params={qparams}" if qparams != "None" else "") + iso
            tbl = self._tbl(dso, "ds_out")
            if not tbl:
                raise _Stub("the sink dataset names no table")
            pre = self._tr(snk["preCopyScript"]) if snk.get("preCopyScript") else "''"
            self.notes.append(f"{a.name}: rows are streamed in chunks through SQLAlchemy; the destination table must exist (unless tableOption is autoCreate) and its column types apply")
            return lines + [f"return adf_copy_sql_to_sql(ctx, {a.name!r}, {senv!r}, {q}, {denv!r}, {tbl}, pre_script={pre}, mapping={mapping}{read_kw}{sink_kw})"]

        raise _Stub(self._stub_reason(a))

    def _databricks(self, a: Activity, tp: dict) -> list[str]:
        ls = self.f.linked_services.get(a.linked_service)
        if ls is None or ls.type != "AzureDatabricks":
            raise _Stub(f"linked service {a.linked_service!r} is not an AzureDatabricks linked service in the export")
        lp = ls.props
        cid, kw = lp.get("existingClusterId"), ""
        if isinstance(cid, str) and cid and not cid.startswith("["):
            kw = f"existing_cluster_id={cid!r}"
        elif lp.get("instancePoolId"):
            raise _Stub("the linked service uses an instance pool, which is not translated")
        elif all(isinstance(lp.get(k), str) and not lp.get(k, "[").startswith("[") for k in ("newClusterVersion", "newClusterNodeType", "newClusterNumOfWorker")):
            n = str(lp["newClusterNumOfWorker"])
            am = re.fullmatch(r"(\d+):(\d+)", n)
            if n.isdigit():
                size = f"'num_workers': {int(n)}"
            elif am:
                size = f"'autoscale': {{'min_workers': {int(am.group(1))}, 'max_workers': {int(am.group(2))}}}"
            else:
                raise _Stub(f"new cluster size {n!r} is not a worker count or a min:max range")
            kw = ("new_cluster={" + f"'spark_version': {lp['newClusterVersion']!r}, 'node_type_id': {lp['newClusterNodeType']!r}, {size}" + "}")
        else:
            raise _Stub("the linked service names no usable cluster (existing cluster id or version, node type and worker count)")
        if tp.get("libraries"):
            self.notes.append(f"{a.name}: the notebook's libraries are not installed on the run")
        self.used_dbx.add(a.linked_service)
        params = self._tr(tp.get("baseParameters") or {})
        return [f"return adf_databricks_notebook(ctx, {a.name!r}, {self._tr(tp.get('notebookPath'))}, {params}, "
                + kw + ")"]

    def _stub_reason(self, a: Activity) -> str:
        t, tp = a.type, a.props if isinstance(a.props, dict) else {}
        if t == "Copy":
            src = tp.get("source", {}).get("type", "?") if isinstance(tp.get("source"), dict) else "?"
            snk = tp.get("sink", {}).get("type", "?") if isinstance(tp.get("sink"), dict) else "?"
            ins = ", ".join(d.get("referenceName", "?") for d in a.inputs) or "none"
            outs = ", ".join(d.get("referenceName", "?") for d in a.outputs) or "none"
            return f"{src} -> {snk} (datasets in: {ins}; out: {outs})"
        if t == "ExecuteDataFlow":
            return f"data flow {((tp.get('dataflow') or {}).get('referenceName') or '?')!r}"
        if t == "ExecuteSSISPackage":
            return f"package {((tp.get('packageLocation') or {}).get('packagePath') or '?')!r}"
        if t == "DatabricksNotebook":
            return f"notebook {tp.get('notebookPath', '?')!r}"
        if t in STUB_HINT:
            return "no automatic translation"
        return "unknown activity type"

    def _storage(self, a: Activity, tp: dict) -> list[str]:
        lines: list[str] = []
        ds = self._ds(tp.get("dataset"), "ds", lines)
        url, env, fname = self._location(ds, "ds")
        st = tp.get("storeSettings") if isinstance(tp.get("storeSettings"), dict) else {}
        if a.type == "GetMetadata":
            if st.get("modifiedDatetimeStart") or st.get("modifiedDatetimeEnd"):
                raise _Stub("a modified-time filter on childItems is not translated")
            fields = tp.get("fieldList") or []
            names = [f if isinstance(f, str) else str(f) for f in fields]
            bad = [f for f in names if f.lower() not in ("exists", "itemname", "itemtype", "size", "lastmodified", "childitems")]
            if bad:
                raise _Stub(f"metadata field(s) not translated: {', '.join(bad)} (exists, itemName, itemType, size, lastModified and childItems are)")
            target = f"fx.concat({url}, '/', {fname})" if fname else url
            return lines + [f"return adf_get_metadata(ctx, {a.name!r}, {target}, {env!r}, {names!r})"]
        if a.type == "Delete":
            if st.get("wildcardFolderPath"):
                raise _Stub("the Delete activity does not support wildcard folder paths (the documentation says so)")
            del_kw, _ = self._store_kwargs(st)
            if tp.get("enableLogging"):
                self.notes.append(f"{a.name}: Delete logging (enableLogging) is not reproduced")
            wild = self._tr(st["wildcardFileName"]) if st.get("wildcardFileName") else (fname or "''")
            self.notes.append(f"{a.name}: a path that does not exist fails the Delete here; check ADF's behavior for a missing path")
            return lines + [f"return adf_delete(ctx, {a.name!r}, {url}, {env!r}, {bool(st.get('recursive', False))}, {wild}{del_kw})"]
        target = f"fx.concat({url}, '/', {fname})" if fname else url
        timeout = _dur_seconds(tp.get("timeout"), 12 * 3600)
        return lines + [f"return adf_validate(ctx, {a.name!r}, {target}, {env!r}, {timeout}, {int(tp.get('sleep', 10))}, "
                        f"{tp.get('minimumSize')!r}, {tp.get('childItems')!r})"]

    def _lookup_file(self, a: Activity, tp: dict, src: dict) -> list[str]:
        lines: list[str] = []
        ds = self._ds(tp.get("dataset"), "ds", lines)
        fmt = str(src.get("type"))[:-6]
        if ds.type != fmt:
            raise _Stub(f"dataset type {ds.type} does not match the {fmt} source")
        ss = self._store_settings(src, "source")
        lk_kw, _ = self._store_kwargs(ss)
        url, env, fname = self._location(ds, "ds", root_only=bool(ss.get("wildcardFolderPath") or ss.get("prefix")))
        wild = self._tr(ss["wildcardFileName"]) if ss.get("wildcardFileName") else (fname or "''")
        wf = self._tr(ss["wildcardFolderPath"]) if ss.get("wildcardFolderPath") else "''"
        kw = ""
        if fmt == "DelimitedText":
            kw = (f", delimiter={ds.props.get('columnDelimiter', ',')!r}, quote={ds.props.get('quoteChar', chr(34))!r}, header={bool(ds.props.get('firstRowAsHeader', False))}, "
                  f"encoding={str(ds.props.get('encodingName', 'UTF-8')).lower()!r}")
        return lines + [f"return adf_lookup_file(ctx, {a.name!r}, {url}, {env!r}, {fmt!r}, {bool(tp.get('firstRowOnly', True))}, "
                        f"{bool(ss.get('recursive', False))}, {wild}, {wf}{kw}{lk_kw})"]

    def _lookup(self, a: Activity, tp: dict) -> list[str]:
        src = tp.get("source")
        if isinstance(src, dict) and src.get("type") in ("DelimitedTextSource", "JsonSource"):
            return self._lookup_file(a, tp, src)
        if not isinstance(src, dict) or src.get("type") not in SQL_SOURCES:
            raise _Stub(f"Lookup over {src.get('type') if isinstance(src, dict) else 'a deploy-time source'} is not translated (SQL sources only)")
        dref = tp.get("dataset") or {}
        ds = self.f.datasets.get(dref.get("referenceName", ""))
        if ds is None:
            raise _Stub(f"dataset {dref.get('referenceName')!r} is not in the export")
        env = self._conn(ds.linked_service, a)
        lines = []
        dsvar = ""
        if dref.get("parameters"):
            dsvar = "ds"
            lines.append("ds = " + self._tr(dref["parameters"]))
        if source_query(src):
            q = self._tr(source_query(src), dsvar)
        elif src.get("sqlReaderStoredProcedureName"):
            name = src["sqlReaderStoredProcedureName"]
            if not isinstance(name, str) or not re.fullmatch(r"[\w.\[\]]+", name):
                raise _Stub("the stored procedure name is not a plain literal")
            ps = src.get("storedProcedureParameters") or {}
            if not isinstance(ps, dict) or not all(_pyid(k) for k in ps):
                raise _Stub("a stored procedure parameter name is not a plain identifier")
            call = f"EXEC {name} " + ", ".join(f"@{k} = :{k}" for k in ps)
            vals = "{" + ", ".join(f"{k!r}: {self._tr((v or {}).get('value') if isinstance(v, dict) else v, dsvar)}" for k, v in ps.items()) + "}"
            first = tp.get("firstRowOnly", True)
            return lines + [f"return adf_sql(ctx, {a.name!r}, {env!r}, {call.strip()!r}, {vals}, {'first' if first else 'all'!r}, lookup=True)"]
        else:
            tbl = self._tbl(ds, dsvar)
            if not tbl:
                raise _Stub("the dataset names no table and the Lookup has no query")
            q = f"fx.concat('SELECT * FROM ', {tbl})"
        first = tp.get("firstRowOnly", True)
        return lines + [f"return adf_sql(ctx, {a.name!r}, {env!r}, {q}, None, {'first' if first else 'all'!r}, lookup=True)"]


def _py_default(t: str, v):
    t = (t or "String").lower()
    if t in ("array", "object"):
        return json.dumps(v if v is not None else ([] if t == "array" else {}))
    if t == "int":
        return int(v) if isinstance(v, (int, float)) else 0
    if t == "float":
        return float(v) if isinstance(v, (int, float)) else 0.0
    if t == "bool":
        return bool(v) if isinstance(v, bool) else False
    return v if isinstance(v, str) else ""


def _py_type(t: str) -> str:
    return {"int": "int", "float": "float", "bool": "bool"}.get((t or "String").lower(), "str")


def convert(factory: Factory, pkg: str = "adf_migrated") -> Result:
    items: list[Item] = []
    code: list[str] = []
    meta: dict = {"pipelines": {}, "triggers": {}}
    taken: set[str] = set()

    # triggers, grouped by pipeline
    by_pipe: dict[str, list[tuple[Trigger, dict]]] = {}
    for t in factory.triggers.values():
        for ref in t.pipelines:
            by_pipe.setdefault(ref["name"], []).append((t, ref))
    called = {(a.props.get("pipeline") or {}).get("referenceName") for p in factory.pipelines.values() for a in _walk(p.activities)
              if a.type == "ExecutePipeline" and isinstance(a.props, dict)}

    sched_code: list[str] = []
    defs_names: list[str] = []
    sched_names: list[str] = []
    job_names: list[str] = []
    ssis_pkgs: dict[str, dict] = {}                    # qualified name -> declared package
    syn_ws: dict[str, dict] = {}                       # workspace -> {"notebooks": {name: deps}, "jobs": {name: deps}}
    sql_copies: list[dict] = []
    flow_comps: list[dict] = []
    pbi_comps: list[tuple[str, dict]] = []
    fabric_ws: dict[str, dict] = {}                    # workspace id (or '' for the current workspace) -> declared notebooks

    def keystr(k: list[str]) -> str:
        return "/".join(k)

    for p in factory.pipelines.values():
        g = PipeGen(factory, p)
        asset = slug(p.name)
        while asset in taken:
            asset += "_"
        taken.add(asset)
        g.slug = slug(p.name)

        # parameters, variables
        types, unresolved, defaults_all = {}, [], {}
        for name, spec in p.parameters.items():
            spec = spec if isinstance(spec, dict) else {}
            types[name] = spec.get("type", "String")
            if spec.get("defaultValue") is None:
                unresolved.append(name)
            defaults_all[name] = _py_default(types[name], spec.get("defaultValue"))
        variables = {n: ((v or {}).get("defaultValue") if (v or {}).get("defaultValue") is not None
                         else ([] if str((v or {}).get("type", "")).lower() == "array" else False if str((v or {}).get("type", "")).lower() == "bool" else ""))
                     for n, v in p.variables.items()}

        # tumbling window partitions
        part_def, sched_for_asset, trig_notes = "", [], []
        tw = [(t, r) for t, r in by_pipe.get(p.name, []) if t.type == "TumblingWindowTrigger"]
        trigger_expr_params = {}
        window_retry = ""
        if tw:
            t, ref = tw[0]
            cron, tn = window_cron(t.props)
            if cron:
                start = str(t.props.get("startTime", "2024-01-01T00:00:00Z")).replace("Z", "")[:16] + ":00"
                end = str(t.props.get("endTime", "")).replace("Z", "")[:19]
                part_def = f"PD_{asset} = dg.TimeWindowPartitionsDefinition(cron_schedule={cron!r}, start={start!r}, fmt='%Y-%m-%dT%H:%M:%S'" + (f", end={end!r})" if end else ")")
                trigger_expr_params = ref.get("parameters") or {}
                for k in ("delay", "maxConcurrency", "dependsOn"):
                    if t.props.get(k):
                        tn.append(f"tumbling window {k} is not enforced")
                rp = t.props.get("retryPolicy") or {}
                if isinstance(rp, dict) and int(rp.get("count") or 0) > 0:
                    window_retry = f"dg.RetryPolicy(max_retries={int(rp['count'])}, delay={max(30, int(rp.get('intervalInSeconds') or 30))})"
                tn.append("ADF runs every window from startTime up to now when the trigger starts (a backfill, oldest first); Dagster creates the latest window's run only, so launch a "
                          "backfill of the partitions for the history")
                meta["triggers"][t.name] = {"cron": cron, "partitioned": asset}
            else:
                meta["triggers"][t.name] = {"cron": None, "partitioned": ""}
            trig_notes += [f"trigger {t.name}: {x}" for x in tn]
        has_part = bool(part_def)

        # split around the activities a registry component runs
        nodes, hints, comps = plan_split(factory, p, g._order(p.activities), asset, called=p.name in called, partitioned=has_part)
        g.hints = hints
        keys_of: dict[int, list[str]] = {}
        units: list[dict] = []                         # this pipeline's assets, in dependency order
        if nodes is None:
            units.append({"kind": "python", "name": asset, "acts": p.activities, "label": "main", "deps": [], "key": [asset]})
        else:
            nseg = sum(1 for n in nodes if n.kind == "segment")
            segno = 0
            for i, n in enumerate(nodes):
                if n.kind == "segment":
                    segno += 1
                    nm = asset if nseg == 1 else f"{asset}__part{segno}"
                    units.append({"kind": "python", "name": nm, "acts": n.acts, "label": f"main{segno}", "node": i, "key": [nm]})
                else:
                    units.append({"kind": "component", "comp": n.comp, "node": i, "key": n.comp.key, "acts": n.acts})
            for u in units:
                keys_of[u["node"]] = [u["key"], *(u["comp"].extra_keys if u["kind"] == "component" else [])]
            for u in units:
                u["deps"] = [k for j in nodes[u["node"]].deps for k in keys_of[j]]

        asset_keys: list[list[str]] = []
        ops_configs: dict[str, dict] = {}               # python asset -> {param: field}
        unit_assets: list[dict] = []                    # for gate 3: {asset key string, deps strings}
        for u in units:
            if u["kind"] == "component":
                c = u["comp"]
                g.total += 1
                g.notes += [f"{c.activity}: {x}" for x in c.notes]
                for ls_name in c.used_ls:
                    g.used_ls.add(ls_name)
                dep_strs = [keystr(k) for k in u["deps"]]
                if c.kind == "ssis":
                    q = "/".join([c.spec["folder"], c.spec["project"], c.spec["package"]])
                    e = ssis_pkgs.setdefault(q, {"folder_name": c.spec["folder"], "project_name": c.spec["project"], "package_name": c.spec["package"],
                                                 "key": c.key, "deps": [], "params": c.spec["params"]})
                    if e["params"] != c.spec["params"]:
                        g.notes.append(f"{c.activity}: the package also runs elsewhere with different parameters; one asset stands for both and the first parameters are used")
                    e["deps"] += [d for d in dep_strs if d not in e["deps"]]
                elif c.kind in ("synapse_notebook", "synapse_spark"):
                    ws = c.spec["workspace"]
                    bucket = syn_ws.setdefault(ws, {"notebooks": {}, "jobs": {}, "linked_service": c.spec["linked_service"]})
                    d = bucket["notebooks" if c.kind == "synapse_notebook" else "jobs"].setdefault(c.spec["name"], [])
                    d += [x for x in dep_strs if x not in d]
                    if c.spec.get("params"):
                        bucket.setdefault("params", {}).setdefault(c.spec["name"], c.spec["params"])
                elif c.kind == "fabric_notebook":
                    bucket = fabric_ws.setdefault(c.spec["workspace"], {"notebooks": {}})
                    e = bucket["notebooks"].setdefault(c.spec["name"], {"id": c.spec["id"], "params": c.spec["params"], "deps": []})
                    e["deps"] += [x for x in dep_strs if x not in e["deps"]]
                elif c.kind == "pbi_refresh":
                    attrs = {"asset_key": keystr(c.key), **c.spec["attrs"], "group_name": "adf_fabric"}
                    env_slug = c.spec["slug"].upper()
                    attrs.update({"workspace_id": "{{ env.PBI_WORKSPACE_ID or 'SET_PBI_WORKSPACE_ID' }}",
                                  "dataset_id": "{{ env.PBI_DATASET_ID_" + env_slug + " or 'SET_PBI_DATASET_ID_" + env_slug + "' }}",
                                  "tenant_id_env_var": "AZURE_TENANT_ID", "client_id_env_var": "AZURE_CLIENT_ID", "client_secret_env_var": "AZURE_CLIENT_SECRET"})
                    if dep_strs:
                        attrs["deps"] = dep_strs
                    pbi_comps.append((c.spec["slug"], attrs))
                elif c.kind == "warehouse_flow":
                    attrs = dict(c.spec["attrs"])
                    attrs["group_name"] = "adf"
                    if dep_strs:
                        attrs["deps"] = dep_strs
                    flow_comps.append(attrs)
                    g.used_flows.add(c.spec["flow"])
                    g.flow_components.add(c.spec["flow"])
                else:
                    attrs = dict(c.spec["attrs"])
                    attrs["group_name"] = "adf"
                    if dep_strs:
                        attrs["deps"] = dep_strs
                    sql_copies.append(attrs)
                for k in [c.key, *c.extra_keys]:
                    asset_keys.append(k)
                    unit_assets.append({"asset": keystr(k), "deps": sorted(dep_strs)})
                continue

            nm = u["name"]
            top = g._scope_acts(u["acts"], u["label"])
            text = json.dumps([a.props for a in u["acts"]], default=str)
            used = [n for n in p.parameters if nodes is None or re.search(r"parameters\.%s\b|parameters\['%s'\]" % (re.escape(n), re.escape(n)), text)]
            cls = "".join(w.capitalize() for w in nm.split("_")).strip() + "Config"
            if not cls[0].isalpha():
                cls = "P" + cls
            fields, mapping = [], {}
            for name in used:
                fld = slug(name)
                if not _pyid(fld) or fld in ("self", "config"):
                    fld = "p_" + fld
                mapping[name] = fld
                fields.append(f"    {fld}: {_py_type(types[name])} = {defaults_all[name]!r}")
            ops_configs[nm] = mapping
            if has_part and nodes is None:
                code.append(part_def + "\n")
            body_params = ", ".join(f"{k!r}: config.{v}" for k, v in mapping.items())
            kinds = "{'python'}"
            asset_args = [f"name={nm!r}", f"group_name={slug(p.folder) if p.folder else 'adf'!r}", f"description={(p.description or '').strip()[:300]!r}", f"kinds={kinds}"]
            if has_part and nodes is None:
                asset_args.append(f"partitions_def=PD_{asset}")
                if window_retry:
                    asset_args.append(f"retry_policy={window_retry}")
            if u["deps"]:
                asset_args.append("deps=[" + ", ".join(f"dg.AssetKey({k!r})" for k in u["deps"]) + "]")
            code.extend(g.defs)
            g.defs.clear()
            code.append(f"class {cls}(dg.Config):\n" + ("\n".join(fields) if fields else "    pass") + "\n")
            trig_apply = ""
            if has_part and trigger_expr_params and nodes is None:
                try:
                    over = ", ".join(f"{k!r}: {translate(v)}" for k, v in trigger_expr_params.items() if k in mapping)
                    trig_apply = f"    if trigger:\n        ctx = Ctx({p.name!r}, {{}}, {{}}, {{}}, trigger=trigger)\n        params.update({{{over}}})\n"
                except Untranslatable as e:
                    trig_notes.append(f"trigger parameters not translated: {e}")
            dflt = {k: defaults_all[k] for k in used}
            code.append(f"_DEFAULTS_{nm} = {dflt!r}\n")
            code.append(
                f"def run_{nm}(params=None, *, trigger=None, log=print, parent=None):\n"
                f"    ctx = Ctx({p.name!r}, {{**_DEFAULTS_{nm}, **(params or {{}})}}, {types!r}, {variables!r}, log=log, trigger=trigger, parent=parent)\n"
                f"    {top}(ctx, None)\n    return ctx\n")
            code.append(
                f"@dg.asset({', '.join(asset_args)})\n"
                f"def {nm}(context: dg.AssetExecutionContext, config: {cls}) -> None:\n"
                f"    params = {{{body_params}}}\n"
                f"    trigger = window_trigger({p.name!r}, context) if context.has_partition_key else schedule_trigger({p.name!r}, context)\n"
                f"{trig_apply}"
                f"    run_{nm}(params, trigger=trigger, log=context.log.info)\n")
            defs_names.append(nm)
            asset_keys.append(u["key"])
            unit_assets.append({"asset": nm, "deps": sorted(keystr(k) for k in u["deps"])})
        if nodes is not None:
            g.notes.append("split into assets around "
                           + ", ".join(f"{n.comp.activity} ({keystr(n.comp.key)})" for n in nodes if n.kind == "component")
                           + "; the Python parts run as separate assets, and a run of the pipeline is a run of all of them")

        notes = list(p.notes) + g.notes
        select = "dg.AssetSelection.assets(" + ", ".join(f"dg.AssetKey({k!r})" for k in asset_keys) + ")"

        # schedules
        for t, ref in by_pipe.get(p.name, []):
            if t.type == "TumblingWindowTrigger":
                if meta["triggers"].get(t.name, {}).get("cron") and t is tw[0][0] and nodes is None:
                    job = f"{asset}_job"
                    sched_code.append(f"{job} = dg.define_asset_job({job!r}, selection=[{asset!r}])\n"
                                      f"{slug(t.name)}_schedule = dg.build_schedule_from_partitioned_job({job}, name={slug(t.name) if len(t.pipelines) == 1 else slug(t.name) + '__' + asset!r}, "
                                      f"default_status=dg.DefaultScheduleStatus.STOPPED)\n")
                    sched_names.append(f"{slug(t.name)}_schedule")
                    job_names.append(job)
                    sched_for_asset.append({"name": slug(t.name) if len(t.pipelines) == 1 else slug(t.name) + "__" + asset, "cron": meta["triggers"][t.name]["cron"]})
            elif t.type == "ScheduleTrigger":
                cron, sn = recurrence_to_cron(t.props.get("recurrence") or {})
                if cron is None or has_part:
                    why = sn or ["the pipeline is partitioned by a tumbling window trigger, which cannot also take a plain schedule"]
                    meta["triggers"].setdefault(t.name, {"cron": None})["why"] = why
                    continue
                tzname = (t.props.get("recurrence") or {}).get("timeZone", "UTC")
                tz = WINDOWS_TZ.get(tzname)
                if tz is None:
                    sn = sn + [f"time zone {tzname!r} is not mapped; UTC used"]
                    tz = "UTC"
                lit = {k: v for k, v in (ref.get("parameters") or {}).items() if k in p.parameters and not is_dynamic(v)}
                dyn = [k for k, v in (ref.get("parameters") or {}).items() if is_dynamic(v)]
                if dyn:
                    sn.append(f"trigger parameters {', '.join(dyn)} use expressions and are not set by the schedule (they take the pipeline defaults)")
                ops = {}
                for nm, mapping in ops_configs.items():
                    cfg_ = {mapping[k]: _coerce(types.get(k), v) for k, v in lit.items() if k in mapping}
                    if cfg_:
                        ops[nm] = {"config": cfg_}
                rc = {"ops": ops} if ops else None
                nmx = slug(t.name) if len(t.pipelines) == 1 else f"{slug(t.name)}__{asset}"
                sched_code.append(f"{nmx}_schedule = dg.ScheduleDefinition(name={nmx!r}, target={select}, cron_schedule={cron!r}, "
                                  f"execution_timezone={tz!r}, default_status=dg.DefaultScheduleStatus.STOPPED"
                                  + (f", run_config={rc!r}" if rc else "") + ")\n")
                sched_names.append(f"{nmx}_schedule")
                sched_for_asset.append({"name": nmx, "cron": cron})
                meta["triggers"][t.name] = {"cron": cron, "notes": sn, "name": nmx}
                trig_notes += [f"trigger {t.name}: {x}" for x in sn]

        # pipeline item
        stubs = len(g.stubs)
        if p.activities and stubs >= g.total and g.total > 0:
            status, caveat = UNMAPPED, "stub"
        elif stubs:
            status, caveat = REVIEW, "stub"
        elif notes or trig_notes:
            status, caveat = REVIEW, "degraded"
        else:
            status, caveat = CONVERTED, ""
        item_notes = [f"{t} '{n}': {r}" for t, n, r in g.stubs] + notes + trig_notes
        if not p.activities:
            item_notes.append("the pipeline has no activities")
        expected = {"schedules": sched_for_asset}
        if nodes is None:
            expected.update({"asset": asset, "deps": []})
        else:
            expected["assets"] = unit_assets
        items.append(Item("pipeline", p.name, "assets " + ", ".join(keystr(k) for k in asset_keys) if nodes is not None else f"asset {asset}", status, item_notes, caveat, expected))
        meta["pipelines"][p.name] = {
            "asset": asset, "stubs": g.stubs, "unresolved": unresolved, "params": list(p.parameters), "used_ls": sorted(g.used_ls),
            "web_auth": g.web_auth, "uses_identity": g.uses_identity, "uses_fabric": g.uses_fabric, "uses_pandas": g.uses_pandas, "uses_parquet": g.uses_parquet, "uses_excel": g.uses_excel, "uses_mail": g.uses_mail, "used_fn": dict(g.used_fn), "used_dbx": sorted(g.used_dbx), "used_flows": sorted(g.used_flows), "flow_components": sorted(g.flow_components), "used_fs": dict(g.used_fs), "called": p.name in called, "triggers": [t.name for t, _ in by_pipe.get(p.name, [])],
            "partitioned": has_part, "types": sorted({t for t, _, _ in g.stubs}), "split": nodes is not None,
            "components": [{"kind": c.kind, "activity": c.activity, "key": keystr(c.key), "workspace": c.spec.get("workspace", ""), "linked_service": c.spec.get("linked_service", "")} for c in comps.values()] if nodes is not None else [],
        }

    # component YAML for the split pipelines
    files: dict[str, str] = {}

    def doc(typ: str, attrs: dict) -> str:
        return yaml.safe_dump({"type": f"{pkg}.components.{typ}", "attributes": attrs}, sort_keys=False, default_flow_style=False)

    def registry_doc(typ: str, attrs: dict) -> str:
        """A registry component used as is. `dry_run` follows ADF_DRY_RUN so the migration gate's run sends nothing."""
        attrs = {**attrs, "dry_run": "{{ env.ADF_DRY_RUN == '1' }}"}
        return yaml.safe_dump({"type": f"dagster_community_components.{typ}", "attributes": attrs}, sort_keys=False, default_flow_style=False)

    if ssis_pkgs:
        by_pkg, overrides, declared = {}, [], []
        for q, e in ssis_pkgs.items():
            declared.append({"folder_name": e["folder_name"], "project_name": e["project_name"], "package_name": e["package_name"]})
            by_pkg[q] = {"key": keystr(e["key"]), **({"deps": e["deps"]} if e["deps"] else {})}
            if e["params"]:
                overrides.append({"match": q, "parameters": e["params"]})
        attrs = {"action": "execute", "group_name": "adf_ssis", "workspace": {"connection_string_env_var": "ADF_CONN_SSISDB", "database": "SSISDB"},
                 "packages": declared, "assets_by_package": by_pkg}
        if overrides:
            attrs["package_overrides"] = overrides
        files["defs/adf_ssis/defs.yaml"] = registry_doc("SsisWorkspaceComponent", attrs)
    for n, (ws, b) in enumerate(sorted(syn_ws.items()), 1):
        env = lambda v: "{{ env." + v + " or 'SET_" + v + "' }}"       # noqa: E731
        ov = {f"synapse_notebook_{nb}": {"depends_on": d} for nb, d in b["notebooks"].items() if d}
        ov.update({f"synapse_spark_job_{j}": {"depends_on": d} for j, d in b["jobs"].items() if d})
        attrs = {"subscription_id": env("AZURE_SUBSCRIPTION_ID"), "resource_group_name": env("AZURE_RESOURCE_GROUP"),
                 "workspace_name": ws or env("SYNAPSE_WORKSPACE"), "tenant_id": env("AZURE_TENANT_ID"), "client_id": env("AZURE_CLIENT_ID"),
                 "client_secret": env("AZURE_CLIENT_SECRET"), "import_pipelines": False, "import_notebooks": bool(b["notebooks"]),
                 "import_spark_jobs": bool(b["jobs"]), "generate_sensor": False, "group_name": "adf_synapse",
                 "notebooks": [{"name": nb, **({"parameters": b["params"][nb]} if nb in b.get("params", {}) else {})} for nb in b["notebooks"]], "spark_jobs": [{"name": j} for j in b["jobs"]]}
        if ov:
            attrs["asset_overrides"] = ov
        files[f"defs/adf_synapse_{n}/defs.yaml"] = registry_doc("AzureSynapseComponent", attrs)
    for n, (wsid, b) in enumerate(sorted(fabric_ws.items()), 1):
        env = lambda v: "{{ env." + v + " or 'SET_" + v + "' }}"       # noqa: E731
        attrs = {"workspace": {"workspace_id": wsid or env("FABRIC_WORKSPACE_ID"), "tenant_id": env("AZURE_TENANT_ID"), "client_id": env("AZURE_CLIENT_ID"),
                               "client_secret": env("AZURE_CLIENT_SECRET")},
                 "key": f"adf_fabric_{n}", "group_name": "adf_fabric", "import_lakehouses": False, "import_warehouses": False, "import_notebooks": True,
                 "import_pipelines": False, "import_dataflows": False,
                 "notebooks": [{"item_id": e["id"], "display_name": nm} for nm, e in b["notebooks"].items()]}
        ip = {nm: e["params"] for nm, e in b["notebooks"].items() if e["params"]}
        idp = {nm: e["deps"] for nm, e in b["notebooks"].items() if e["deps"]}
        if ip:
            attrs["item_parameters"] = ip
        if idp:
            attrs["item_deps"] = idp
        files[f"defs/adf_fabric_{n}/defs.yaml"] = registry_doc("FabricWorkspaceComponent", attrs)
    for slug_, attrs in pbi_comps:
        files[f"defs/adf_pbi_{slug_}/defs.yaml"] = registry_doc("SemanticModelRefreshAssetComponent", attrs)
    for attrs in sql_copies:
        files[f"defs/adf_sqlcopy_{attrs['asset_name']}/defs.yaml"] = registry_doc("SQLToDatabaseAssetComponent", attrs)
    for attrs in flow_comps:
        files[f"defs/adf_flow_{attrs['asset_name']}/defs.yaml"] = doc("AdfWarehousePipelineComponent", attrs)
    meta["component_files"] = sorted(files)
    meta["uses"] = {"ssis": bool(ssis_pkgs), "synapse": bool(syn_ws), "sqlcopy": bool(sql_copies), "flow": bool(flow_comps), "pbi": bool(pbi_comps), "fabric": bool(fabric_ws)}

    # triggers as units
    for t in factory.triggers.values():
        m = meta["triggers"].get(t.name, {})
        refs = ", ".join(r["name"] for r in t.pipelines) or "no pipeline"
        if t.type == "ScheduleTrigger" and m.get("cron"):
            notes = list(m.get("notes") or []) + ([f"ADF runtime state is {t.state}; the schedule starts stopped so both systems can run in parallel"] if t.state else [])
            items.append(Item("trigger", t.name, f"schedule {m['name']} ({m['cron']})", REVIEW if m.get("notes") else CONVERTED, notes,
                              "degraded" if m.get("notes") else ""))
        elif t.type == "TumblingWindowTrigger" and m.get("cron"):
            items.append(Item("trigger", t.name, f"partitions + schedule on {m['partitioned']} ({m['cron']})", REVIEW,
                              ["tumbling window as a time-partitioned asset; windows are aligned to the cadence, not to the trigger's start time",
                               "window dependencies, delay, retries and max concurrency are not enforced"], "degraded"))
        else:
            why = (m.get("why") or ["no translation for this trigger type"]) if t.type in ("ScheduleTrigger", "TumblingWindowTrigger") else \
                [f"{t.type} has no translation yet: nothing was generated, the pipeline must be started another way"]
            items.append(Item("trigger", t.name, "none", UNMAPPED, [f"for {refs}: " + w for w in why], "stub"))

    for ls in factory.linked_services.values():
        dbx_users = sorted(n for n, m in meta["pipelines"].items() if ls.name in m["used_dbx"])
        if dbx_users:
            dom = ls.props.get("domain", "")
            items.append(Item("linked_service", ls.name, "env DATABRICKS_HOST and DATABRICKS_TOKEN (databricks-sdk unified auth)", REVIEW,
                              [x for x in (f"used by {', '.join(dbx_users)}", f"workspace {dom}" if dom else "", f"{ls.redacted} secret value(s) were dropped; credentials are never copied" if ls.redacted else "") if x], "degraded"))
            continue
        syn_users = sorted(n for n, m in meta["pipelines"].items() for c in m["components"] if c["kind"].startswith("synapse") and c.get("linked_service") == ls.name)
        if syn_users:
            items.append(Item("linked_service", ls.name, "env AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET (AdfAzureSynapseComponent)", REVIEW,
                              [x for x in (f"used by {', '.join(sorted(set(syn_users)))}", f"workspace {ls.props.get('endpoint')}" if ls.props.get("endpoint") else "",
                                           f"{ls.redacted} secret value(s) were dropped; credentials are never copied" if ls.redacted else "") if x], "degraded"))
            continue
        fn_users = sorted(n for n, m in meta["pipelines"].items() if ls.name in m["used_fn"])
        if fn_users:
            kvname = next((m["used_fn"][ls.name] for m in meta["pipelines"].values() if m["used_fn"].get(ls.name)), "")
            items.append(Item("linked_service", ls.name, f"function app {ls.props.get('functionAppUrl')}; env var ADF_FUNCTIONKEY_{re.sub(r'\W', '_', ls.name).upper()} for the function key", REVIEW,
                              [x for x in (f"used by {', '.join(fn_users)}", f"the key is Key Vault secret {kvname!r} in ADF: put its value in the environment variable (it was not copied)" if kvname else "",
                                           f"{ls.redacted} secret value(s) were dropped; credentials are never copied" if ls.redacted else "") if x], "degraded"))
            continue
        fs_users = sorted(n for n, m in meta["pipelines"].items() if ls.name in m["used_fs"])
        if fs_users:
            proto = FS_LS.get(ls.type, "")
            what = "a local path on the machine running Dagster (a file server reached through a self-hosted runtime in ADF)" if proto == "file" else \
                f"env var ADF_FS_{re.sub(r'\W', '_', ls.name).upper()} (JSON fsspec storage options, for {proto}://)"
            items.append(Item("linked_service", ls.name, what, REVIEW,
                              [x for x in (f"used by {', '.join(fs_users)}",
                                           f"{ls.redacted} secret value(s) were dropped; credentials are never copied" if ls.redacted else "",
                                           f"runs through integration runtime {ls.connect_via}: the Dagster code location needs the same network path" if ls.connect_via else "") if x], "degraded"))
            continue
        users = sorted(n for n, m in meta["pipelines"].items() if ls.name in m["used_ls"])
        sec = f"{ls.redacted} secret value(s) were dropped; credentials are never copied" if ls.redacted else ""
        via = f"runs through integration runtime {ls.connect_via}: the Dagster code location needs the same network path" if ls.connect_via else ""
        if ls.name in {l for m in meta["pipelines"].values() for l in m["used_ls"]}:
            items.append(Item("linked_service", ls.name, f"env var {ls_env(ls.name)} (SQLAlchemy URL)", REVIEW,
                              [x for x in (f"used by {', '.join(users)}", sec, via) if x], "degraded"))
        else:
            items.append(Item("linked_service", ls.name, "none", UNMAPPED,
                              [x for x in (f"{ls.type}: no translation (used by Copy or storage activities, or not used by a translated activity)", sec, via) if x], "stub"))
    flows_used = {n: sorted(pn for pn, m in meta["pipelines"].items() if n in m.get("used_flows", [])) for n in factory.data_flows}
    for name, t in factory.data_flows.items():
        if flows_used.get(name):
            as_comp = sorted(pn for pn, m in meta["pipelines"].items() if name in m.get("flow_components", []))
            if as_comp:
                items.append(Item("data_flow", name, "warehouse_pipeline component (SQL steps, one asset per sink)", REVIEW,
                                  [f"run by {', '.join(as_comp)}; each transformation is a SQL step and each sink a table rebuilt with CREATE OR REPLACE; the SQL was checked against the inline "
                                   "executor, not against ADF or your database"], "degraded"))
            else:
                items.append(Item("data_flow", name, "adf_dataflow (runs in this process)", REVIEW,
                                  [f"run by {', '.join(flows_used[name])}; translated to a row-by-row Python run (exact, not Spark-fast); check the output against a run in ADF"], "degraded"))
            continue
        d = factory.data_flow_defs.get(name) or {}
        script = d.get("script") or "\n".join(d.get("scriptLines") or [])
        reason = f"{t or 'data flow'}: not translated"
        if script.strip():
            from . import dflow
            try:
                why = dflow.check_flow(dflow.parse_script(script)[1])
            except dflow.Unsupported as e:
                why = [str(e)]
            reason = ("; ".join(sorted(dict.fromkeys(why))[:3]) + (f" (and {len(why) - 3} more)" if len(why) > 3 else "")) if why else \
                "no pipeline runs it, or the pipeline that does could not translate its datasets (see that pipeline)"
        else:
            reason += ": the export has no script; rewrite in SQL or Spark, or keep it in ADF"
        items.append(Item("data_flow", name, "none", UNMAPPED, [reason + ". Rewrite it in SQL or Spark, or keep running it in ADF"], "stub"))

    header = textwrap.dedent('''\
        """Generated by adf-to-dagster. One asset per ADF pipeline; see MIGRATION.md for what was translated and what was not."""
        import dagster as dg

        from ..runtime import (Ctx, Out, adf_caveat, adf_copy_csv_to_sql, adf_copy_files, adf_copy_rest, adf_copy_sql_to_csv, adf_copy_sql_to_json,
                               adf_copy_convert, adf_copy_frames_to_sql, adf_copy_sql_to_parquet, adf_copy_sql_to_sql, adf_databricks_notebook, adf_dataflow, adf_delete, adf_fabric_run, df_param, adf_fail, adf_filter, adf_foreach, adf_function, adf_get_metadata,
                               adf_lookup_file, adf_mail, adf_script, adf_sql, adf_until, adf_validate, adf_wait, adf_web, fx, run_activities, schedule_trigger, window_trigger)

        ''')
    tail = ("\n@dg.definitions\ndef defs() -> dg.Definitions:\n    return dg.Definitions(\n"
            f"        assets=[{', '.join(defs_names)}],\n"
            f"        jobs=[{', '.join(job_names)}],\n"
            f"        schedules=[{', '.join(sched_names)}],\n    )\n")
    full = header + "\n".join(code) + "\n" + "".join(sched_code) + tail
    res = Result(code=full, items=items, meta=meta, files=files)
    res.questions = pipeline_questions(factory, meta)
    return res


def _coerce(t, v):
    t = (t or "String").lower()
    if t in ("array", "object"):
        return json.dumps(v)
    return v


def _walk(acts):
    for a in acts:
        yield a
        tp = a.props if isinstance(a.props, dict) else {}
        for k in ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities"):
            yield from _walk(parse_activities(tp.get(k)))
        for c in tp.get("cases") or []:
            if isinstance(c, dict):
                yield from _walk(parse_activities(c.get("activities")))


def write_project(result: Result, out_dir: Path, pkg: str, factory_name: str = "") -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pipes = result.meta.get("pipelines", {}).values()
    protos = {pr for m in pipes for pr in m.get("used_fs", {}).values()}
    deps = ('"dagster>=1.11", "sqlalchemy>=2"' + (', "databricks-sdk"' if any(m.get("used_dbx") for m in pipes) else "")
            + (', "azure-identity"' if any(m.get("uses_identity") for m in pipes) and not result.meta.get("uses", {}).get("synapse") else "")
            + (', "dagster-community-components>=0.10.179", "azure-identity", "requests"' if any(m.get("uses_fabric") for m in pipes) else "")
            + (', "pandas"' if any(m.get("uses_pandas") for m in pipes) and not result.meta.get("uses", {}).get("sqlcopy") else "")
            + (', "pyarrow"' if any(m.get("uses_parquet") for m in pipes) else "") + (', "openpyxl"' if any(m.get("uses_excel") for m in pipes) else "")
            + (', "fsspec"' if protos else "") + (', "adlfs"' if "abfs" in protos else "") + (', "s3fs"' if "s3" in protos else ""))
    uses = result.meta.get("uses", {})
    comp = any(uses.values())
    if comp:
        deps += ', "dagster-community-components>=0.10.179"'
        if uses.get("synapse"):
            deps += ', "azure-identity", "azure-synapse-artifacts", "azure-mgmt-synapse"'
        if uses.get("flow"):
            deps += ', "pandas"'
        if uses.get("sqlcopy"):
            deps += ', "pandas"'
        if uses.get("fabric") or uses.get("pbi"):
            deps += ', "azure-identity", "requests"'
    registry = f'registry_modules = ["{pkg}.components"]' if comp else ""
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
    (root / "dflow.py").write_text(DFLOW_SOURCE)
    (root / "definitions.py").write_text(textwrap.dedent('''\
        from pathlib import Path

        from dagster import definitions, load_from_defs_folder


        @definitions
        def defs():
            return load_from_defs_folder(path_within_project=Path(__file__).parent)
        '''))
    (root / "defs" / "adf.py").write_text(result.code)
    if comp:
        (root / "components.py").write_text(COMPONENTS_SOURCE)
        for rel, content in result.files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    (out_dir / "MIGRATION.md").write_text(render_report(result))


def render_report(result: Result) -> str:
    counts = {CONVERTED: 0, REVIEW: 0, UNMAPPED: 0}
    for i in result.items:
        counts[i.status] += 1
    total = sum(counts.values())
    rate = 100 * counts[CONVERTED] / total if total else 100
    stubs = sum(1 for i in result.items if i.caveat == "stub")
    degraded = sum(1 for i in result.items if i.caveat == "degraded")
    lines = [
        "# Migration report", "",
        f"{total} source objects: **{counts[CONVERTED]} converted**, **{counts[REVIEW]} need review**, "
        f"**{counts[UNMAPPED]} unmapped** ({rate:.0f}% converted without review).", "",
        f"Caveats: **{stubs} with a stub** (the asset exists and fails with `ADF-CAVEAT: <reason>` until it is ported) and "
        f"**{degraded} converted with a known difference** (see Notes). Nothing is silently dropped.", "",
        "Converted means the generated code loads and encodes the same structure. It does not mean behavior was verified against ADF; run both in "
        "parallel before cutover. Differences that apply to every pipeline: independent activities run one after another (ADF runs them in parallel), "
        "activity timeouts are not enforced, and every schedule starts stopped.", "",
        "| Kind | Source | Dagster artifact | Status | Notes |", "|---|---|---|---|---|",
    ]
    order = {UNMAPPED: 0, REVIEW: 1, CONVERTED: 2}
    for i in sorted(result.items, key=lambda x: (order[x.status], x.kind, x.source)):
        lines.append(f"| {i.kind} | {i.source} | {i.artifact} | {i.status} | {'; '.join(i.notes).replace('|', '/')} |")
    lines += render_questions(result.questions)
    return "\n".join(lines) + "\n"
