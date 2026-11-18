import json
a, d = json.load(open("af_out.json")), json.load(open("dg_out.json"))
bad = 0
for k in a:
    if k.startswith("_"): continue
    ac, dc = a[k], d.get(k, {})
    a_err, d_err = "error" in ac, "error" in dc or dc.get("success") is False
    same_calls = ac["calls"] == dc.get("calls")
    same_result = ac.get("result") is None or ac.get("result") == dc.get("result")      # the component may return no value where Airflow returns None
    status = "same" if same_calls and a_err == d_err and (a_err or same_result) else "DIFFERENT"
    bad += status != "same"
    print(f"{k:34} {status}")
    if status != "same":
        print("   airflow:", json.dumps(ac)[:600]); print("   dagster:", json.dumps(dc)[:600])
print("differences:", bad)
