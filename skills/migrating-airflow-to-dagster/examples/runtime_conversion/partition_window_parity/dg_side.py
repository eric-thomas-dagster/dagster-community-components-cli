import importlib.util, json, pathlib
from datetime import datetime
import dagster_community_components as dcc
p = next((pathlib.Path(dcc.__file__).parent / "sensors").glob("partition_window_sensor/component.py"))
spec = importlib.util.spec_from_file_location("pws", p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
af = json.load(open("af_out.json")); bad = 0
FMT = "%Y-%m-%dT%H:%M:%S"
for name, expected in af.items():
    kind, window, ref = name.split()[0], name.split()[1], name.split()[2]
    if kind == "rollup" and window == "day":
        anchor = m._rollup_anchor(datetime.strptime(ref, "%Y-%m-%d"), "day")
        got = sorted(m._expected_upstream_keys(anchor, "day", FMT))
    elif kind == "rollup" and window == "month":
        anchor = m._rollup_anchor(datetime.strptime(ref + "-01", "%Y-%m-%d"), "month")
        got = sorted(m._expected_upstream_keys(anchor, "month", "%Y-%m-%d"))
    else:
        direction = window                                                   # forward | backward
        got = sorted(m._fan_out_keys(datetime.strptime(ref, "%Y-%m-%d"), "week", 7, direction, "%Y-%m-%d"))
    same = got == expected
    bad += not same
    print(f"{name:32} {'same' if same else 'DIFFERENT'}")
    if not same: print("   airflow:", expected[:3], "...", expected[-1], len(expected)); print("   sensor :", got[:3], "...", got[-1], len(got))
print("differences:", bad)
