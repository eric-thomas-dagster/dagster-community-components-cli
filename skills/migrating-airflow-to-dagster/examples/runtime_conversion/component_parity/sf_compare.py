import json, re
a, d = json.load(open("sf_af_out.json")), json.load(open("sf_dg_out.json"))


def norm(sql):
    s = re.sub(r"\s+", " ", sql).strip()
    s = re.sub(r"\s*=\s*", "=", s)
    s = re.sub(r",\s+", ",", s)
    s = s.replace("@ST/ ", "@ST ").replace("DB.RAW.", "RAW.")        # database qualifier is added by the component, the operator sets it on the session
    return s.upper()


bad = 0
for k in a:
    same = norm(a[k]["sql"]) == norm(d[k]["sql"])
    bad += not same
    print(f"{k:30} {'same after whitespace/case normalization' if same else 'DIFFERENT'}")
    if not same:
        print("   airflow:", norm(a[k]["sql"])); print("   dagster:", norm(d[k]["sql"]))
print("differences:", bad)
