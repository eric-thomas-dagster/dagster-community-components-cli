#!/usr/bin/env python3
"""Seeded, deterministic, stratified sample of the evaluation corpus (the DAGs eligible for behaviour comparison).

    python3 eval_sample.py --manifest corpus/manifest.json --stage 1 --seed 20260101 --out sample.json
    python3 eval_sample.py --manifest corpus/manifest.json --stage 2 --out sample.json            (every eligible DAG; stage 1's DAGs come first)
    python3 eval_sample.py --manifest corpus/manifest.json --n 120 --out sample.json              (a stratified sample of exactly 120)
    python3 eval_sample.py --subset-of sample.json --n 12 --out sample_b.json                     (a stratified subset, for the second independent pass)

Strata are the converter's result: clean, degraded, stubbed, deferred ("hard" = stubbed or deferred). Inside a stratum DAGs are drawn round-robin over (pattern, repository) buckets in a seeded
order, so no single pattern and no single repository dominates, and a repository holds at most `--max-repo-share` of the sample (relaxed only when nothing else is left to draw).

  stage 1   about 40 DAGs: every eligible hard DAG up to 20, the rest split between degraded and clean (about 40 / 60)
  stage 2   as many as possible: the whole eligible corpus. There is no cap: 100 or 280 DAGs work the same. The stage 1 sample is its prefix, so stage 2 continues stage 1 rather than redoing it
  --n       a stratified sample of that size, proportional to the strata (hard DAGs first when n is small)
  --exclude earlier sample files whose DAGs are removed from the pool first (a fresh sample, for example stage 2a without stage 1's 40)
  --arms-seed  then assign every DAG to ARM A or ARM B, stratified by stratum and pattern (seeded), and to a worker group of --group-size that never mixes arms

With --baselines (from eval_baseline.py) only DAGs whose Airflow baseline ran are sampled. Same manifest, baselines and seed give the same file. Writes sample.json and a readable sample.md.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
from pathlib import Path
from typing import Any

STRATA = ["clean", "degraded", "stubbed", "deferred"]


USABLE_BASELINES = {"ok", "by-design-failure"}


def eligible(manifest: dict[str, Any], baselines: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Eligible DAGs; with `baselines` (from eval_baseline.py) only those whose Airflow baseline ran: ok, or failing by design (reproducibly)."""
    rows = [r for r in manifest["dags"] if r.get("eligible") and r["converter"] in STRATA]
    if baselines is not None:
        rows = [r for r in rows if baselines.get(r["key"], {}).get("class") in USABLE_BASELINES]
    return sorted(rows, key=lambda r: r["key"])


def draw(rows: list[dict[str, Any]], k: int, rng: random.Random, taken_by_repo: collections.Counter, repo_cap: int) -> list[dict[str, Any]]:
    """k rows from `rows`, round-robin over pattern then repository, seeded; respects repo_cap while another bucket can still give."""
    if k <= 0 or not rows:
        return []
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        buckets[r["pattern"]][r["repo"]].append(r)
    for p in buckets:
        for repo in buckets[p]:
            rng.shuffle(buckets[p][repo])
    patterns = sorted(buckets)
    rng.shuffle(patterns)
    picked: list[dict[str, Any]] = []
    for relax in (False, True):
        progress = True
        while len(picked) < k and progress:
            progress = False
            for p in patterns:
                if len(picked) >= k:
                    break
                repos = sorted(r for r in buckets[p] if buckets[p][r])
                repos.sort(key=lambda r: (taken_by_repo[r], r))              # the least represented repository first
                for repo in repos:
                    if not relax and taken_by_repo[repo] >= repo_cap:
                        continue
                    row = buckets[p][repo].pop()
                    picked.append(row)
                    taken_by_repo[repo] += 1
                    progress = True
                    break
    return picked


