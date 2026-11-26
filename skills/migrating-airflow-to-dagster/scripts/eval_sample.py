#!/usr/bin/env python3
"""Seeded, deterministic, stratified sample of the evaluation corpus (the DAGs eligible for behaviour comparison).

    python3 eval_sample.py --manifest corpus/manifest.json --stage 1 --seed 20260101 --out sample.json
    python3 eval_sample.py --manifest corpus/manifest.json --stage 2 --out sample.json            (every eligible DAG; stage 1's DAGs come first)
    python3 eval_sample.py --manifest corpus/manifest.json --n 120 --out sample.json              (a stratified sample of exactly 120)

Strata are the converter's result: clean, degraded, stubbed, deferred ("hard" = stubbed or deferred). Inside a stratum DAGs are drawn round-robin over (pattern, repository) buckets in a seeded
order, so no single pattern and no single repository dominates, and a repository holds at most `--max-repo-share` of the sample (relaxed only when nothing else is left to draw).

  stage 1   about 40 DAGs: every eligible hard DAG up to 20, the rest split between degraded and clean (about 40 / 60)
  stage 2   as many as possible: the whole eligible corpus. There is no cap: 100 or 280 DAGs work the same. The stage 1 sample is its prefix, so stage 2 continues stage 1 rather than redoing it
  --n       a stratified sample of that size, proportional to the strata (hard DAGs first when n is small)

Same manifest and same seed give the same file. Writes sample.json and a readable sample.md.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
from pathlib import Path
from typing import Any

STRATA = ["clean", "degraded", "stubbed", "deferred"]


def eligible(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted((r for r in manifest["dags"] if r.get("eligible") and r["converter"] in STRATA), key=lambda r: r["key"])


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


def build_sample(manifest: dict[str, Any], stage: int | None, n: int | None, seed: int, max_share: float = 0.25, n1: int = 40) -> dict[str, Any]:
    rows = eligible(manifest)
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
    s1 = {r["key"] for r in first}
    entries = [{"key": r["key"], "dag_id": r["dag_id"], "repo": r["repo"], "stratum": r["converter"], "hard": r["converter"] in ("stubbed", "deferred"), "pattern": r["pattern"], "patterns": r["patterns"],
                "airflow": r["airflow"], "stage1": r["key"] in s1} for r in chosen]
    cnt = lambda f: dict(sorted(collections.Counter(f(e) for e in entries).items()))      # noqa: E731
    return {"seed": seed, "label": label, "n": len(entries), "eligible_corpus": len(rows), "max_repo_share": max_share, "dags": entries,
            "counts": {"stratum": cnt(lambda e: e["stratum"]), "pattern": cnt(lambda e: e["pattern"]), "repo": cnt(lambda e: e["repo"])}}


def render_md(s: dict[str, Any]) -> str:
    out = [f"# Evaluation sample ({s['label']}, seed {s['seed']}): {s['n']} DAGs of {s['eligible_corpus']} eligible", "",
           f"By stratum: {s['counts']['stratum']}", f"By pattern: {s['counts']['pattern']}", f"By repository: {s['counts']['repo']}", ""]
    for st in STRATA:
        es = [e for e in s["dags"] if e["stratum"] == st]
        if es:
            out += [f"## {st} ({len(es)})", ""] + [f"- `{e['dag_id']}` ({e['repo']}, {e['pattern']}, Airflow {e['airflow']})" for e in es] + [""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--stage", type=int, choices=[1, 2])
    ap.add_argument("--n", type=int)
    ap.add_argument("--seed", type=int, default=20260101)
    ap.add_argument("--max-repo-share", type=float, default=0.25)
    ap.add_argument("--out", type=Path, default=Path("sample.json"))
    a = ap.parse_args(argv)
    if not a.stage and not a.n:
        ap.error("give --stage 1, --stage 2 or --n")
    s = build_sample(json.loads(a.manifest.read_text()), a.stage, a.n, a.seed, a.max_repo_share)
    a.out.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")
    a.out.with_suffix(".md").write_text(render_md(s))
    print(f"wrote {a.out} and {a.out.with_suffix('.md')}: {s['n']} DAGs; by stratum {s['counts']['stratum']}; by pattern {s['counts']['pattern']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
