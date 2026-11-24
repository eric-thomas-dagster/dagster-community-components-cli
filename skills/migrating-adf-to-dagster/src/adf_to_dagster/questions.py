"""Questions only the customer can answer, per pipeline, derived from what the export contains and does not contain."""
from __future__ import annotations

import re

from .model import Factory

_KEYLIKE = re.compile(r"(date|(^|_)dt($|_)|day|period|month|year|week|hour|window|region|plant|site|store|company|fiscal|batch|run_?id|table|file)", re.I)

_BY_TYPE = {
    "ExecuteSSISPackage": "Which SSIS catalog (server, folder, project) holds the package, and can the Dagster deployment reach it? SsisWorkspaceComponent in the registry runs packages from SSISDB",
    "ExecuteDataFlow": "How large and how critical is each Mapping Data Flow, and can it be rewritten in SQL or Spark (or stay in ADF and be triggered)?",
    "ExecuteWranglingDataflow": "Which Power Query data flows are these, and can the logic move to SQL?",
    "SynapseNotebook": "Which Synapse workspace and Spark pool run the notebooks, and are they still needed?",
    "SparkJob": "Which Synapse Spark job definitions are these, and where do they run?",
    "HDInsightSpark": "Which HDInsight cluster and Spark entry file are these?",
    "DatabricksNotebook": "Which Databricks workspace, and which cluster, should these notebooks run on? The linked service must be an AzureDatabricks one with an existing cluster id or new-cluster settings",
    "Copy": "For each Copy that is still a stub (see its note for the pair): the source and sink systems, credentials, and whether the volume needs a managed connector or a plain Python copy is enough",
    "Delete": "Which storage account and containers do the Delete activities clean, and with which identity?",
    "GetMetadata": "Which storage account do the GetMetadata activities read, and with which identity?",
    "Validation": "Which storage path does the Validation activity wait for, and for how long?",
}


