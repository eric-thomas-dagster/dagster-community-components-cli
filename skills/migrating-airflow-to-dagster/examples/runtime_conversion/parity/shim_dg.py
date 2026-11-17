import json, sys, tempfile, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
import shim_scenarios as S
exec(open(sys.argv[1]).read(), globals())     # the generated runtime.py
hook = S3Hook("aws_default"); hook._client = S.FakeClient()
out = S.run(hook)
json.dump(out, open("shim_dg.json", "w"), indent=1); print("shim scenarios:", len(out))
