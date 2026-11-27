#!/usr/bin/env python3
"""Aggregate gates and behaviour verdicts into `reference/rewrite-route-evaluation.md` (generated: every number comes from the result files, none is written by hand).

    python3 eval_report.py --work DIR --runs stage1 [stage2 ...] --writers cold|designer [--costs costs.json] [--variance-run stage1b] [--manifest corpus/manifest.json] [--baselines corpus/baselines.json] [--out reference/rewrite-route-evaluation.md]

--writers says who wrote the rewrites and is required: `cold` means fresh agents that had not seen the skill's internals and received only the kit (`eval_kit.py check` guards that);
`designer` means the agent that designed the prompts, and the report then says its numbers are NOT cold. The report states this next to every table.

costs.json (optional): {"<dag_id>": {"tokens": 123456, "tool_uses": 41, "duration_s": 380}, ...} (the key may also be the folder name or `<repo>::<dag_id>`).
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any

import eval_common as C

sys.path.insert(0, str(C.HERE / "parity"))
import behavior_compare as B  # noqa: E402

VERDICTS = ["equivalent", "equivalent-with-differences", "matches-baseline-failure", "stub-matches-failure", "different", "not-comparable", "no-output", "baseline-failed"]
GOOD_STRICT = {"equivalent"}
GOOD_LENIENT = {"equivalent", "equivalent-with-differences", "matches-baseline-failure"}
DIMS = ["outcome", "tasks", "values", "files", "sqlite", "http", "smtp", "s3", "stdout", "failure", "schedule", "retries", "pools", "dependencies", "job"]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def pct(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.0f}% (95% CI {100 * lo:.0f}-{100 * hi:.0f}%)"


def sign_test(b: int, c: int) -> float:
    """two-sided exact binomial (p=0.5) on the discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def gather(work: Path, run: str) -> dict[str, dict[str, Any]]:
    priv = work / f"{run}-private"
    index = json.loads((priv / "kit_index.json").read_text())
    out: dict[str, dict[str, Any]] = {}
    for key, v in index.items():
        d = v["dir"]
        rec: dict[str, Any] = {"key": key, "dir": d, "run": run, **{k: v[k] for k in ("dag_id", "stratum", "pattern", "repo", "project")}, "arm": v.get("arm"), "group": v.get("group"), "behavior": None, "gates": None, "report": None}
        for name, path in (("behavior", priv / "behavior" / d / "behavior.json"), ("gates", priv / "gates" / f"{d}.gates.json"), ("report", priv / "gates" / f"{d}.report.json")):
            if path.exists():
                rec[name] = json.loads(path.read_text())
        # a route whose failing step is a loud converter stub that "matches" a failing baseline is reported as its own verdict, never as a success
        rec["verdicts"] = {r: x["rollup"].get("detail") or x["rollup"]["verdict"] for r, x in (rec["behavior"] or {}).get("routes", {}).items()} if rec["behavior"] else {}
        if rec["behavior"] and rec["behavior"].get("stratum"):               # the stratum of the CURRENT converter report (behavior.json), not the one the manifest was built with
            rec["manifest_stratum"], rec["stratum"] = rec["stratum"], rec["behavior"]["stratum"]
        rec["baseline_failed"] = bool(rec["behavior"]) and rec["behavior"]["baseline"]["class"][0] == "baseline-failed"
        out[key] = rec
    return out


def nondeterministic_rows(recs: list[dict[str, Any]], route: str = "rewrite") -> list[tuple[str, str]]:
    """(dag id, reason) for each DAG whose task-state comparison was `nondeterministic: not compared` (a branch that reads the clock or a random number): outcome and failure behaviour were still compared."""
    out = []
    for r in recs:
        x = ((r.get("behavior") or {}).get("routes", {}).get(route) or {}).get("dimensions", {}).get("tasks") or {}
        nd = x.get("nondeterministic")
        if nd:
            out.append((r["dag_id"], "; ".join(sorted(set(nd["nondeterministic_decisions"].values())))))
    return out


def counts(recs: list[dict[str, Any]], route: str) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for r in recs:
        if not r["behavior"]:
            c["not run"] += 1
        elif r["baseline_failed"]:
            c["baseline-failed"] += 1
        else:
            v = r["verdicts"].get(route, "not run")
            c[v] += 1
            if v == "equivalent":                                              # strict `equivalent` split: a value, file, row, request, mail, object or printed line was compared, or only outcome and structure were
                c["equivalent:thin" if thin(r, route) else "equivalent:substantive"] += 1
    return c


def thin(r: dict[str, Any], route: str) -> bool:
    return bool((((r.get("behavior") or {}).get("routes") or {}).get(route) or {}).get("rollup", {}).get("thin"))


def route_x(r: dict[str, Any], route: str) -> dict[str, Any]:
    return ((r.get("behavior") or {}).get("routes") or {}).get(route) or {}


def unobserved_of(x: dict[str, Any]) -> dict[str, str]:
    """what a route's `equivalent` did not look at (from the stored dimensions, so it also works on older results): dimension -> the harness's reason."""
    dims = x.get("dimensions") or {}
    return {n: str(dims[n].get("reason") or "not compared") for n in (*B.SUBSTANTIVE, "failure") if n in dims and dims[n].get("verdict") == B.NOT}


def short_reason(n: str, why: str) -> str:
    w = re.sub(r"^(no value to compare: )", "", why)
    return f"{n} ({w[:70]})" if n in ("values", "stdout") else n


