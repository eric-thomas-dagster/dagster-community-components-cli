"""Runtime helpers copied into every generated project (src/<pkg>/runtime.py).

Kept dependency-light: shell steps use subprocess; SQL steps use SQLAlchemy only if
the project installs it and the connection env var is set.
"""
RUNTIME_SOURCE = '''\
"""Helpers used by the generated RunMyJobs assets."""
from __future__ import annotations

import os
import string
import subprocess
import sys
import tempfile
from typing import Any

import dagster as dg


def rmj_caveat(reason: str) -> None:
    """A part of the RunMyJobs definition the converter did not translate. Fails loudly, with the reason."""
    raise dg.Failure(description=f"RMJ-CAVEAT: {reason}")


def _dry() -> bool:
    """RMJ_DRY_RUN=1 (set by `validate --execute`): render and log the work, do not run it."""
    return os.environ.get("RMJ_DRY_RUN") == "1"


def _render(text: str, params: dict[str, Any]) -> str:
    return string.Template(text).safe_substitute({k: v for k, v in params.items()})


def run_shell(context: dg.AssetExecutionContext, command: str, params: dict[str, Any],
              timeout: int | None = None, run_as: str = "", ok_codes: tuple[int, ...] = (), ignore_failure: bool = False) -> None:
    """Run an OS process definition. Parameters become $name substitutions and RMJ_PARAM_* env vars."""
    env = {**os.environ, **{f"RMJ_PARAM_{k.upper()}": str(v) for k, v in params.items()}}
    rendered = _render(command, params)
    if run_as:
        context.log.info(f"(RunMyJobs ran this as OS user {run_as!r}; Dagster runs it as the code-location user)")
    context.log.info(f"$ {rendered}")
    if _dry():
        context.log.info("DRY RUN: command not executed")
        return
    proc = subprocess.run(rendered, shell=True, capture_output=True, text=True, env=env, timeout=timeout)
    if proc.stdout:
        context.log.info(proc.stdout)
    if proc.returncode != 0 and proc.returncode not in ok_codes and ignore_failure:
        context.log.warning(f"command exited {proc.returncode}; ignored (RunMyJobs marks this step completed on error)\\n{proc.stderr[-2000:]}")
        return
    if proc.returncode != 0 and proc.returncode not in ok_codes:
        raise dg.Failure(
            description=f"command exited {proc.returncode}",
            metadata={"stderr": proc.stderr[-4000:], "command": rendered},
        )


def run_python(context: dg.AssetExecutionContext, source: str, params: dict[str, Any],
               timeout: int | None = None, ignore_failure: bool = False) -> None:
    """Run a Python process definition's source as a script. Parameters become RMJ_PARAM_* env vars."""
    env = {**os.environ, **{f"RMJ_PARAM_{k.upper()}": str(v) for k, v in params.items()}}
    source = _render(source, params)
    compile(source, "<process>", "exec")                            # a syntax error fails here, in a dry run too
    if _dry():
        context.log.info("DRY RUN: script compiled, not executed")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(source)
        path = f.name
    try:
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True, env=env, timeout=timeout)
    finally:
        os.unlink(path)
    if proc.stdout:
        context.log.info(proc.stdout)
    if proc.returncode != 0 and ignore_failure:
        context.log.warning(f"script exited {proc.returncode}; ignored (RunMyJobs marks this step completed on error)\\n{proc.stderr[-2000:]}")
        return
    if proc.returncode != 0:
        raise dg.Failure(description=f"script exited {proc.returncode}", metadata={"stderr": proc.stderr[-4000:]})


def run_sql(context: dg.AssetExecutionContext, sql: str, params: dict[str, Any], connection_env: str) -> None:
    """Run a SQL process definition against the database named by env var `connection_env` (a SQLAlchemy URL)."""
    if _dry():
        context.log.info(_render(sql, params))
        context.log.info("DRY RUN: SQL not executed")
        return
    url = os.environ.get(connection_env)
    if not url:
        raise dg.Failure(description=f"set {connection_env} to a SQLAlchemy URL to run this SQL step")
    from sqlalchemy import create_engine, text  # imported lazily: only SQL steps need it

    rendered = _render(sql, params)
    context.log.info(rendered)
    with create_engine(url).begin() as conn:
        conn.execute(text(rendered))

def rmj_config(params: dict[str, Any], config: Any) -> dict[str, Any]:
    """Defaults from the export, overridden by any non-empty run config value."""
    given = config.model_dump() if hasattr(config, "model_dump") else config.dict()
    return {**params, **{k: v for k, v in given.items() if v != ""}}


def run_mail(context: dg.AssetExecutionContext, params: dict[str, Any]) -> None:
    """Send an email (RunMyJobs System_Mail_Send). Parameters To, Subject, Text (HTML when it starts with <html>), optional Cc and Bcc.
    Env: SMTP_HOST (required), SMTP_PORT (587), SMTP_USER and SMTP_PASSWORD (optional login), SMTP_FROM, SMTP_STARTTLS (1). Attachments are not sent."""
    import smtplib
    from email.message import EmailMessage
    p = {k.lower(): ("" if v is None else str(v)) for k, v in params.items()}
    to = [a.strip() for a in p.get("to", "").replace(";", ",").split(",") if a.strip()]
    if _dry():
        context.log.info(f"DRY RUN: email not sent. To={to} Subject={p.get('subject', '')!r}")
        return
    if not to:
        raise dg.Failure(description="System_Mail_Send has no recipient: set the To parameter (the export has none when the chain supplies it at run time)")
    host = os.environ.get("SMTP_HOST")
    if not host:
        raise dg.Failure(description="set SMTP_HOST (and SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM) to send this email")
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "dagster@localhost"
    msg["To"] = ", ".join(to)
    for h in ("cc", "bcc"):
        if p.get(h):
            msg[h.capitalize()] = p[h]
    msg["Subject"] = p.get("subject", "")
    body = p.get("text", "")
    if body.lstrip().lower().startswith("<html"):
        msg.set_content("This message is HTML.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=60) as s:
        if os.environ.get("SMTP_STARTTLS", "1") == "1":
            s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
    context.log.info(f"email sent to {to}")


def run_dbt_cloud(context: dg.AssetExecutionContext, job_id: int, params: dict[str, Any],
                  timeout: int | None = None, cause: str = "Triggered by Dagster (migrated from RunMyJobs)") -> None:
    """Trigger a dbt Cloud job by id and wait for it. Env: DBT_CLOUD_TOKEN (or DBT_CLOUD_API_TOKEN), DBT_CLOUD_ACCOUNT_ID, optional DBT_CLOUD_HOST."""
    import json
    import time
    import urllib.request
    host = os.environ.get("DBT_CLOUD_HOST", "cloud.getdbt.com")
    acct = os.environ.get("DBT_CLOUD_ACCOUNT_ID", "")
    context.log.info(f"dbt Cloud job {job_id} on {host}")
    if _dry():
        context.log.info("DRY RUN: dbt Cloud job not triggered")
        return
    token = os.environ.get("DBT_CLOUD_TOKEN") or os.environ.get("DBT_CLOUD_API_TOKEN")
    if not acct or not token:
        raise dg.Failure(description="set DBT_CLOUD_ACCOUNT_ID and DBT_CLOUD_TOKEN")
    headers = {"Authorization": "Token " + token, "Content-Type": "application/json"}
    base = f"https://{host}/api/v2/accounts/{acct}"

    def call(url: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["data"]

    run = call(f"{base}/jobs/{job_id}/run/", {"cause": cause})
    deadline = time.time() + timeout if timeout else None
    while True:
        run = call(f"{base}/runs/{run['id']}/")
        if run["status"] in (10, 20, 30):          # 10 success, 20 error, 30 cancelled
            break
        if deadline and time.time() > deadline:
            raise dg.Failure(description=f"dbt Cloud run {run['id']} still running after {timeout}s")
        time.sleep(10)
    if run["status"] != 10:
        raise dg.Failure(description=f"dbt Cloud run {run['id']} ended with status {run['status']}",
                         metadata={"run_url": run.get("href", ""), "job_id": job_id})
    context.add_output_metadata({"dbt_cloud_run_id": run["id"], "run_url": run.get("href", "")})

'''


