"""Function-level parity: run the ORIGINAL Airflow task bodies (through a stub `airflow` that makes @task
a pass-through and records return values) and compare them to the Dagster assets' materialized outputs.
This checks task logic and data passing. It does not exercise Airflow's scheduler, XCom serialization or triggers."""
import importlib.util, sys, types, pathlib

OUT = {}                                          # "<file stem>.<func>" -> return value

def _stub():
    air = types.ModuleType("airflow"); dec = types.ModuleType("airflow.decorators")
    class Dataset:                                # noqa
        def __init__(self, uri): self.uri = uri
    def task(f=None, **kw):
        def wrap(fn):
            def inner(*a, **k):
                r = fn(*a, **k); OUT[f"{fn.__module__}.{fn.__name__}"] = r; return r
            inner.__name__ = fn.__name__; return inner
        return wrap(f) if callable(f) else wrap
    def dag(**kw):
        return lambda fn: fn                      # keep the function; the file calls it at import
    air.Dataset = Dataset; dec.task = task; dec.dag = dag
    sys.modules["airflow"] = air; sys.modules["airflow.decorators"] = dec

def run_originals():
    _stub()
    for stem in ("simple_sequential", "dag_with_datasets_producer", "dag_with_datasets_consumer"):
        spec = importlib.util.spec_from_file_location(stem, pathlib.Path(__file__).parent / "airflow_src" / f"{stem}.py")
        mod = importlib.util.module_from_spec(spec); sys.modules[stem] = mod; spec.loader.exec_module(mod)
    return dict(OUT)

if __name__ == "__main__":
    import dagster as dg
    sys.path.insert(0, "dagster_trial/src")
    from dagster_trial.defs import simple_sequential_pipeline as ss, dataset_pipelines as dp
    original = run_originals()
    assets = [ss.fetch_data, ss.process_data, ss.save_results, dp.fetch_raw_data, dp.process_data,
              dp.save_processed_data, dp.load_and_analyze, dp.publish_results]
    res = dg.materialize(assets)
    assert res.success, "dagster materialization failed"
    pairs = [("simple_sequential.fetch_data", ss.fetch_data), ("simple_sequential.process_data", ss.process_data),
             ("simple_sequential.save_results", ss.save_results),
             ("dag_with_datasets_producer.fetch_raw_data", dp.fetch_raw_data),
             ("dag_with_datasets_producer.process_data", dp.process_data),
             ("dag_with_datasets_producer.save_processed_data", dp.save_processed_data),
             ("dag_with_datasets_consumer.load_and_analyze", dp.load_and_analyze),
             ("dag_with_datasets_consumer.publish_results", dp.publish_results)]
    bad = 0
    for name, a in pairs:
        got = res.asset_value(a.key); want = original[name]
        ok = got == want; bad += not ok
        print(f"{'OK  ' if ok else 'DIFF'} {name:52} {str(got)[:60]}")
    print("PARITY:", "all 8 tasks match" if not bad else f"{bad} mismatches")
