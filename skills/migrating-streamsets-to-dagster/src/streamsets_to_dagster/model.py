"""Normalized model of a StreamSets pipeline: everything downstream works on this, never on raw JSON."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Stage:
    instance: str                     # e.g. "JDBCProducer_01"
    cls: str                          # short class, e.g. "JdbcDTarget" (last "_" segment of stageName)
    stage_name: str                   # full stageName
    library: str
    role: str                         # origin | processor | destination | executor | unknown
    inputs: list[str] = field(default_factory=list)      # lanes
    outputs: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)      # event lanes
    conf: dict = field(default_factory=dict)             # flat configuration name -> value (credential literals removed)
    on_error: str = ""
    preconditions: list[str] = field(default_factory=list)
    redacted: int = 0


@dataclass
class Pipeline:
    id: str
    title: str
    description: str = ""
    product: str = "datacollector"                       # datacollector | transformer
    execution_mode: str = ""                             # STANDALONE | CLUSTER_BATCH | CLUSTER_YARN_STREAMING | EDGE | BATCH | STREAMING ...
    constants: dict[str, str] = field(default_factory=dict)       # runtime parameters; credential values removed
    secret_constants: list[str] = field(default_factory=list)     # names whose values were dropped
    stages: list[Stage] = field(default_factory=list)
    error_stage: str = ""
    start_event_stage: str = ""
    stop_event_stage: str = ""
    source: str = ""
    notes: list[str] = field(default_factory=list)
    schedule_hint: dict = field(default_factory=dict)

    def stage(self, instance: str) -> Stage | None:
        return next((s for s in self.stages if s.instance == instance), None)

    def producers(self) -> dict[str, str]:
        """lane -> instance that writes it (data lanes and event lanes)."""
        out = {}
        for s in self.stages:
            for ln in [*s.outputs, *s.events]:
                out[ln] = s.instance
        return out

    def upstream(self, s: Stage) -> list[str]:
        prod = self.producers()
        seen, res = set(), []
        for ln in s.inputs:
            p = prod.get(ln)
            if p and p not in seen:
                seen.add(p)
                res.append(p)
        return res

    def topo(self) -> list[Stage]:
        """Stages in dependency order; raises ValueError on a cycle."""
        by = {s.instance: s for s in self.stages}
        order, state = [], {}

        def visit(n: str):
            if state.get(n) == 2:
                return
            if state.get(n) == 1:
                raise ValueError(f"cycle through {n}")
            state[n] = 1
            for u in self.upstream(by[n]):
                visit(u)
            state[n] = 2
            order.append(by[n])

        for s in self.stages:
            visit(s.instance)
        return order


@dataclass
class Estate:
    pipelines: dict[str, Pipeline] = field(default_factory=dict)      # keyed by unique title
    warnings: list[str] = field(default_factory=list)


def slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return ("p_" + s if s[:1].isdigit() else s) or "pipeline"