def strict_unobserved_lines(recs: list[dict[str, Any]], route: str) -> list[str]:
    """For the DAGs judged strictly `equivalent`: how many are thin and, per DAG, what was not observable, so that `strict` cannot be read as `values, files, requests and output were compared`."""
    strict = [r for r in recs if r["behavior"] and not r["baseline_failed"] and r["verdicts"].get(route) == "equivalent"]
    if not strict:
        return []
    thin_rows = [r for r in strict if thin(r, route)]
    L = [f"### `{route}`: what the {len(strict)} strict `equivalent` DAGs did not observe", "",
         f"**{len(thin_rows)} of {len(strict)} are thin** (nothing was compared but the outcome, the task states and the structure); {len(strict) - len(thin_rows)} compared at least one value, file, row, request, mail, object, printed line, user-function call or command. "
         "A thin `equivalent` says: it ran, the tasks ended in the same states, the metadata matches. It does not say the data or output matched.", ""]
    per = collections.Counter(n for r in thin_rows for n in unobserved_of(route_x(r, route)))
    if per:
        L += ["Not observable, over the thin DAGs: " + ", ".join(f"{n} {k}" for n, k in sorted(per.items(), key=lambda kv: -kv[1])) + ".", ""]
    if thin_rows:
        L += ["| thin strict DAG | not observable |", "|---|---|"]
        for r in sorted(thin_rows, key=lambda r: r["dag_id"]):
            u = unobserved_of(route_x(r, route))
            L.append(f"| `{r['dag_id']}` | {'; '.join(short_reason(n, w) for n, w in u.items()) or 'everything beyond outcome and structure'} |")
        L.append("")
    return L


def failed_by_design_lines(recs: list[dict[str, Any]], route: str) -> list[str]:
    """The DAGs whose own code fails in Airflow and whose route fails the same way (`matches-baseline-failure`), kept apart from the successes, with whether the two failures share a cause; and, apart again, the ones where the route's failing step is a converter stub (`stub-matches-failure`: never a success)."""
    mb = [r for r in recs if r["behavior"] and not r["baseline_failed"] and r["verdicts"].get(route) == "matches-baseline-failure"]
    st = [r for r in recs if r["behavior"] and not r["baseline_failed"] and r["verdicts"].get(route) == "stub-matches-failure"]
    if not mb and not st:
        return []
    L = [f"### `{route}`", ""]
    if mb:
        cause = {r["key"]: (route_x(r, route).get("rollup") or {}).get("cause") or {} for r in mb}
        n_same = sum(1 for c in cause.values() if c.get("same") is True)
        n_diff = sum(1 for c in cause.values() if c.get("same") is False)
        L += [f"**Failed by design in Airflow, the route fails the same way: {len(mb)}.** Neither a success nor a failure of the route: the DAG's own code fails. Failure causes: same {n_same}, different {n_diff} (agreement is coincidental), not told {len(mb) - n_same - n_diff}.", "",
              "| DAG | thin | cause | Airflow failure | route failure |", "|---|---|---|---|---|"]
        for r in sorted(mb, key=lambda r: r["dag_id"]):
            c = cause[r["key"]]
            tag = {True: "same", False: "different"}.get(c.get("same"), "not told")
            L.append(f"| `{r['dag_id']}` | {'thin' if thin(r, route) else 'substantive'} | {tag} | {', '.join(c.get('baseline') or []) or '-'} | {', '.join(c.get('route') or []) or '-'} |")
        L.append("")
    if st:
        L += [f"**Stub fails the same way: {len(st)}** (`stub-matches-failure`). The route's failing step is a converter stub or an operation the converter does not mirror, so nothing was converted. Never counted as a success and never in the group above:", ""]
        L += [f"- `{r['dag_id']}`: {(route_x(r, route).get('rollup') or {}).get('why', '')[:200]}" for r in sorted(st, key=lambda r: r["dag_id"])]
        L.append("")
    return L


def route_environment_lines(recs: list[dict[str, Any]], route: str) -> list[str]:
    """Routes that failed on a module their environment does not have: an install is needed, whatever the converter did."""
    rows = [(r["dag_id"], (route_x(r, route).get("rollup") or {}).get("route_missing_modules")) for r in recs if r["behavior"] and (route_x(r, route).get("rollup") or {}).get("route_missing_modules")]
    if not rows:
        return []
    mods = sorted({m for _, ms in rows for m in ms})
    return [f"### `{route}`: the route's environment lacks a package", "", f"{len(rows)} DAG(s) failed in the route on a missing module (install: {', '.join(mods)}). Their verdict is not moved by this; re-measure after the install.", ""] + [f"- `{d}`: {', '.join(ms)}" for d, ms in sorted(rows)] + [""]


def route_table(recs: list[dict[str, Any]], route: str) -> list[str]:
    c = counts(recs, route)
    n_eval = len(recs) - c["baseline-failed"] - c["not run"]
    rows = ["| verdict | DAGs | share of evaluated DAGs |", "|---|---|---|"]
    for v in VERDICTS[:-1]:
        rows.append(f"| {v} | {c[v]} | {pct(c[v], n_eval)} |")
        if v == "equivalent":
            rows.append(f"| &nbsp;&nbsp;of which **with a substantive comparison** (a value, file, row, request, mail, object or printed line was compared) | {c['equivalent:substantive']} | {pct(c['equivalent:substantive'], n_eval)} |")
            rows.append(f"| &nbsp;&nbsp;of which **thin** (only outcome, task states and structure were observable) | {c['equivalent:thin']} | {pct(c['equivalent:thin'], n_eval)} |")
    rows.append(f"| equivalent or equivalent-with-differences (or matches a by-design baseline failure) | {sum(c[v] for v in GOOD_LENIENT)} | {pct(sum(c[v] for v in GOOD_LENIENT), n_eval)} |")
    rows.append(f"| baseline-failed (excluded from the shares) | {c['baseline-failed']} | |")
    if c["not run"]:
        rows.append(f"| **not run** (no behaviour result yet) | {c['not run']} | |")
    return rows + ["", f"Evaluated DAGs (sampled minus baseline-failed minus not run): **{n_eval}** of {len(recs)}."]


