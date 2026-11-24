"""A fake SAP system for the XBP (External Background Processing) job interface, written from SAP's XBP 6.10 interface documentation.

It is strict where the documentation is: it accepts only the documented function names and parameters, and answers BAPI_XBP_JOB_STATUS_GET with
the documented one-character status (P scheduled, S released, Y ready, R active, F finished, A cancelled). A call the real interface does not
have raises, as an RFC would (function module not found). It checks the shape of the conversation, not SAP's behavior.
"""
from __future__ import annotations

import contextlib

DOCUMENTED = {
    "BAPI_XMI_LOGON": {"EXTCOMPANY", "EXTPRODUCT", "INTERFACE", "VERSION"},
    "BAPI_XMI_CLOSE": {"INTERFACE"},
    "BAPI_XBP_JOB_OPEN": {"JOBNAME", "EXTERNAL_USER_NAME", "JOBCLASS"},
    "BAPI_XBP_JOB_ADD_ABAP_STEP": {"JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME", "ABAP_PROGRAM_NAME", "ABAP_VARIANT_NAME", "SAP_USER_NAME", "LANGUAGE"},
    "BAPI_XBP_JOB_CLOSE": {"JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME", "TARGET_SERVER"},
    "BAPI_XBP_JOB_START_IMMEDIATELY": {"JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME", "TARGET_SERVER"},
    "BAPI_XBP_JOB_STATUS_GET": {"JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME"},
    "BAPI_XBP_JOB_JOBLOG_READ": {"JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME"},
}
REQUIRED = {"BAPI_XMI_LOGON": {"EXTCOMPANY", "EXTPRODUCT"}, "BAPI_XBP_JOB_OPEN": {"JOBNAME", "EXTERNAL_USER_NAME"},
            "BAPI_XBP_JOB_ADD_ABAP_STEP": {"JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME", "ABAP_PROGRAM_NAME"},
            "BAPI_XBP_JOB_CLOSE": {"JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME"}, "BAPI_XBP_JOB_START_IMMEDIATELY": {"JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME"},
            "BAPI_XBP_JOB_STATUS_GET": {"JOBNAME", "JOBCOUNT"}}
OK = {"RETURN": {"TYPE": "S"}}


class XbpError(Exception):
    pass


class FakeXbpSystem:
    """`statuses` is what successive polls return, e.g. ["S", "R", "F"]; `log` is the job log lines."""

    def __init__(self, statuses=("S", "R", "F"), log=("job log line",)):
        self.statuses, self.log, self.calls, self.jobs = list(statuses), list(log), [], {}
        self.logged_on = False

    def call(self, name: str, **kw):
        self.calls.append((name, kw))
        if name not in DOCUMENTED:
            raise XbpError(f"function module {name} not found (not in the XBP interface)")
        bad = set(kw) - DOCUMENTED[name]
        if bad:
            raise XbpError(f"{name}: unknown parameter(s) {sorted(bad)}")
        missing = REQUIRED.get(name, set()) - set(kw)
        if missing:
            raise XbpError(f"{name}: missing required parameter(s) {sorted(missing)}")
        if name == "BAPI_XMI_LOGON":
            self.logged_on = True
            return {**OK, "SESSIONID": "S1"}
        if not self.logged_on and name != "BAPI_XMI_CLOSE":
            raise XbpError("not logged on to the XMI interface")
        if name == "BAPI_XBP_JOB_OPEN":
            self.jobs[(kw["JOBNAME"], "00000001")] = "open"
            return {**OK, "JOBCOUNT": "00000001"}
        if name == "BAPI_XBP_JOB_STATUS_GET":
            return {**OK, "STATUS": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]}
        if name == "BAPI_XBP_JOB_JOBLOG_READ":
            return {**OK, "JOB_PROTOCOL": [{"MESSAGE": line} for line in self.log]}
        return OK

    @contextlib.contextmanager
    def connection(self):
        yield self

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]
