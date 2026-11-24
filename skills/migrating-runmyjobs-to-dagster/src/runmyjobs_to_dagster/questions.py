"""Questions only the customer can answer, per chain, derived from what the export contains and does not contain.

Anything the export holds is converted; anything it cannot hold (business keys, owners, connection details, where an event is raised) is asked here, so
MIGRATION.md ends with a list a person can work through chain by chain.
"""
from __future__ import annotations

import re

from .model import Chain, Model

_KEYLIKE = re.compile(r"(date|(^|_)dt($|_)|day|period|month|year|week|region|plant|site|store|company|bukrs|werks|fiscal|batch|run_?id)", re.I)


def _calls(chain: Chain, model: Model, seen: tuple[str, ...] = ()):
    """Every call of a chain, including the calls of chains it runs (each chain once)."""
    for st in chain.steps:
        for c in st.calls:
            yield c
            if c.process in model.chains and c.process not in seen and c.process != chain.name:
                yield from _calls(model.chains[c.process], model, seen + (chain.name,))


def chain_questions(model: Model) -> dict[str, list[str]]:
    scheduled = {s.target for s in model.schedules} | {f.target for f in model.file_events}
    out: dict[str, list[str]] = {}
    for chain in model.chains.values():
        if chain.inline:
            continue
        q: list[str] = []
        calls = list(_calls(chain, model))
        kinds = {c.process: (model.processes[c.process].type.lower() if c.process in model.processes else "missing") for c in calls if c.process not in model.chains}
        params = list(chain.parameters)
        for c in calls:
            if c.process in model.chains:
                params += model.chains[c.process].parameters
        unresolved = sorted({p.name for p in params if p.default is None and p.direction in ("in", "in_out")})
        if unresolved:
            q.append(f"What does the caller pass for {', '.join(unresolved)}? (no value in the export; the Python output takes them as run config at launch)")
        keys = sorted({p.name for p in params if _KEYLIKE.search(p.name)})
        if keys:
            q.append(f"Is {', '.join(keys)} the business key this chain runs for (a date, period, region or plant)? If so it can become a partition, so you can backfill and see which keys ran; if not, leave it as a parameter")
        if chain.name not in scheduled:
            q.append("When does this chain run? The export has no submit frame or file event for it (it may be started by a person, another chain or an event)")
        else:
            q.append("Confirm the schedule against what actually runs today (the Q1 run history is the check)")
        windows = sorted({(c.precondition or {}).get("time_window") for c in calls if (c.precondition or {}).get("time_window")})
        if windows or any(c.precondition for c in calls) or chain.precondition:
            q.append(f"Which hours and days does time window {', '.join(windows) or 'the precondition'} allow? The definition is not in the export, so the condition is reported but not enforced")
        events = sorted({w["event"] for c in calls for w in c.wait_events} | {n.split("'")[1] for n in chain.notes if "WaitEvent" in n and "'" in n})
        if events or any("WaitEvent" in n or "wait" in n.lower() for n in chain.notes):
            q.append(f"What raises event {', '.join(events) or '(the chain start event)'}, and should it be cleared after use? Dagster needs the producer (a sensor on a file or asset, or another chain) to model it")
        if "sap" in kinds.values():
            q.append("Which SAP system and client do these jobs run on, and which RFC user may start background jobs? Is there a test system to run them against?")
        if "mail" in kinds.values():
            q.append("Who receives the emails the chain sends, from which SMTP server and sender? Do any need the attachment (the upstream log)?")
        sql_conns = sorted({model.processes[p].connection or model.processes[p].queue or "default" for p, k in kinds.items() if k == "sql"})
        if sql_conns:
            q.append(f"Which database does each SQL connection point to ({', '.join(sql_conns)}), and how are its credentials supplied?")
        if any(re.search(r"dbt|hvr", (c.process + " " + str(model.processes[c.process].command if c.process in model.processes else "")), re.I) for c in calls):
            q.append("For the dbt Cloud and HVR steps: which Cloud job ids (or HVR channels) are they, and should a dbt job keep its per-model assets?")
        if "missing" in kinds.values():
            q.append(f"Export the definitions this chain calls that are not in the file: {', '.join(sorted(p for p, k in kinds.items() if k == 'missing'))} (or tell us they are built in)")
        q.append("Who owns this chain, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known")
        out[chain.name] = q
    return out


def render_questions(questions: dict[str, list[str]]) -> list[str]:
    if not questions:
        return []
    lines = ["", "## Needs your input", "",
             "The export cannot answer these; each answer changes what the converter generates. Work through them chain by chain.", ""]
    for chain, qs in sorted(questions.items()):
        lines += [f"### {chain}", ""] + [f"- [ ] {x}" for x in qs] + [""]
    return lines