def split_table(recs: list[dict[str, Any]], route: str, field: str) -> list[str]:
    vals = sorted({r[field] for r in recs})
    rows = [f"| {field} | evaluated | equivalent | of which substantive | of which thin | equivalent-with-differences | different | no-output | not-comparable / matches-baseline-failure / stub-matches-failure |", "|---|---|---|---|---|---|---|---|---|"]
    for val in vals:
        sub = [r for r in recs if r[field] == val]
        c = counts(sub, route)
        n = len(sub) - c["baseline-failed"] - c["not run"]
        rows.append(f"| {val} | {n} | {pct(c['equivalent'], n)} | {c['equivalent:substantive']} | {c['equivalent:thin']} | {c['equivalent-with-differences']} | {c['different']} | {c['no-output']} | {c['not-comparable'] + c['matches-baseline-failure'] + c['stub-matches-failure']} |")
    return rows


def evidence_text(x: dict[str, Any], limit: int = 420) -> str:
    out = []
    for n, d in (x.get("dimensions") or {}).items():
        if d["verdict"] == "differ":
            out.append(f"**{n}**: {json.dumps(d.get('evidence'), default=str)[:limit]}")
    return "; ".join(out) or x["rollup"].get("why", "")


def med(xs: list[float]) -> str:
    return f"{statistics.mean(xs):.1f} / {statistics.median(xs):.1f}" if xs else "n/a"


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """two-sided Fisher exact test on the 2x2 table [[a, b], [c, d]] (sum of the probabilities of every table as or less likely than the observed one)."""
    n1, n2, k = a + b, c + d, a + c
    n = n1 + n2

    def p(x: int) -> float:
        return math.comb(n1, x) * math.comb(n2, k - x) / math.comb(n, k)
    lo, hi = max(0, k - n2), min(k, n1)
    obs = p(a)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= obs * (1 + 1e-9)))


def diff_ci(k1: int, n1: int, k2: int, n2: int, z: float = 1.96) -> tuple[float, float, float]:
    """difference of two proportions (arm A minus arm B) with Newcombe's hybrid-score 95% interval (built from the two Wilson intervals)."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p1 - p2
    return d, d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)


def evaluated(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in recs if r["behavior"] and not r["baseline_failed"]]


def arm_comparison(recs: list[dict[str, Any]]) -> list[str]:
    """ARM A (behaviour-feedback tool) against ARM B (none): the strata balance, then the difference in the share of strictly equivalent rewrites with its interval and Fisher's exact test. The arms are DIFFERENT DAGs."""
    L: list[str] = []
    arms = ["A", "B"]
    L += ["## Arm A against arm B: does the feedback tool change the result?", "",
          "**This is a between-DAG comparison.** Each DAG was given to ONE arm (assigned at random, stratified by the converter's stratum and by pattern), so the two arms are different DAGs, not the same DAG written twice: a "
          "difference can come from the tool, from which DAGs fell into which arm, or from noise between workers. The balance of the arms over the strata is shown first; the interval is Newcombe's 95% interval for the "
          "difference of two proportions and the test is Fisher's exact test (two-sided).", "",
          "| | " + " | ".join(f"arm {x}" for x in arms) + " |", "|---|---|---|"]
    for title, field in (("stratum", "stratum"), ("pattern", "pattern")):
        for v in sorted({r[field] for r in recs}):
            L.append(f"| {title} `{v}` | " + " | ".join(str(sum(1 for r in recs if r.get("arm") == x and r[field] == v)) for x in arms) + " |")
    L.append("| **sampled DAGs** | " + " | ".join(str(sum(1 for r in recs if r.get("arm") == x)) for x in arms) + " |")
    L.append("| evaluated (baseline ran, behaviour compared) | " + " | ".join(str(len([r for r in evaluated(recs) if r.get("arm") == x])) for x in arms) + " |")
    L.append("")
    L += ["| measure | arm A | arm B | A minus B (95% CI) | Fisher exact p |", "|---|---|---|---|---|"]
    for name, good, subst in (("strictly equivalent", GOOD_STRICT, False), ("strictly equivalent with a substantive comparison", GOOD_STRICT, True), ("equivalent, lenient (with differences or matching a by-design failure)", GOOD_LENIENT, False)):
        k, n = {}, {}
        for x in arms:
            sub = [r for r in evaluated(recs) if r.get("arm") == x]
            n[x] = len(sub)
            k[x] = sum(1 for r in sub if r["verdicts"].get("rewrite") in good and not (subst and thin(r, "rewrite")))
        if min(n.values()) == 0:
            L.append(f"| {name} | {k['A']}/{n['A']} | {k['B']}/{n['B']} | n/a | n/a |")
            continue
        d, lo, hi = diff_ci(k["A"], n["A"], k["B"], n["B"])
        L.append(f"| {name} | {pct(k['A'], n['A'])} | {pct(k['B'], n['B'])} | {100 * d:+.0f} points ({100 * lo:+.0f} to {100 * hi:+.0f}) | {fisher_exact(k['A'], n['A'] - k['A'], k['B'], n['B'] - k['B']):.3f} |")
    L += ["", "With groups of about 30 DAGs per arm the interval is wide: a difference smaller than about 25 points cannot be told apart from noise here. Read an interval that contains 0 as `not shown`, not as `no effect`.", ""]
    return L


def components_used(r: dict[str, Any]) -> list[str]:
    return list(((r.get("report") or {}).get("components") or {}).get("registry") or [])


