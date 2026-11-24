"""Size up a folder of ADF JSON (git-integration files or ARM exports).

Usage: python -I tools/survey.py <dir>

Counts pipelines, activity types (nested ones included), expression functions,
trigger types, Copy source/sink pairs and how many pipelines contain an
activity with no automatic translation. Read-only; prints to stdout.
"""
import collections
import glob
import json
import re
import sys

STUB_ONLY = {"ExecuteDataFlow", "ExecuteWranglingDataflow", "ExecuteSSISPackage",
             "SynapseNotebook", "SparkJob", "HDInsightSpark", "DatabricksNotebook"}

acts, trig, fn, pairs = (collections.Counter() for _ in range(4))
pipes, stubby, arm_params, total_strings, bad = 0, 0, 0, 0, 0
per_pipe = []


def children(a):
    tp = a.get("typeProperties")
    if not isinstance(tp, dict):
        return
    for k in ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities"):
        if isinstance(tp.get(k), list):
            yield from tp[k]
    if isinstance(tp.get("cases"), list):
        for c in tp["cases"]:
            if isinstance(c, dict) and isinstance(c.get("activities"), list):
                yield from c["activities"]


def walk(lst, seen):
    for a in lst if isinstance(lst, list) else []:
        if not isinstance(a, dict):
            continue
        acts[a.get("type")] += 1
        seen.add(a.get("type"))
        if a.get("type") == "Copy":
            tp = a.get("typeProperties")
            tp = tp if isinstance(tp, dict) else {}
            s = tp["source"].get("type") if isinstance(tp.get("source"), dict) else "(parameterised)"
            k = tp["sink"].get("type") if isinstance(tp.get("sink"), dict) else "(parameterised)"
            pairs[f"{s} -> {k}"] += 1
        walk(list(children(a)), seen)


def scan(o):
    global arm_params, total_strings
    if isinstance(o, dict):
        if o.get("type") == "Expression" and "value" in o:
            for m in re.findall(r"\b([a-zA-Z]+)\(", str(o["value"])):
                fn[m] += 1
        for v in o.values():
            scan(v)
    elif isinstance(o, list):
        for v in o:
            scan(v)
    elif isinstance(o, str):
        total_strings += 1
        if o.startswith("[parameters("):
            arm_params += 1


def pipeline(p):
    global pipes, stubby
    props = p.get("properties", p)
    if not isinstance(props, dict) or "activities" not in props:
        return
    pipes += 1
    seen = set()
    walk(props["activities"], seen)
    scan(props)
    per_pipe.append(seen)
    if seen & STUB_ONLY:
        stubby += 1


for f in glob.glob(sys.argv[1] + "/**/*.json", recursive=True):
    if "manifest" in f:
        continue
    try:
        d = json.load(open(f, encoding="utf-8-sig"))
    except Exception:
        bad += 1
        continue
    if not isinstance(d, dict):
        continue
    if isinstance(d.get("resources"), list):
        for r in d["resources"]:
            t = str(r.get("type", ""))
            if t.endswith("/pipelines"):
                pipeline(r)
            elif t.endswith("/triggers"):
                trig[(r.get("properties") or {}).get("type")] += 1
    elif "activities" in d.get("properties", {}):
        pipeline(d)
    elif str((d.get("properties") or {}).get("type", "")).endswith("Trigger"):
        trig[d["properties"]["type"]] += 1

print(f"pipelines {pipes}  unparsable files {bad}")
print(f"pipelines with a stub-only activity (data flow, SSIS, Spark, notebooks): {stubby}")
print(f"ARM-parameterised strings: {arm_params} of {total_strings}")
print("\nactivities (nested included)")
for k, n in acts.most_common():
    print(f"  {n:5d} {k}")
print("\ntriggers", dict(trig))
print("\ncopy source -> sink (top 15)")
for k, n in pairs.most_common(15):
    print(f"  {n:4d} {k}")
print("\nexpression functions (top 25)")
print("  " + ", ".join(f"{k}:{n}" for k, n in fn.most_common(25)))
