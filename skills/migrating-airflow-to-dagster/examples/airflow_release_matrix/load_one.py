import importlib, json, os, re, sys, warnings
warnings.filterwarnings("ignore")
src = sys.argv[1]; sys.path.insert(0, src)
import dagster as dg
rep = json.load(open(os.path.join(os.path.dirname(src), "report.json")))["dags"]
safe = lambda n: re.sub(r"\W", "_", n) if not re.sub(r"\W", "_", n)[:1].isdigit() else "_" + re.sub(r"\W", "_", n)
bad = {}
for did, v in rep.items():
    if v["status"] != "converted": continue
    try:
        mod = importlib.import_module(f"pkg.defs.{safe(did)}")
        dg.Definitions(assets=[x for x in vars(mod).values() if isinstance(x, dg.AssetsDefinition)])
    except Exception as e:
        bad[did] = f"{type(e).__name__}: {str(e)[:100]}"
print(json.dumps(bad))