def registry_first(recs: list[dict[str, Any]]) -> list[str]:
    """Is `registry first` measurable? Per rewrite: did the project USE a registry component (read from its defs.yaml `type:` entries and the `.dg-community.json` marker `dagster-component add` writes), and did the worker's
    report say registry searches were run (parsed from WORKER_REPORT.md)."""
    rows = [r for r in recs if r.get("report") and "components" in (r["report"] or {})]
    L = ["## Registry first: component use and searches", ""]
    if not rows:
        return L + ["No gate run recorded which components the rewrites use (`eval_gates.py` writes it), so this is **not measured** for this run.", ""]
    arms = sorted({r["arm"] for r in rows if r.get("arm")}) or [None]
    L += ["Per rewrite: **uses a registry component** = a `defs.yaml` under `defs/` names a component installed from the community registry (read from the project by the harness, whatever the worker reported); "
          "**searches reported** = the report's `What you searched` section says registry searches were run (the worker's own statement, parsed, not verified); **component style** = at least one component folder.", "",
          "| " + ("arm | " if arms != [None] else "") + "rewrites | use a registry component | component-style projects | report says searches were run | searched but used no component | no search reported and no component |", "|" + ("---|" if arms != [None] else "") + "---|---|---|---|---|---|"]
    for arm in arms:
        sub = [r for r in rows if arm is None or r.get("arm") == arm]
        use = [r for r in sub if components_used(r)]
        srch = [r for r in sub if r["report"].get("searches_reported")]
        L.append(f"| " + (f"{arm} | " if arm else "") + f"{len(sub)} | {pct(len(use), len(sub))} | {sum(1 for r in sub if r['report'].get('style') == 'component')} | {pct(len(srch), len(sub))} | "
                 f"{sum(1 for r in srch if not components_used(r))} | {sum(1 for r in sub if not r['report'].get('searches_reported') and not components_used(r))} |")
    cc: collections.Counter = collections.Counter(c for r in rows for c in components_used(r))
    if cc:
        L += ["", "Registry components used (DAGs using each): " + ", ".join(f"`{c}` {n}" for c, n in cc.most_common()) + "."]
    other = collections.Counter(c for r in rows for c in ((r["report"].get("components") or {}).get("other") or []))
    if other:
        L += ["", "Other component types used (official integrations and anything else): " + ", ".join(f"`{c}` {n}" for c, n in other.most_common()) + "."]
    claimed = sum(1 for r in rows if r["report"].get("registry_components") and not components_used(r))
    L += ["", f"{claimed} report(s) list a registry component in the `Registry components used` table although the project's `defs.yaml` files use none (a hand-written asset that follows a component's idea, or a component only read): the harness counts what the project uses, not what the report claims.", ""]
    return L


def baseline_nondeterministic(recs: list[dict[str, Any]], route: str = "rewrite") -> list[tuple[str, list[str]]]:
    """(dag id, tasks) for each DAG with a task whose Airflow state differed between the recorded baseline runs: `nondeterministic: not compared`, the rest of the DAG still compared."""
    out = []
    for r in recs:
        ev = ((((r.get("behavior") or {}).get("routes") or {}).get(route) or {}).get("dimensions") or {}).get("tasks", {}).get("evidence") or {}
        if ev.get("nondeterministic_baseline"):
            out.append((r["dag_id"], sorted(ev["nondeterministic_baseline"])))
    return out


