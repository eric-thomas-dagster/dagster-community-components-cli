"""Run every ADF template in a folder through the converter, one factory per file (or per git-integration folder), and report.

Usage: python -I tools/corpus.py <dir> [--verbose]

Per factory: load, convert, write the project, import the generated module (gate 1 and a load check), and dry-run every generated pipeline
function in-process with ADF_DRY_RUN=1. Prints totals, the reasons activities stay stubs, and every crash. Needs the dev environment
(dagster) because the generated module imports it: `uv run python tools/corpus.py <dir>`.
"""
import collections
import importlib
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ["ADF_DRY_RUN"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from adf_to_dagster.convert import convert, write_project   # noqa: E402
from adf_to_dagster.loader import load                     # noqa: E402


def units(root: Path):
    """Each ARM template file is a factory; a folder holding pipeline/ is a git-integration factory."""
    seen = set()
    for d in sorted(root.rglob("pipeline")):
        if d.is_dir():
            seen.add(d.parent)
            yield d.parent
    for f in sorted(root.rglob("*.json")):
        if any(p in seen for p in f.parents) or f.name == "manifest.json":
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("resources"), list) and any("pipelines" in str(r.get("type", "")) for r in doc["resources"] if isinstance(r, dict)):
            yield f


def main():
    root = Path(sys.argv[1])
    verbose = "--verbose" in sys.argv
    tot = collections.Counter()
    reasons = collections.Counter()
    acts = collections.Counter()
    crashes = []
    n = 0
    for u in units(root):
        n += 1
        try:
            f = load(u)
            if not f.pipelines:
                tot["factories without pipelines"] += 1
                continue
            res = convert(f, pkg=f"corp{n}")
            for i in res.items:
                tot[f"{i.kind}:{i.status}"] += 1
            for m in res.meta["pipelines"].values():
                for t, name, why in m["stubs"]:
                    reasons[(t, why.split(":")[0][:90] if t != "Copy" else why[:100])] += 1
            with tempfile.TemporaryDirectory() as d:
                write_project(res, Path(d), f"corp{n}")
                sys.path.insert(0, str(Path(d) / "src"))
                try:
                    mod = importlib.import_module(f"corp{n}.defs.adf")
                finally:
                    sys.path.pop(0)
                for name, m in res.meta["pipelines"].items():
                    if m.get("split"):
                        tot["pipelines split"] += 1
                        continue
                    fn = getattr(mod, f"run_{m['asset']}")
                    try:
                        fn({}, log=lambda s: None)
                        tot["dry run ok"] += 1
                    except Exception as e:             # noqa: BLE001
                        cls = type(e).__name__
                        if cls == "AdfCaveat":
                            tot["dry run stub (ADF-CAVEAT)"] += 1
                        elif cls == "AdfFail":
                            tot["dry run Fail activity"] += 1
                        else:
                            tot["dry run UNEXPECTED"] += 1
                            crashes.append((str(u), name, f"{cls}: {str(e)[:200]}"))
        except Exception as e:                         # noqa: BLE001
            tot["CRASH"] += 1
            crashes.append((str(u), "", "".join(traceback.format_exception_only(type(e), e)).strip()[:300] + ("\n" + traceback.format_exc()[-600:] if verbose else "")))
    print(f"factories: {n}")
    for k, v in sorted(tot.items()):
        print(f"  {v:5d} {k}")
    print("\nwhy activities stay stubs (top 25)")
    for (t, why), c in reasons.most_common(25):
        print(f"  {c:4d} {t}: {why}")
    if crashes:
        print(f"\nproblems ({len(crashes)})")
        for c in crashes[:40]:
            print("  ", *c)


main()