def stage1(rows: list[dict[str, Any]], seed: int, n: int, max_share: float) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    cap = max(2, int(n * max_share))
    taken: collections.Counter = collections.Counter()
    by = {s: [r for r in rows if r["converter"] == s] for s in STRATA}
    hard = by["stubbed"] + by["deferred"]
    k_hard = min(20, len(hard), n // 2)
    sel = draw(hard, k_hard, rng, taken, cap)
    rest = n - len(sel)
    k_deg = min(len(by["degraded"]), round(rest * 0.4))
    sel += draw(by["degraded"], k_deg, rng, taken, cap)
    sel += draw(by["clean"], n - len(sel), rng, taken, cap)
    if len(sel) < n:                                                          # a stratum ran out: fill from whatever is left
        have = {r["key"] for r in sel}
        sel += draw([r for r in rows if r["key"] not in have], n - len(sel), rng, taken, cap)
    return sel


def stratified(rows: list[dict[str, Any]], seed: int, n: int, max_share: float) -> list[dict[str, Any]]:
    if n >= len(rows):
        return list(rows)
    rng = random.Random(seed)
    cap = max(2, int(n * max_share))
    taken: collections.Counter = collections.Counter()
    by = {s: [r for r in rows if r["converter"] == s] for s in STRATA}
    sel: list[dict[str, Any]] = []
    quota = {s: max(1 if by[s] else 0, round(n * len(by[s]) / len(rows))) for s in STRATA}
    while sum(quota.values()) > n:
        quota[max(quota, key=quota.get)] -= 1
    for s in ["stubbed", "deferred", "degraded", "clean"]:
        sel += draw(by[s], quota[s], rng, taken, cap)
    if len(sel) < n:
        have = {r["key"] for r in sel}
        sel += draw([r for r in rows if r["key"] not in have], n - len(sel), rng, taken, cap)
    return sel


def build_sample(manifest: dict[str, Any], stage: int | None, n: int | None, seed: int, max_share: float = 0.25, n1: int = 40, baselines: dict[str, Any] | None = None, exclude: set[str] | None = None) -> dict[str, Any]:
    rows = eligible(manifest, baselines)
    n_before = len(rows)
    if exclude:                                                                # a fresh sample: nothing drawn earlier (for example stage 1) may be drawn again
        rows = [r for r in rows if r["key"] not in exclude]
    first = stage1(rows, seed, min(n1, len(rows)), max_share)
    if stage == 1:
        chosen, label = first, "stage 1"
    elif stage == 2:
        have = {r["key"] for r in first}
        rest = [r for r in rows if r["key"] not in have]
        rng = random.Random(seed + 2)
        taken: collections.Counter = collections.Counter(r["repo"] for r in first)
        extra = draw(rest, len(rest) if n is None else max(0, min(n, len(rows)) - len(first)), rng, taken, len(rows))
        chosen, label = first + extra, "stage 2"
    else:
        chosen, label = stratified(rows, seed, n or 40, max_share), f"n={n}"
    s1 = {r["key"] for r in first} if not exclude else set()                   # `stage1` marks the stage 1 prefix; a fresh sample has none
    entries = [{"key": r["key"], "dag_id": r["dag_id"], "repo": r["repo"], "stratum": r["converter"], "hard": r["converter"] in ("stubbed", "deferred"), "pattern": r["pattern"], "patterns": r["patterns"],
                "airflow": r["airflow"], "stage1": r["key"] in s1} for r in chosen]
    cnt = lambda f: dict(sorted(collections.Counter(f(e) for e in entries).items()))      # noqa: E731
    return {"seed": seed, "label": label, "n": len(entries), "eligible_corpus": len(rows), "baseline_filter": baselines is not None,
            "dropped_for_baseline": (len(eligible(manifest)) - n_before) if baselines is not None else None, "excluded_earlier_draws": (n_before - len(rows)) if exclude else 0, "max_repo_share": max_share, "dags": entries,
            "counts": {"stratum": cnt(lambda e: e["stratum"]), "pattern": cnt(lambda e: e["pattern"]), "repo": cnt(lambda e: e["repo"])}}


def subset(sample: dict[str, Any], n: int, seed: int) -> dict[str, Any]:
    """A stratified subset of an existing sample (for the second, independent pass that measures variance): same strata proportions, hard DAGs first."""
    rows = [{**e, "converter": e["stratum"]} for e in sample["dags"]]
    chosen = stratified(rows, seed, min(n, len(rows)), 1.0)
    entries = [{k: v for k, v in r.items() if k != "converter"} for r in chosen]
    cnt = lambda f: dict(sorted(collections.Counter(f(e) for e in entries).items()))      # noqa: E731
    return {**{k: sample[k] for k in ("eligible_corpus", "max_repo_share", "baseline_filter", "dropped_for_baseline") if k in sample}, "seed": seed, "label": f"subset of {sample['label']} for variance (n={len(entries)})",
            "n": len(entries), "dags": entries, "counts": {"stratum": cnt(lambda e: e["stratum"]), "pattern": cnt(lambda e: e["pattern"]), "repo": cnt(lambda e: e["repo"])}}


def assign_arms(sample: dict[str, Any], seed: int, group_size: int = 4) -> dict[str, Any]:
    """Randomly (seeded) assign each DAG to ARM A or ARM B, stratified by the converter's stratum and by pattern, and split each arm into worker groups. Inside every (stratum, pattern) cell the DAGs are shuffled and dealt
    alternately to A and B, and the alternation CONTINUES across cells (a seeded random first arm), so each cell is balanced within one DAG and the arms differ in size by at most one overall. Groups: each arm's DAGs are
    shuffled (seeded) and cut into consecutive groups of `group_size` (the last may be smaller); a group never mixes arms, because a worker sees one arm only. Adds `arm` and `group` to every entry, `arm_seed`,
    `groups` and the balance tables to the sample."""
    rng = random.Random(seed)
    cells: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for e in sample["dags"]:
        cells[(e["stratum"], e["pattern"])].append(e)
    nxt = rng.choice("AB")
    for key in sorted(cells):
        es = sorted(cells[key], key=lambda e: e["key"])
        rng.shuffle(es)
        for e in es:
            e["arm"] = nxt
            nxt = "B" if nxt == "A" else "A"
    groups: dict[str, list[str]] = {}
    for arm in "AB":
        es = sorted((e for e in sample["dags"] if e["arm"] == arm), key=lambda e: e["key"])
        rng.shuffle(es)
        for i in range(0, len(es), group_size):
            gid = f"{arm}{i // group_size + 1}"
            for e in es[i:i + group_size]:
                e["group"] = gid
            groups[gid] = [e["dag_id"] for e in es[i:i + group_size]]
    cnt = lambda f: dict(sorted(collections.Counter(f(e) for e in sample["dags"]).items()))      # noqa: E731
    sample["arm_seed"] = seed
    sample["groups"] = groups
    sample["counts"]["arm"] = cnt(lambda e: e["arm"])
    sample["counts"]["arm_by_stratum"] = cnt(lambda e: f"{e['arm']}/{e['stratum']}")
    sample["counts"]["arm_by_pattern"] = cnt(lambda e: f"{e['arm']}/{e['pattern']}")
    return sample


def render_groups(sample: dict[str, Any], folder_names: dict[str, str]) -> str:
    """GROUPS.md: group id, arm, DAG folder names, then the strata and pattern balance of the two arms."""
    out = [f"# Worker groups ({sample['label']})", "",
           "A worker takes ONE group and sees one arm only. **ARM A**: the kit has the behaviour-feedback tool (`tools/compare_behavior.py` and `tools/baseline/`). **ARM B**: no feedback tool and no baseline; everything else is identical (the pattern library, the gates, the sandbox runner).", "",
           "| group | arm | DAGs | folders |", "|---|---|---|---|"]
    for gid, ids in sample["groups"].items():
        es = [e for e in sample["dags"] if e.get("group") == gid]
        out.append(f"| {gid} | {es[0]['arm']} | {len(es)} | " + ", ".join(f"`{folder_names[e['key']]}`" for e in es) + " |")
    out += ["", f"Arm assignment seed {sample['arm_seed']}; sample seed {sample['seed']}. Strata balance (DAGs per arm):", "", "| | A | B |", "|---|---|---|"]
    for title, field in (("stratum", "stratum"), ("pattern", "pattern")):
        for v in sorted({e[field] for e in sample["dags"]}):
            out.append(f"| {title} `{v}` | {sum(1 for e in sample['dags'] if e['arm'] == 'A' and e[field] == v)} | {sum(1 for e in sample['dags'] if e['arm'] == 'B' and e[field] == v)} |")
    out.append(f"| **all** | {sum(1 for e in sample['dags'] if e['arm'] == 'A')} | {sum(1 for e in sample['dags'] if e['arm'] == 'B')} |")
    return "\n".join(out) + "\n"


def render_md(s: dict[str, Any]) -> str:
    out = [f"# Evaluation sample ({s['label']}, seed {s['seed']}): {s['n']} DAGs of {s['eligible_corpus']} eligible" + (f" with a usable Airflow baseline ({s['dropped_for_baseline']} eligible DAGs dropped because their baseline failed or was unstable)" if s.get("baseline_filter") else ""), "",
           f"By stratum: {s['counts']['stratum']}", f"By pattern: {s['counts']['pattern']}", f"By repository: {s['counts']['repo']}", ""]
    for st in STRATA:
        es = [e for e in s["dags"] if e["stratum"] == st]
        if es:
            out += [f"## {st} ({len(es)})", ""] + [f"- `{e['dag_id']}` ({e['repo']}, {e['pattern']}, Airflow {e['airflow']})" for e in es] + [""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--subset-of", type=Path, help="an existing sample.json: write a stratified subset of --n of its DAGs (the variance pass)")
    ap.add_argument("--stage", type=int, choices=[1, 2])
    ap.add_argument("--n", type=int)
    ap.add_argument("--seed", type=int, default=20260101)
    ap.add_argument("--max-repo-share", type=float, default=0.25)
    ap.add_argument("--baselines", type=Path, help="baselines.json from eval_baseline.py: sample only DAGs whose Airflow baseline ran (ok, or failing by design)")
    ap.add_argument("--exclude", type=Path, nargs="*", default=[], help="earlier sample.json files whose DAGs must not be drawn again (a fresh sample)")
    ap.add_argument("--arms-seed", type=int, help="after drawing, assign the DAGs to ARM A / ARM B (stratified by stratum and pattern) with this seed and write `arm` and `group` into every entry")
    ap.add_argument("--group-size", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path("sample.json"))
    a = ap.parse_args(argv)
    if a.subset_of:
        s = subset(json.loads(a.subset_of.read_text()), a.n or 12, a.seed)
        a.out.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")
        a.out.with_suffix(".md").write_text(render_md({**s, "eligible_corpus": s.get("eligible_corpus", "?")}))
        print(f"wrote {a.out}: {s['n']} DAGs; by stratum {s['counts']['stratum']}")
        return 0
    if not a.manifest or (not a.stage and not a.n):
        ap.error("give --manifest and --stage 1, --stage 2 or --n")
    excl = {e["key"] for f in a.exclude for e in json.loads(f.read_text())["dags"]}
    s = build_sample(json.loads(a.manifest.read_text()), a.stage, a.n, a.seed, a.max_repo_share, baselines=json.loads(a.baselines.read_text()) if a.baselines else None, exclude=excl)
    if a.arms_seed is not None:
        assign_arms(s, a.arms_seed, a.group_size)
    a.out.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")
    a.out.with_suffix(".md").write_text(render_md(s))
    print(f"wrote {a.out} and {a.out.with_suffix('.md')}: {s['n']} DAGs; by stratum {s['counts']['stratum']}; by pattern {s['counts']['pattern']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