def render(a: argparse.Namespace) -> str:
    work = a.work.resolve()
    recs: list[dict[str, Any]] = []
    for run in a.runs:
        recs += list(gather(work, run).values())
    seen = collections.Counter(r["key"] for r in recs)
    if any(v > 1 for v in seen.values()):
        raise SystemExit("a DAG appears in more than one run: " + ", ".join(k for k, v in seen.items() if v > 1))
    manifest = C.load_manifest(a.manifest) if a.manifest and Path(a.manifest).exists() else None
    costs = json.loads(a.costs.read_text()) if a.costs and a.costs.exists() else {}
    cold = a.writers == "cold"
    who = ("**The rewrites in this report were written by fresh agents that did not design the prompts**: each received only its own folder (`TASK.md`, the generated spec and prompt, the original source, the gate tools) "
           "and the `dagster-expert` and `dagster-community-components` skills, and was told not to read the converter's output, the rewrite examples or any other DAG's result; `eval_kit.py check` verified before launch that no folder held converter output or another DAG's data." if cold
           else "**The rewrites in this report were written by the agent that designed the prompts, so they are NOT cold-worker numbers.** They show the harness working, not the route's performance.")
    L: list[str] = []
    A = L.append
    ev = [r for r in recs if r["behavior"]]
    A("# The rewrite route against real Airflow: behaviour evaluation")
    A("")
    A("*Generated by `scripts/eval_report.py` from the files `eval_corpus.py`, `eval_sample.py`, `eval_kit.py`, `eval_gates.py` and `eval_behavior.py` wrote. Nothing in this file was typed by hand; re-run the pipeline and it changes.*")
    A("")
    if not ev:
        A("**Not run.** No behaviour result exists for this run yet, so nothing below has been measured and this file holds no number. "
          f"{len(recs)} DAGs are sampled ({', '.join(a.runs)}); the harness, the cold-worker kit and the run book are ready: [rewrite-route-evaluation-runbook.md](rewrite-route-evaluation-runbook.md). "
          "Once the workers have written the rewrites and `eval_gates.py`, `eval_behavior.py` and `eval_report.py` have run, this file is regenerated with the results, who wrote the rewrites, and every `different` with its diff.")
        A("")
        return "\n".join(L) + "\n"
    A("## What this measures, and who wrote what")
    A("")
    A(who)
    A("")
    A(f"**Sample.** {len(recs)} DAGs ({', '.join(a.runs)}), drawn by a seeded, stratified sampler from the {('%d DAGs of the corpus that are eligible for behaviour comparison' % sum(1 for r in manifest['dags'] if r.get('eligible'))) if manifest else 'eligible corpus'}. "
      "**All are public example DAGs** (Astronomer example repositories, Apache Airflow's own example DAGs and a few others): teaching DAGs, chosen by what they could run without a real external system. This is not an estate; "
      "read the numbers as what the two routes do on these DAGs and nothing about yours.")
    A("")
    A("**The comparison is behaviour, not shape.** For each DAG the original runs in real Airflow (`dag.test()`, twice, so nondeterministic fields are excluded), and each route's Dagster output runs in an identical sandbox "
      "(scratch working directory, SQLite, local HTTP and SMTP recorders, moto for S3). Compared separately: overall outcome and every task's outcome, XCom values against asset values, files created or changed (names and content hashes), "
      "SQLite rows, HTTP requests sent (method, URL, body), mails sent, S3 objects, failure behaviour, and the metadata (schedule, retries, pools, dependencies, job). Verdicts: **equivalent** (every compared dimension matches), "
      "**equivalent-with-differences** (only metadata differs, or the only value difference is a value nothing consumes), **different** (a behavioural dimension differs), **not-comparable**, and **baseline-failed** "
      "(Airflow itself failed for an environmental reason: excluded from every share and listed below). **matches-baseline-failure** is reported separately: the DAG's own code fails in Airflow and the route fails the same way. **stub-matches-failure** is the same outcome where the route's failing step is a loud converter stub (`AIRFLOW-CAVEAT`): nothing was converted, so it is counted separately and never as a success. "
      "**no-output** means the route produced nothing (the converter deferred the DAG, or the worker produced no project); it counts against the route.")
    A("")
    if manifest:
        rows = manifest["dags"]
        ext = [r for r in rows if r["extracted"]]
        elig = [r for r in rows if r.get("eligible")]
        base = json.loads(a.baselines.read_text()) if a.baselines and Path(a.baselines).exists() else None
        usable = [r for r in elig if base and base.get(r["key"], {}).get("class") in ("ok", "by-design-failure")]
        A("## The corpus and the sample")
        A("")
        A(f"{len(manifest['repos'])} public repositories ({', '.join(sorted({v['github'].split('/')[0] for v in manifest['repos'].values()}))}); Airflow {', '.join(f'{k}.x: {v}' for k, v in sorted(manifest.get('airflow_versions', {}).items()))}. "
          f"**{len(rows)} DAGs known** (by Airflow's own parse or the static scan); **{len(ext)} extracted** (the rest failed to import: a missing package, a provider version, a file that needs a service); "
          f"**{len(elig)} eligible** for behaviour comparison (every external system the DAG touches has a local stand-in)" + (f"; **{len(usable)} of those have a usable Airflow baseline** (it ran, or failed by design and reproducibly); the other {len(elig) - len(usable)} are baseline-failed or unstable and were not sampled." if base else "."))
        A("")
        cc = collections.Counter(r["converter"] for r in (usable if base else elig))
        pc = collections.Counter(r["pattern"] for r in (usable if base else elig))
        A(f"Converter result over the {'usable' if base else 'eligible'} DAGs: {dict(sorted(cc.items()))}. Pattern: {dict(sorted(pc.items()))}.")
        A("")
        A("Sample: " + "; ".join(f"{r}: {len([x for x in recs if x['run'] == r])} DAGs" for r in a.runs) + f". By stratum: {dict(sorted(collections.Counter(x['stratum'] for x in recs).items()))}.")
        A("")
    def block(sub: list[dict[str, Any]], H: str) -> None:
        A(f"{H} Results by route")
        A("")
        for route, label in (("converter", "Deterministic converter"), ("rewrite", "Rewrite (spec and prompt, written by a worker)")):
            A(f"{H}# {label}")
            A("")
            A(who if route == "rewrite" else "The converter is deterministic; no model wrote it. Its output here is `convert_ir.py --return-all` (every Python task returns its value, which the value comparison needs).")
            A("")
            L.extend(route_table(sub, route))
            A("")
        A(f"{H} Split by stratum (the converter's own result) and by pattern")
        A("")
        for route in ("converter", "rewrite"):
            for field in ("stratum", "pattern", "repo"):
                A(f"**{route}, by {field}**")
                A("")
                L.extend(split_table(sub, route, field))
                A("")
        # paired
        A(f"{H} Paired comparison: the same DAG through both routes")
        A("")
        pool = [r for r in sub if r["behavior"] and not r["baseline_failed"] and r["verdicts"].get("rewrite") not in (None, "no-output")]
        A(f"Population: the {len(pool)} DAGs where Airflow's baseline ran and the worker produced a rewrite. A converter `no-output` (deferred) counts as the converter not being equivalent.")
        A("")
        def ok_in(r: dict[str, Any], route: str, good: set[str], substantive: bool) -> bool:
            return r["verdicts"].get(route) in good and not (substantive and thin(r, route))
        for name, good, subst in (("strictly equivalent (thin ones included)", GOOD_STRICT, False), ("strictly equivalent **with a substantive comparison** (a thin `equivalent`, where only outcome and structure were observable, does not count as shown equivalent)", GOOD_STRICT, True),
                                  ("equivalent, with or without metadata differences (or matching a by-design failure)", GOOD_LENIENT, False)):
            both = sum(1 for r in pool if ok_in(r, "rewrite", good, subst) and ok_in(r, "converter", good, subst))
            rw = sum(1 for r in pool if ok_in(r, "rewrite", good, subst) and not ok_in(r, "converter", good, subst))
            cv = sum(1 for r in pool if not ok_in(r, "rewrite", good, subst) and ok_in(r, "converter", good, subst))
            neither = len(pool) - both - rw - cv
            A(f"**{name}**")
            A("")
            A("| both routes | rewrite only | converter only | neither |")
            A("|---|---|---|---|")
            A(f"| {both} | {rw} | {cv} | {neither} |")
            A("")
            A(f"Discordant pairs: {rw + cv}; exact two-sided sign test p = {sign_test(rw, cv):.3f}" + (" (too few discordant pairs to conclude either way)" if rw + cv < 10 else "") + ".")
            A("")

    arms = sorted({r["arm"] for r in recs if r.get("arm")})
    block(recs, "##")
    if len(arms) > 1:
        A("## The same tables split by arm")
        A("")
        A("ARM A: the worker's kit had the behaviour-feedback tool (`tools/compare_behavior.py` and the baseline). ARM B: no feedback tool and no baseline; every other instruction, the gates and the pattern library were identical. "
          "**The two arms are different DAGs** (each DAG was given to one arm), so everything below is a between-DAG comparison.")
        A("")
        for arm in arms:
            A(f"## Arm {arm} ({len([r for r in recs if r.get('arm') == arm])} DAGs)")
            A("")
            block([r for r in recs if r.get("arm") == arm], "###")
        L += arm_comparison(recs)
    nd = nondeterministic_rows(list(recs))
    if nd:
        A("## Task states not compared: nondeterministic decisions")
        A("")
        A(f"{len(nd)} DAG(s) have a branch or short-circuit decision that reads the clock or a random number, so the baseline's run and a later run can take different branches through no fault of the route. For these the per-task state comparison below the decision is reported as `nondeterministic: not compared`, not as a difference; the overall outcome and the failure behaviour are still compared.")
        A("")
        for dag_id, why in sorted(nd):
            A(f"- `{dag_id}`: {why}")
        A("")
    nb = baseline_nondeterministic(list(recs))
    if nb:
        A("## Task states not compared: tasks that are nondeterministic in Airflow itself")
        A("")
        A(f"{len(nb)} DAG(s) have a task whose final state in Airflow differed between the recorded baseline runs (the measurement's two runs and any earlier run of the same DAG the kit baked in). Such a task is classified `nondeterministic: not compared`: the baseline cannot say what the correct state is. The rest of the DAG is compared as usual.")
        A("")
        for dag_id, tasks in sorted(nb):
            A(f"- `{dag_id}`: {', '.join(f'`{t}`' for t in tasks)}")
        A("")
    L += registry_first(recs)
    # gates vs behaviour
    A("## Gates against behaviour (rewrite)")
    A("")
    g = [r for r in recs if r["gates"] is not None]
    if g:
        gp = [r for r in g if r["gates"].get("passed")]
        ng = max((len(r["gates"].get("gates", [])) for r in g), default=0)
        A(f"All {ng} gates pass for {pct(len(gp), len(g))} of the rewrites that were gated. The gates check that the code loads, accounts for its tasks, has no secrets or invented APIs, and (in the later gate sets) that task states and effects match a baseline and a decision record exists; they are not the behaviour comparison. Gate pass against behaviour verdict:")
        A("")
        A("| | " + " | ".join(VERDICTS[:-1]) + " |")
        A("|---|" + "---|" * (len(VERDICTS) - 1))
        for lab, sub in (("all gates passed", gp), ("a gate failed", [r for r in g if not r["gates"].get("passed")])):
            c = collections.Counter(r["verdicts"].get("rewrite", "not run") for r in sub if not r["baseline_failed"])
            A(f"| {lab} | " + " | ".join(str(c[v]) for v in VERDICTS[:-1]) + " |")
        A("")
        gc: collections.Counter = collections.Counter()
        for r in g:
            for x in r["gates"].get("gates", []):
                gc[(x["gate"], x["passed"])] += 1
        A("| gate | passed | failed |")
        A("|---|---|---|")
        for name in sorted({k[0] for k in gc}):
            A(f"| {name} | {gc[(name, True)]} | {gc[(name, False)]} |")
        dec = [(r, x) for r in g for x in r["gates"].get("gates", []) if x["gate"] == "decision_record" and x.get("declared") is not None]
        if dec:
            A("")
            A("### Decision records: what workers declared not preserved")
            A("")
            nd = [(r, x["declared"]["not_preserved"]) for r, x in dec]
            A(f"{len(dec)} rewrite(s) had a decision record checked; {sum(1 for _, n in nd if n)} declared at least one behaviour not preserved ({sum(len(n) for _, n in nd)} declarations in all). A declaration is honest accounting, not a pass: the behaviour comparison still judges the result.")
            A("")
            fb = [(r, x["declared"].get("fallback") or {}) for r, x in dec]
            A(f"{sum(1 for _, f in fb if f)} rewrite(s) used a **fallback to plain Python** ({sum(len(f) for _, f in fb)} declarations) instead of a native Dagster form; the native form is the expected path and its skipped states are compared directly, the fallback is accepted but counted here with its reason.")
            A("")
            if any(f for _, f in fb):
                A("| behaviour | fallbacks | reasons |")
                A("|---|---|---|")
                fc: collections.Counter = collections.Counter(b for _, f in fb for b in f)
                for b in sorted(fc):
                    A(f"| {b} | {fc[b]} | " + "; ".join(sorted({str(f[b])[:90] for _, f in fb if b in f})[:4]).replace("|", "/") + " |")
                A("")
            bc: collections.Counter = collections.Counter(b for _, n in nd for b in n)
            if bc:
                A("| behaviour | declared not preserved | rewrites that declared it preserved |")
                A("|---|---|---|")
                for b in sorted(bc):
                    A(f"| {b} | {bc[b]} | {sum(1 for _, x in dec if b in x['declared']['preserved'])} |")
    else:
        A("Gate results: not run (no `eval_gates.py` output).")
    A("")
    # cost
    A("## Cost")
    A("")
    A("The converter costs seconds of CPU and no model tokens. The rewrite's cost per DAG, from the workers' own `WORKER_REPORT.md` (attempts) and the `costs.json` supplied for tokens and tool uses (mean / median):")
    A("")
    A("| stratum | DAGs | attempts | tokens | tool uses | duration (s) |")
    A("|---|---|---|---|---|---|")
    def cost_of(r: dict[str, Any], f: str) -> float | None:
        for k in (r["key"], r["dir"], r["dag_id"]):
            if k in costs and costs[k].get(f) is not None:
                return float(costs[k][f])
        return None
    for st in ["clean", "degraded", "stubbed", "deferred", "all"]:
        sub = [r for r in recs if st == "all" or r["stratum"] == st]
        att = [float(r["report"]["attempts"]) for r in sub if r["report"] and r["report"].get("attempts") is not None]
        vals = {f: [x for x in (cost_of(r, f) for r in sub) if x is not None] for f in ("tokens", "tool_uses", "duration_s")}
        A(f"| {st} | {len(sub)} | {med(att)} ({len(att)} reported) | {med(vals['tokens'])} ({len(vals['tokens'])}) | {med(vals['tool_uses'])} ({len(vals['tool_uses'])}) | {med(vals['duration_s'])} ({len(vals['duration_s'])}) |")
    for arm in sorted({r["arm"] for r in recs if r.get("arm")}) if len({r.get("arm") for r in recs if r.get("arm")}) > 1 else []:
        sub = [r for r in recs if r.get("arm") == arm]
        att = [float(r["report"]["attempts"]) for r in sub if r["report"] and r["report"].get("attempts") is not None]
        vals = {f: [x for x in (cost_of(r, f) for r in sub) if x is not None] for f in ("tokens", "tool_uses", "duration_s")}
        A(f"| **arm {arm}** | {len(sub)} | {med(att)} ({len(att)} reported) | {med(vals['tokens'])} ({len(vals['tokens'])}) | {med(vals['tool_uses'])} ({len(vals['tool_uses'])}) | {med(vals['duration_s'])} ({len(vals['duration_s'])}) |")
    if not costs:
        A("")
        A("`costs.json` was not supplied: token, tool-use and duration columns are **not available**.")
    A("")
    # variance
    A("## Variance: two independent rewrites of the same DAG")
    A("")
    if a.variance_run:
        v2 = gather(work, a.variance_run)
        pairs = [(r, v2[r["key"]]) for r in recs if r["key"] in v2 and r["behavior"] and v2[r["key"]]["behavior"] and not r["baseline_failed"]]
        A(f"{len(pairs)} DAGs have a second independent rewrite (run `{a.variance_run}`).")
        A("")
        agree = sum(1 for x, y in pairs if x["verdicts"].get("rewrite") == y["verdicts"].get("rewrite"))
        A(f"Same behaviour verdict in both passes: {pct(agree, len(pairs))}.")
        A("")
        A("| DAG | pass 1 | pass 2 | assets (1 / 2) | registry components (1 / 2) | attempts (1 / 2) |")
        A("|---|---|---|---|---|---|")
        for x, y in pairs:
            def nassets(r: dict[str, Any]) -> str:
                return str(len(((r["behavior"]["routes"].get("rewrite") or {}).get("taskmap") or {}) and {v.get("asset") for v in r["behavior"]["routes"]["rewrite"]["taskmap"].values() if v.get("asset")}))
            A(f"| `{x['dag_id']}` | {x['verdicts'].get('rewrite')} | {y['verdicts'].get('rewrite')} | {nassets(x)} / {nassets(y)} | "
              f"{len((x['report'] or {}).get('registry_components', []))} / {len((y['report'] or {}).get('registry_components', []))} | {(x['report'] or {}).get('attempts')} / {(y['report'] or {}).get('attempts')} |")
        A("")
        both_good = sum(1 for x, y in pairs if x["verdicts"].get("rewrite") in GOOD_LENIENT and y["verdicts"].get("rewrite") in GOOD_LENIENT)
        one_good = sum(1 for x, y in pairs if (x["verdicts"].get("rewrite") in GOOD_LENIENT) != (y["verdicts"].get("rewrite") in GOOD_LENIENT))
        A(f"Both passes equivalent (lenient): {both_good}; exactly one: {one_good}; neither: {len(pairs) - both_good - one_good}. A route whose second pass disagrees with the first is a route whose single-pass number carries that much noise.")
    else:
        A("No second pass was run: variance is **not measured** (not run).")
    A("")
    # differ list
    A("## Every `different`, with the diff")
    A("")
    for route in ("rewrite", "converter"):
        rows = [r for r in recs if r["behavior"] and not r["baseline_failed"] and r["verdicts"].get(route) == "different"]
        A(f"### {route} ({len(rows)})")
        A("")
        for r in sorted(rows, key=lambda r: r["dag_id"]):
            x = r["behavior"]["routes"][route]
            A(f"- `{r['dag_id']}` ({r['stratum']}, {r['pattern']}, {r['repo']}): {x['rollup'].get('why')}. {evidence_text(x)}")
        A("")
    # baseline-failed
    A("## Baseline-failed DAGs (excluded from the shares)")
    A("")
    bf = [r for r in recs if r["baseline_failed"]]
    A(f"{len(bf)} of {len(recs)} sampled DAGs failed in Airflow itself for an environmental reason (a missing package, an unreachable service, an undefined connection or variable, a timeout):")
    A("")
    for r in sorted(bf, key=lambda r: r["dag_id"]):
        A(f"- `{r['dag_id']}` ({r['repo']}): {r['behavior']['baseline']['class'][1][:220]}")
    A("")
    # failed by design, kept apart
    A("## Failed by design (not successes)")
    A("")
    A("`matches-baseline-failure` is its own group: the DAG's own code fails in Airflow and the route fails the same way. It is shown here, not among the successes, and a route whose failing step is a converter stub is `stub-matches-failure`, a separate group.")
    A("")
    for route in ("converter", "rewrite"):
        for ln in failed_by_design_lines(recs, route):
            A(ln)
    A("## What a strict `equivalent` did not observe")
    A("")
    for route in ("converter", "rewrite"):
        for ln in strict_unobserved_lines(recs, route):
            A(ln)
    envl = [ln for route in ("converter", "rewrite") for ln in route_environment_lines(recs, route)]
    if envl:
        A("## Route environment")
        A("")
        for ln in envl:
            A(ln)
    # dims
    A("## What was compared, and what could not be")
    A("")
    for route in ("converter", "rewrite"):
        A(f"**{route}**: dimension verdicts over the evaluated DAGs")
        A("")
        A("| dimension | match | differ | not-compared |")
        A("|---|---|---|---|")
        for dname in DIMS:
            c = collections.Counter(((r["behavior"]["routes"].get(route) or {}).get("dimensions") or {}).get(dname, {}).get("verdict") for r in recs if r["behavior"] and not r["baseline_failed"] and route in r["behavior"]["routes"] and (r["behavior"]["routes"][route].get("dimensions")))
            A(f"| {dname} | {c['match']} | {c['differ']} | {c['not-compared']} |")
        A("")
    thin_rows = [r for r in recs if r["behavior"] and not r["baseline_failed"] and thin(r, "rewrite")]
    A(f"Rewrites judged `equivalent` where only the outcome and structure were observable (no value, file, row, request, mail, object or printed line to compare; flagged `thin`): "
      f"{sum(1 for r in thin_rows if r['verdicts'].get('rewrite') == 'equivalent')} of {sum(1 for r in recs if r['verdicts'].get('rewrite') == 'equivalent')}. An `equivalent` on a thin DAG means: it ran, the tasks ended in the same states, the structure matches. "
      "The headline table, the split tables and the paired test all show the `equivalent` count with and without a substantive comparison.")
    A("")
    A("**The printed-output dimension (`stdout`).** The plain lines a run printed (not log records: no timestamp or level prefix; no warnings or tracebacks) are compared as multisets, after masking the tokens that differ between two runs of the same code "
      "(timestamps and dates, run ids, uuids, memory addresses, temporary and sandbox paths, process ids, durations). Controls against false positives: only lines BOTH of the original's runs printed are required; the route may print MORE than "
      "the original (its own `$ command` echo, progress lines), and extra lines are listed, never a difference; the order and the task a line belongs to are not compared (the Dagster side has no reliable per-task attribution of process output); "
      "it is not one of the behaviour dimensions, so a printed-line difference makes a rewrite `equivalent-with-differences`, never `different`; when the original printed no stable line (or no record exists) it is `not compared`. "
      "It does NOT see output a task writes through a logger (Airflow's BashOperator output is a log record), so a bash-only DAG stays `thin`.")
    A("")
    A("**The harness cannot compare:** durations and data volume; anything that needs a real external system (the sample excludes such DAGs; the stand-ins are SQLite, HTTP and SMTP recorders and S3 through moto, and a DAG using any other database, warehouse, queue or API is out of scope); "
      "HTTP responses other than a canned 200 `{\"ok\": true}` (a DAG whose logic depends on the response body is compared only for that response); Airflow scheduling (ticks, catchup, backfill, data intervals: a cron string and timezone are compared, "
      "not the runs they would create); asset-triggered and interval schedules (reported not-compared); retries as they happen (the policy is compared, no failure is injected); pool and concurrency behaviour under load; "
      "sensors' reschedule behaviour over wall-clock time; callbacks, SLAs and notifications sent through channels other than SMTP and HTTP; the Airflow UI, logs and run history; deferrable triggers. "
      "Values are compared as JSON, so a different but equivalent representation (a date as a string against a datetime) shows as a difference.")
    A("")
    for ln in '## The prompt changed after stage 2a\n\nThe rewrite numbers in this report for `stage1`, `stage1b` and `stage2a` belong to the previous rewrite prompt. After stage 2a the prompt gained a resource-first rule, a registry-resource table for the operator families a DAG uses, a registry-search checklist and an optional converter starting point, and was made smaller (`reference/rewrite-route.md`, "The prompt after stage 2a"). A smaller prompt is not shown to give equal accuracy; a run built with the new prompt must be reported separately.'.splitlines():
        A(ln)
    A("")
    A("## Earlier measurement, for context")
    A("")
    A("The first measurement (`reference/rewrite-route.md`, `examples/rewrite/sample/`) had 8 DAGs, **the rewrites were written by the agent that designed the prompts (not cold)**, and one table mixed \"loads and has the right shape\" with \"behaves like Airflow\". "
      "Nothing in this report reuses those numbers.")
    A("")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--writers", choices=["cold", "designer"], required=True)
    ap.add_argument("--costs", type=Path)
    ap.add_argument("--variance-run")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--baselines", type=Path, help="baselines.json from eval_baseline.py (for the corpus section)")
    ap.add_argument("--out", type=Path, default=C.SKILL / "reference" / "rewrite-route-evaluation.md")
    a = ap.parse_args(argv)
    text = render(a)
    a.out.write_text(text)
    print(f"wrote {a.out} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
