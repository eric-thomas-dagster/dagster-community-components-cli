"""Automation-condition analyzer/recommender.

Given a Dagster project directory, invokes the introspection worker inside
that project's venv, then transforms the resulting inventory (assets +
schedules + jobs + sensors) into a proposed
``AutomationConditionApplicatorComponent`` rules block + a human-readable
plan of what to disable / migrate.

Public entry points:

    * ``analyze(project_dir: Path) -> AnalyzerResult``  — programmatic
    * ``main() -> None``                                 — the CLI / uvx entry
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# The worker script lives next to this file. We locate it by absolute path
# rather than importing it — the worker runs inside the TARGET project's venv,
# which may not have dagster_component_cli installed.
_WORKER_MODULE_FILE = str(Path(__file__).parent / "_automation_worker.py")


# ─── Result shapes ─────────────────────────────────────────────────────────


@dataclass
class ProposedRule:
    name: str
    selection: str
    cron: Optional[str] = None
    preset: Optional[str] = None
    derive_from_upstreams: bool = False
    strategy: Optional[str] = None
    reason: str = ""

    def to_yaml_dict(self) -> dict:
        d: dict = {"name": self.name, "selection": self.selection}
        if self.cron:
            d["cron"] = self.cron
        if self.preset:
            d["preset"] = self.preset
        if self.derive_from_upstreams:
            d["derive_from_upstreams"] = True
            if self.strategy:
                d["strategy"] = self.strategy
        return d


@dataclass
class AnalyzerResult:
    rules: list[ProposedRule] = field(default_factory=list)
    schedules_to_disable: list[dict] = field(default_factory=list)
    schedules_to_review: list[dict] = field(default_factory=list)
    jobs_manual_or_sensor: list[dict] = field(default_factory=list)
    unmapped_assets: list[str] = field(default_factory=list)
    partitioned_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Counts for the summary
    counts: dict = field(default_factory=dict)


# ─── Worker invocation ─────────────────────────────────────────────────────


def _run_worker(project_dir: Path) -> dict:
    """Execute the worker script inside the target project's venv."""
    if not project_dir.exists():
        raise FileNotFoundError(f"Project dir not found: {project_dir}")

    # Prefer `uv run` for reproducibility, fall back to invoking the venv python directly.
    worker_path = Path(_WORKER_MODULE_FILE).resolve()
    # Note: `uv run --project <dir>` executes in the target project's venv.
    # Pass the project dir explicitly to the worker so it doesn't rely on cwd.
    cmd = [
        "uv", "run", "--project", str(project_dir),
        "python", str(worker_path), str(project_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        # stdout still may contain a JSON error payload; try to surface both.
        raise RuntimeError(
            f"Worker exited {proc.returncode}. STDERR:\n{proc.stderr}\n\nSTDOUT:\n{proc.stdout}"
        )
    # Worker prints JSON to stdout; strip anything before the first '{'
    text = proc.stdout
    brace = text.find("{")
    if brace < 0:
        raise RuntimeError(f"Worker output has no JSON payload:\n{text}")
    payload = json.loads(text[brace:])
    if "error" in payload:
        raise RuntimeError(f"Worker error: {payload['error']}\n{payload.get('traceback', '')}")
    return payload


# ─── Rule-generation heuristics ────────────────────────────────────────────


def _selection_for_asset_keys(keys: list[str]) -> str:
    """Build a `dg` asset-selection string from a list of concrete keys.

    Uses ``key:<foo> or key:<bar> or ...`` — the selection syntax the
    applicator's Selection engine understands.
    """
    if not keys:
        return ""
    return " or ".join(f'key:"{k}"' for k in keys)


def _group_schedules_by_cron(schedules: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for s in schedules:
        cron = s.get("cron") or ""
        if cron:
            grouped[cron].append(s)
    return grouped


def _asset_keys_for_job(jobs: list[dict], job_name: str) -> list[str]:
    for j in jobs:
        if j.get("name") == job_name:
            return list(j.get("asset_keys") or [])
    return []


def _slugify(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()


def _friendly_cron_name(cron: str) -> str:
    """Turn a cron expression into a human-friendly rule name."""
    parts = cron.split()
    if len(parts) != 5:
        return f"cron_{_slugify(cron)}"
    minute, hour, day, month, dow = parts
    # Common shapes
    if cron == "0 * * * *":
        return "hourly_top_of_hour"
    if cron.startswith("*/") and hour == "*" and day == "*" and month == "*" and dow == "*":
        n = minute[2:]
        return f"every_{n}_minutes"
    if minute == "0" and hour.isdigit() and day == "*" and month == "*" and dow == "*":
        return f"daily_at_{hour}am" if int(hour) < 12 else f"daily_at_{int(hour) - 12 or 12}pm"
    if minute == "0" and hour == "0" and day == "*" and month == "*" and dow.isdigit():
        days = {"0": "sunday", "1": "monday", "2": "tuesday", "3": "wednesday",
                "4": "thursday", "5": "friday", "6": "saturday"}
        return f"weekly_{days.get(dow, dow)}"
    if minute == "0" and hour == "0" and day == "1" and month == "*":
        return "monthly_first"
    return f"cron_{_slugify(cron)}"


def generate_rules(payload: dict) -> AnalyzerResult:
    result = AnalyzerResult()
    assets = payload.get("assets", [])
    schedules = payload.get("schedules", [])
    jobs = payload.get("jobs", [])
    sensors = payload.get("sensors", [])

    result.counts = {
        "assets": len(assets),
        "schedules": len(schedules),
        "jobs": len(jobs),
        "sensors": len(sensors),
        "assets_already_conditioned": sum(1 for a in assets if a.get("has_automation_condition")),
    }

    # ── Track which assets end up covered ────────────────────────────────
    covered: set[str] = set()

    # ── Rule 1..N: one per cron cluster (group schedules that fire the same cron) ─
    for cron, sched_group in sorted(_group_schedules_by_cron(schedules).items()):
        # Collect all asset keys touched by any job in this cron cluster
        all_keys: list[str] = []
        source_names: list[str] = []
        for s in sched_group:
            keys = _asset_keys_for_job(jobs, s.get("job_name", ""))
            all_keys.extend(keys)
            source_names.append(s["name"])
            result.schedules_to_disable.append({
                "name": s["name"],
                "job_name": s.get("job_name"),
                "cron": cron,
                "replaced_by_rule": None,  # filled below
            })
        # Dedup
        all_keys = sorted(set(all_keys))
        if not all_keys:
            # Nothing to attach to — flag for review.
            for s in sched_group:
                result.schedules_to_review.append({
                    "name": s["name"],
                    "cron": cron,
                    "reason": "schedule fires a job with no resolvable asset selection",
                })
            # Roll back the disable entries since we can't replace them
            result.schedules_to_disable = [
                d for d in result.schedules_to_disable if d["name"] not in [s["name"] for s in sched_group]
            ]
            continue

        # Partition awareness: split the covered keys into partitioned vs.
        # not. Partitioned assets can share the same `cron` rule (on_cron
        # fires the LATEST partition per tick for time-window partitions),
        # but STATIC-partitioned assets need a warning — cron doesn't map
        # cleanly to enumerated partitions.
        assets_by_key = {a["key"]: a for a in assets}
        partitioned_static_keys = [
            k for k in all_keys
            if assets_by_key.get(k, {}).get("partitions_def_type") in (
                "StaticPartitionsDefinition",
                "MultiPartitionsDefinition",
                "DynamicPartitionsDefinition",
            )
        ]
        partitioned_time_keys = [
            k for k in all_keys
            if assets_by_key.get(k, {}).get("partitions_def_type") in (
                "DailyPartitionsDefinition",
                "HourlyPartitionsDefinition",
                "MonthlyPartitionsDefinition",
                "WeeklyPartitionsDefinition",
                "TimeWindowPartitionsDefinition",
            )
        ]
        was_partitioned_schedule = any(s.get("is_partitioned") for s in sched_group)

        rule_name = _friendly_cron_name(cron)
        reason_parts = [f"replaces {len(sched_group)} schedule(s): {', '.join(source_names)}"]
        if was_partitioned_schedule:
            reason_parts.append("(source schedules were partition-aware)")
        if partitioned_time_keys:
            reason_parts.append(
                f"time-partitioned assets included ({len(partitioned_time_keys)}) — on_cron "
                "fires the LATEST partition per tick"
            )
        rule = ProposedRule(
            name=rule_name,
            selection=_selection_for_asset_keys(all_keys),
            cron=cron,
            reason=" · ".join(reason_parts),
        )
        result.rules.append(rule)
        covered.update(all_keys)

        # Static/dynamic-partitioned assets need a separate warning
        for k in partitioned_static_keys:
            ptype = assets_by_key[k].get("partitions_def_type", "?")
            result.partitioned_notes.append(
                f"'{k}' ({ptype}): rule '{rule_name}' fires on cron but the asset has "
                f"enumerated partitions — Dagster fires the LATEST partition per tick. "
                f"If you want per-partition backfills, keep the original partitioned schedule "
                f"OR add a per-partition sensor."
            )
        # Backfill the replaced_by_rule field
        for d in result.schedules_to_disable:
            if d["name"] in source_names and d.get("replaced_by_rule") is None:
                d["replaced_by_rule"] = rule_name

    # ── Rule N+1: derive_from_upstreams for LINEAGE downstream assets ────
    #
    # Walk the actual asset graph. Any asset that has at least one in-project
    # upstream dep AND isn't already covered by a cron rule should inherit
    # cadence from its upstreams (strategy=most_frequent handles the multi-
    # upstream case). No group-name assumptions — this works regardless of
    # naming conventions.
    in_project_keys = {a["key"] for a in assets}
    downstream_uncovered = [
        a for a in assets
        if a["key"] not in covered
        and not a.get("has_automation_condition")
        and any(dep in in_project_keys for dep in (a.get("deps") or []))
    ]
    if downstream_uncovered:
        keys = sorted(a["key"] for a in downstream_uncovered)
        result.rules.append(ProposedRule(
            name="derive_downstream_from_upstreams",
            selection=_selection_for_asset_keys(keys),
            derive_from_upstreams=True,
            strategy="most_frequent",
            reason=(
                f"{len(keys)} lineage-downstream asset(s) (each has at least one in-project "
                f"upstream) inherit cadence from those upstreams — strategy=most_frequent picks "
                f"the shortest cadence when upstreams differ"
            ),
        ))
        covered.update(keys)

    # ── Roots that aren't scheduled — flag for user review ───────────────
    #
    # Assets with no in-project upstream deps AND not covered by any schedule
    # are typically SOURCE assets (external ingestion, manually-triggered).
    # Emitting eager on them is usually wrong (would try to materialize on any
    # tick). Warn the user instead of guessing.
    root_uncovered = [
        a for a in assets
        if a["key"] not in covered
        and not a.get("has_automation_condition")
        and not any(dep in in_project_keys for dep in (a.get("deps") or []))
    ]
    if root_uncovered:
        result.warnings.append(
            f"{len(root_uncovered)} root asset(s) have no upstream deps and no schedule: "
            f"{', '.join(a['key'] for a in root_uncovered[:5])}"
            f"{'...' if len(root_uncovered) > 5 else ''}. These are typically external-source "
            f"or sensor-triggered — decide manually whether to add a cron / eager / leave "
            f"un-conditioned."
        )
        # Don't add them to `covered` — they intentionally fall through to whatever
        # the user configures (they will otherwise hit the catchall below).

    # ── Rule N+2: eager catchall for anything else ───────────────────────
    unconditioned = [
        a for a in assets
        if not a.get("has_automation_condition") and a["key"] not in covered
    ]
    if unconditioned:
        result.rules.append(ProposedRule(
            name="eager_default",
            selection="*",
            preset="eager",
            reason=f"catchall — {len(unconditioned)} asset(s) not covered by explicit rules; eager materializes them when their upstreams land",
        ))

    # ── Jobs / sensors: identify what to keep manual ─────────────────────
    scheduled_job_names = {s.get("job_name") for s in schedules if s.get("job_name")}
    sensor_triggered_jobs = {j for s in sensors for j in (s.get("job_names") or [])}
    for j in jobs:
        if j.get("name") not in scheduled_job_names:
            triggered_by = "sensor" if j.get("name") in sensor_triggered_jobs else "manual"
            result.jobs_manual_or_sensor.append({
                "name": j["name"],
                "trigger": triggered_by,
                "asset_selection": j.get("asset_selection_str"),
            })

    # ── Unmapped assets warning ──────────────────────────────────────────
    covered_by_catchall = any(r.selection == "*" for r in result.rules)
    for a in assets:
        if a["key"] in covered or covered_by_catchall or a.get("has_automation_condition"):
            continue
        result.unmapped_assets.append(a["key"])

    # ── Already-conditioned assets warning ──────────────────────────────
    already = [a for a in assets if a.get("has_automation_condition")]
    if already:
        result.warnings.append(
            f"{len(already)} asset(s) already have an automation_condition set. "
            f"Rules use preserve_existing: true by default — those stay put."
        )

    return result


# ─── YAML + report rendering ───────────────────────────────────────────────


def render_yaml(result: AnalyzerResult) -> str:
    lines: list[str] = [
        "# Proposed AutomationConditionApplicatorComponent rules",
        "# Generated by dagster-community-components analyze-schedules",
        f"# Analyzed: {result.counts.get('assets', 0)} assets, "
        f"{result.counts.get('schedules', 0)} schedules, "
        f"{result.counts.get('jobs', 0)} jobs, "
        f"{result.counts.get('sensors', 0)} sensors",
        "# Reviewed by: <you>",
        "#",
        "# Apply this via apply_rules() in your definitions.py — see",
        "# https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/infrastructure/automation_condition_applicator/README.md",
        "",
        "type: dagster_community_components.AutomationConditionApplicatorComponent",
        "attributes:",
        "  preserve_existing: true",
        "  rules:",
    ]
    if not result.rules:
        lines.append("    []  # no rules generated — likely because the project had no schedules")
    for r in result.rules:
        d = r.to_yaml_dict()
        lines.append(f"    # {r.reason}" if r.reason else "")
        lines.append(f"    - name: {d['name']}")
        lines.append(f"      selection: {d['selection']!r}")
        if "cron" in d:
            lines.append(f"      cron: {d['cron']!r}")
        if "preset" in d:
            lines.append(f"      preset: {d['preset']}")
        if "derive_from_upstreams" in d:
            lines.append(f"      derive_from_upstreams: true")
            if "strategy" in d:
                lines.append(f"      strategy: {d['strategy']}")
        lines.append("")
    return "\n".join(lines)


def render_report(result: AnalyzerResult) -> str:
    lines: list[str] = []
    c = result.counts
    lines.append("═" * 70)
    lines.append("  Automation Condition Analyzer — recommendation")
    lines.append("═" * 70)
    lines.append("")
    lines.append(
        f"Inventory:  {c.get('assets', 0)} assets · {c.get('schedules', 0)} schedules · "
        f"{c.get('jobs', 0)} jobs · {c.get('sensors', 0)} sensors "
        f"({c.get('assets_already_conditioned', 0)} assets already have automation_condition set)"
    )
    lines.append("")
    lines.append(f"✓ Proposed rules ({len(result.rules)}):")
    for r in result.rules:
        lines.append(f"    • {r.name}  ({r.selection})")
        if r.reason:
            lines.append(f"        {r.reason}")
    lines.append("")

    lines.append(f"✓ Schedules to disable AFTER applying the rules ({len(result.schedules_to_disable)}):")
    for s in result.schedules_to_disable:
        lines.append(
            f"    • {s['name']}  (job={s.get('job_name')}, cron={s.get('cron')}) "
            f"→ covered by '{s.get('replaced_by_rule')}'"
        )
    lines.append("")

    if result.schedules_to_review:
        lines.append(f"⚠  Schedules that need manual review ({len(result.schedules_to_review)}):")
        for s in result.schedules_to_review:
            lines.append(f"    • {s['name']}  (cron={s.get('cron')})")
            lines.append(f"        reason: {s.get('reason')}")
        lines.append("")

    if result.jobs_manual_or_sensor:
        lines.append(f"✓ Jobs to keep manual/sensor-only ({len(result.jobs_manual_or_sensor)}):")
        for j in result.jobs_manual_or_sensor:
            lines.append(f"    • {j['name']}  (trigger={j.get('trigger')})")
        lines.append("")

    if result.unmapped_assets:
        lines.append(f"⚠  Assets not covered by any rule ({len(result.unmapped_assets)}):")
        for k in result.unmapped_assets[:20]:
            lines.append(f"    • {k}")
        if len(result.unmapped_assets) > 20:
            lines.append(f"    … and {len(result.unmapped_assets) - 20} more")
        lines.append("")

    if result.partitioned_notes:
        lines.append(f"⚠  Partition-aware notes ({len(result.partitioned_notes)}):")
        for n in result.partitioned_notes[:10]:
            lines.append(f"    • {n}")
        if len(result.partitioned_notes) > 10:
            lines.append(f"    … and {len(result.partitioned_notes) - 10} more")
        lines.append("")

    for w in result.warnings:
        lines.append(f"NOTE: {w}")

    return "\n".join(lines)


# ─── Public API ────────────────────────────────────────────────────────────


def analyze(project_dir: Path) -> AnalyzerResult:
    payload = _run_worker(project_dir)
    return generate_rules(payload)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze a Dagster project's existing schedules + jobs and emit "
            "recommended AutomationConditionApplicatorComponent rules."
        )
    )
    parser.add_argument(
        "--project-dir", "-p", default=".",
        help="Path to the Dagster project (default: current dir)",
    )
    parser.add_argument(
        "--output", "-o", default="automation_conditions_proposal.yaml",
        help="Output YAML path (default: automation_conditions_proposal.yaml in the project)",
    )
    parser.add_argument(
        "--stdout", action="store_true",
        help="Print YAML to stdout instead of writing a file",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    try:
        result = analyze(project_dir)
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        return 1

    yaml_text = render_yaml(result)
    if args.stdout:
        print(yaml_text)
    else:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = project_dir / out_path
        out_path.write_text(yaml_text)
        print(f"[wrote] {out_path}")

    print(render_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