COMPONENTS_SOURCE = '''"""Registry components with a dry-run mode, for the migration gate (`runmyjobs-to-dagster validate --execute`).

Each class subclasses the community component and adds `dry_run` (an attribute, or set RMJ_DRY_RUN=1 in the environment). In a dry run the
shell command or SQL is rendered and logged but not executed, so dependencies, pools, retries and parameter substitution can be exercised
without touching a customer system. Without the flag these behave exactly like the registry components.
"""
from __future__ import annotations

import os
import shlex

import dagster as dg
import dagster_community_components as _dcc

# The package exports lazy placeholders: a subclass of one looks itself up by name in the registry and fails. Subclass the real classes.
ShellCommandAssetComponent = _dcc._load_real_class("ShellCommandAssetComponent")
SqlAssetComponent = _dcc._load_real_class("SqlAssetComponent")
TaskChainAssetComponent = _dcc._load_real_class("TaskChainAssetComponent")
EmailSendAssetComponent = _dcc._load_real_class("EmailSendAssetComponent")
SapJobTriggerAssetComponent = _dcc._load_real_class("SapJobTriggerAssetComponent")
try:                                                   # needs dagster-dbt; projects without a dbt Cloud step do not install it
    EnrichedDbtCloudWorkspaceComponent = _dcc._load_real_class("EnrichedDbtCloudWorkspaceComponent")
except Exception:                                      # noqa: BLE001
    EnrichedDbtCloudWorkspaceComponent = None


def _dry(flag: bool) -> bool:
    return flag or os.environ.get("RMJ_DRY_RUN") == "1"


class RmjShellCommandAssetComponent(ShellCommandAssetComponent):
    dry_run: bool = False

    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        if _dry(self.dry_run) and self.command:
            echoed = self.model_copy(update={"command": "echo " + shlex.quote("DRY RUN, not executed: " + self.command)})
            return ShellCommandAssetComponent.build_defs(echoed, context)
        return super().build_defs(context)


def _upstream_dry_run(base):
    """A subclass whose dry_run is also switched on by RMJ_DRY_RUN=1; the dry run itself is the registry component's own."""
    class _Dry(base):
        def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
            if _dry(False) and not self.dry_run:
                return base.build_defs(self.model_copy(update={"dry_run": True}), context)
            return super().build_defs(context)
    return _Dry


class RmjSqlAssetComponent(_upstream_dry_run(SqlAssetComponent)):
    pass


class RmjEmailSendAssetComponent(_upstream_dry_run(EmailSendAssetComponent)):
    pass


class RmjSapJobTriggerAssetComponent(_upstream_dry_run(SapJobTriggerAssetComponent)):
    pass


class RmjTaskChainAssetComponent(TaskChainAssetComponent):
    """A chain of steps as one asset. In a dry run every step is logged ("would run ...") and none executes."""
    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        if _dry(self.dry_run) and not self.dry_run:
            return TaskChainAssetComponent.build_defs(self.model_copy(update={"dry_run": True}), context)
        return super().build_defs(context)


if EnrichedDbtCloudWorkspaceComponent is not None:
    class RmjEnrichedDbtCloudWorkspaceComponent(EnrichedDbtCloudWorkspaceComponent):
        """The dbt Cloud workspace. In a dry run (RMJ_DRY_RUN=1) the executable job assets log the job id and do not trigger the Cloud job."""
        def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
            if _dry(False) and not self.dry_run:
                return EnrichedDbtCloudWorkspaceComponent.build_defs(self.model_copy(update={"dry_run": True}), context)
            return super().build_defs(context)
'''
