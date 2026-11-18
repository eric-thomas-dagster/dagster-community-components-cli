import json, sys
sys.path.insert(0, ".")
import cases as C
ns = {}
exec(open(sys.argv[1]).read(), ns)            # the generated runtime.py
out = {}
for name, (lo, hi) in C.DATETIME_CASES.items():
    out[name] = {n.isoformat(): ns["branch_datetime"]({"logical_date": n}, lo, hi, "Y", "N", C.TZ) for n in C.NOWS}
for name, wd in C.WEEKDAY_CASES.items():
    out[name] = {n.isoformat(): ns["branch_weekday"]({"logical_date": n}, wd, "Y", "N", C.TZ) for n in C.NOWS}
json.dump(out, open("dg_out.json", "w"), indent=1)
a = json.load(open("af_out.json")); bad = 0; total = 0
for k in a:
    for t, v in a[k].items():
        total += 1
        if v != out[k][t]:
            bad += 1; print("DIFFERENT", k, t, "airflow", v, "runtime", out[k][t])
print(f"{total} checks, {bad} differences")
