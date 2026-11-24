"""Normalized model of an Azure Data Factory: everything downstream works on this, never on raw JSON files."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Activity:
    name: str
    type: str
    depends_on: list[tuple[str, list[str]]] = field(default_factory=list)   # (activity, [Succeeded|Failed|Skipped|Completed])
    props: Any = field(default_factory=dict)       # typeProperties (a str when an ARM deploy-time parameter could not be resolved)
    policy: dict = field(default_factory=dict)
    description: str = ""
    linked_service: str = ""                       # activity-level linkedServiceName reference
    inputs: list[dict] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)


@dataclass
class Pipeline:
    name: str
    parameters: dict[str, dict] = field(default_factory=dict)   # name -> {type, defaultValue}
    variables: dict[str, dict] = field(default_factory=dict)
    activities: list[Activity] = field(default_factory=list)
    folder: str = ""
    description: str = ""
    concurrency: int | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Trigger:
    name: str
    type: str                                      # ScheduleTrigger | TumblingWindowTrigger | BlobEventsTrigger | ...
    state: str = ""                                # runtimeState: Started | Stopped
    pipelines: list[dict] = field(default_factory=list)   # [{name, parameters}]
    props: dict = field(default_factory=dict)


@dataclass
class LinkedService:
    name: str
    type: str
    props: dict = field(default_factory=dict)      # typeProperties with every secret removed
    connect_via: str = ""                          # integration runtime reference (self-hosted ones reach private networks)
    redacted: int = 0                              # how many secret values were dropped


@dataclass
class Dataset:
    name: str
    type: str
    linked_service: str = ""
    props: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)


@dataclass
class Factory:
    pipelines: dict[str, Pipeline] = field(default_factory=dict)
    triggers: dict[str, Trigger] = field(default_factory=dict)
    linked_services: dict[str, LinkedService] = field(default_factory=dict)
    datasets: dict[str, Dataset] = field(default_factory=dict)
    data_flows: dict[str, str] = field(default_factory=dict)     # name -> type
    data_flow_defs: dict[str, dict] = field(default_factory=dict)   # name -> typeProperties (sources, sinks, script)
    warnings: list[str] = field(default_factory=list)


def parse_activity(d: dict) -> Activity:
    deps = []
    for dep in d.get("dependsOn") or []:
        if isinstance(dep, dict) and dep.get("activity"):
            deps.append((dep["activity"], list(dep.get("dependencyConditions") or ["Succeeded"])))
    ls = d.get("linkedServiceName") or {}
    return Activity(
        name=d.get("name", ""), type=d.get("type", ""), depends_on=deps, props=d.get("typeProperties", {}),
        policy=d.get("policy") if isinstance(d.get("policy"), dict) else {}, description=d.get("description", ""),
        linked_service=ls.get("referenceName", "") if isinstance(ls, dict) else "",
        inputs=[x for x in d.get("inputs") or [] if isinstance(x, dict)],
        outputs=[x for x in d.get("outputs") or [] if isinstance(x, dict)],
    )


def parse_activities(lst) -> list[Activity]:
    return [parse_activity(a) for a in lst if isinstance(a, dict)] if isinstance(lst, list) else []
