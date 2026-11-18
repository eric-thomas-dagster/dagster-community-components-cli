import importlib.util, pathlib, json
import dagster as dg
import dagster_community_components as dcc
p = next((pathlib.Path(dcc.__file__).parent / "assets").glob("*/shell_command_asset/component.py"))
spec = importlib.util.spec_from_file_location("sh", p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cmds = ["echo 1", "echo 1.5", "echo true", "echo null", "echo '[1, 2]'", "echo '{\"a\": 1}'", "echo 007", "echo hello", "echo 1e3", "echo a; echo last", "true"]
expected_airflow3 = {"echo 1": 1, "echo 1.5": 1.5, "echo true": True, "echo null": None, "echo '[1, 2]'": [1, 2], "echo '{\"a\": 1}'": {"a": 1}, "echo 007": "007", "echo hello": "hello", "echo 1e3": 1000.0, "echo a; echo last": "last", "true": None}
bad = 0
for c in cmds:
    comp = m.ShellCommandAssetComponent(asset_name="x", command=c, return_last_stdout_line=True, parse_json_output=True)
    d = comp.build_defs(context=None).assets[0]
    r = dg.materialize([d], raise_on_error=False)
    try: v = r.asset_value(d.key)
    except Exception as e: v = f"<{type(e).__name__}>"
    ok = v == expected_airflow3[c] and type(v) == type(expected_airflow3[c])
    bad += not ok
    print(f"{c:22} component={v!r:22} airflow3={expected_airflow3[c]!r:22} {'same' if ok else 'DIFFERENT'}")
print("differences:", bad)