def pipeline_questions(f: Factory, meta: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, p in f.pipelines.items():
        m = meta["pipelines"].get(name, {})
        q: list[str] = []
        if m.get("unresolved"):
            q.append(f"What does the trigger or caller pass for {', '.join(m['unresolved'])}? (no default in the export; the generated asset takes them as run config, blank until set)")
        keys = sorted({x for x in m.get("params", []) if _KEYLIKE.search(x)})
        if keys:
            q.append(f"Is {', '.join(keys)} the business key this pipeline runs for (a date, window, region or table)? If so it can become a partition, so you can backfill and see which keys ran; if not, leave it as a parameter")
        if not m.get("triggers") and not m.get("called"):
            q.append("When does this pipeline run? No trigger in the export starts it and no other pipeline calls it (it may be started by a person, an API call or an event)")
        elif m.get("triggers"):
            q.append("Confirm the trigger against what actually runs today (the ADF monitor history is the check), and say whether the ADF trigger should stay on during a parallel run")
        for t in m.get("triggers", []):
            tr = f.triggers.get(t)
            if tr and tr.type == "TumblingWindowTrigger":
                q.append(f"Trigger {t} is a tumbling window: does a window depend on the previous one, and are past windows re-run when they fail? The converter reports these settings but does not enforce them")
            elif tr and tr.type not in ("ScheduleTrigger",):
                q.append(f"Trigger {t} is a {tr.type}: what raises the event, and can a Dagster sensor on the same source replace it? Nothing was generated for it")
        for typ in m.get("types", []):
            if typ == "ExecuteDataFlow" and m.get("used_flows"):
                q.append(f"Data flow {', '.join(m['used_flows'])} runs in this process over lists of rows, not on Spark: how many rows does each source hold (everything is read into memory), and can you run it in ADF and "
                         "here on the same input and compare the sinks? Output files are named by partitionFileNames, delimited text is written with a line feed, and each derived column sees the incoming row")
            elif typ in _BY_TYPE:
                q.append(_BY_TYPE[typ])
        for ls in m.get("used_ls", []):
            lsd = f.linked_services.get(ls)
            q.append(f"Which database does linked service {ls} point to, and how will its credentials be supplied (set ADF_CONN_{re.sub(r'\W', '_', ls).upper()} to a SQLAlchemy URL)?"
                     + (f" It runs through integration runtime {lsd.connect_via}, so the Dagster code location needs the same network path." if lsd and lsd.connect_via else ""))
        for c in m.get("components", []):
            if c["kind"] == "ssis":
                q.append(f"SSIS package {c['key'].split('/', 1)[1]}: set ADF_CONN_SSISDB to a SQLAlchemy URL for the SSISDB catalog (an Azure-SSIS catalog lives on Azure SQL; install the driver your URL names, such as pyodbc). "
                         "The package runs through SsisWorkspaceComponent, which discovers nothing at load time here (the package is declared from the ADF activity)")
            elif c["kind"].startswith("synapse"):
                q.append(f"Synapse {c['activity']}: set AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET (a service principal with access to workspace "
                         f"{c['workspace'] or '(name it: SYNAPSE_WORKSPACE)'}). The registry component polls the run to its final state and fails the asset if the notebook or job fails")
            elif c["kind"] == "fabric_notebook":
                q.append(f"Fabric notebook {c['activity']}: set AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET (a service principal that is a member of the workspace), and FABRIC_WORKSPACE_ID if the "
                         "notebook is in the pipeline's own workspace. It runs through fabric_workspace; the asset is named after the activity because the pipeline holds only the notebook's id. "
                         "Does anything read the notebook's exit value? That needs the component's exit_value_file and lakehouse_id")
            elif c["kind"] == "pbi_refresh":
                q.append(f"Semantic model refresh {c['activity']}: which workspace and model does it refresh? Set PBI_WORKSPACE_ID and the model's PBI_DATASET_ID_* variable named in its defs.yaml, plus AZURE_TENANT_ID, "
                         "AZURE_CLIENT_ID and AZURE_CLIENT_SECRET for a service principal that is a workspace member and allowed to use the Power BI APIs (a tenant admin setting)")
            elif c["kind"] == "warehouse_flow":
                q.append(f"Data flow {c['activity']}: set the SQLAlchemy URL named in its defs.yaml (database_url_env_var) for the database it reads and writes. It runs as the warehouse_pipeline "
                         "component: each transformation is a SQL step, and each sink table is rebuilt with CREATE OR REPLACE instead of truncated and loaded, so check grants, indexes and "
                         "columns the data flow never wrote. Run it beside ADF on the same input and compare the tables")
            elif c["kind"] == "sqlcopy":
                q.append(f"SQL copy {c['activity']}: set the two ADF_CONN_* URLs it names; the component moves rows through pandas in chunks, so check column types on the destination")
        for ls, proto in sorted(m.get("used_fs", {}).items()):
            lsd = f.linked_services.get(ls)
            if proto == "file":
                q.append(f"Copy reads or writes file server linked service {ls}: where does that path live for the Dagster deployment (a mount, or copy the files to blob or S3 first)?"
                         + (f" ADF reaches it through integration runtime {lsd.connect_via}." if lsd and lsd.connect_via else ""))
            else:
                q.append(f"Which storage account and credentials does linked service {ls} use? Set ADF_FS_{re.sub(r'\W', '_', ls).upper()} to a JSON object of fsspec storage options for {proto}:// "
                         f"(for blob and ADLS, account_name plus a key, SAS token or service principal); nothing was copied from ADF")
        for ls in m.get("used_dbx", []):
            q.append(f"Linked service {ls} runs Databricks notebooks: set DATABRICKS_HOST and DATABRICKS_TOKEN (or another databricks-sdk auth setting) for the Dagster deployment; the access token was not copied. Should the notebook runs be modelled as a Databricks workflow (dagster-databricks) instead of one-off runs?")
        if m.get("web_auth"):
            q.append(f"How do the web calls {', '.join(m['web_auth'])} authenticate (managed identity, service principal, key)? Credentials are never copied from ADF")
        q.append("Who owns this pipeline, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known")
        out[name] = q
    return out


def render_questions(questions: dict[str, list[str]]) -> list[str]:
    if not questions:
        return []
    lines = ["", "## Needs your input", "",
             "The export cannot answer these; each answer changes what the converter generates. Work through them pipeline by pipeline.", ""]
    for pipe, qs in sorted(questions.items()):
        lines += [f"### {pipe}", ""] + [f"- [ ] {x}" for x in qs] + [""]
    return lines
