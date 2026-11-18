"""Import every converted module of a repo and validate the merged definitions (no files are created or removed)."""
import importlib, json, pathlib, sys, warnings
warnings.filterwarnings("ignore")
import dagster as dg
repo = sys.argv[1]; src = (pathlib.Path(__file__).resolve().parent / "conv" / repo / "src"); sys.path.insert(0, str(src))
mods = sorted(p.stem for p in (src / "pkg" / "defs").glob("*.py") if p.stem != "__init__")
assets, jobs, schedules, extra = [], [], [], []
for m in mods:
    mod = importlib.import_module(f"pkg.defs.{m}")
    for v in vars(mod).values():
        if isinstance(v, dg.AssetsDefinition): assets.append(v)
        elif isinstance(v, (dg.ScheduleDefinition,)): schedules.append(v)
        elif type(v).__name__ in ("UnresolvedAssetJobDefinition", "JobDefinition"): jobs.append(v)
        elif type(v).__name__ == "LazyDefinitions" or (callable(v) and getattr(v, "__name__", "") in ("resources", "sources") and m in ("resources", "sources")):
            try: extra.append(v())
            except Exception: pass
n_dag = len([m for m in mods if m not in ("resources", "sources")])
if n_dag == 0: print(f"{repo}: nothing converted"); sys.exit()
defs = dg.Definitions.merge(dg.Definitions(assets=assets, jobs=jobs, schedules=schedules), *extra)
try:
    repo_def = defs.get_repository_def()
    print(f"{repo}: LOADS ({n_dag} DAG modules): {len(assets)} assets, {len(jobs)} jobs, {len(schedules)} schedules")
except Exception as e:
    print(f"{repo}: FAILS ({n_dag} modules): {type(e).__name__}: {str(e)[:160]}")
