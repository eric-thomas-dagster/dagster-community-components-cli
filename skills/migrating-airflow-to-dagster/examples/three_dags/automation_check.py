"""Does eager() on the consumer assets behave like Airflow's `schedule=[processed_data_dataset]`?"""
import sys, warnings; warnings.filterwarnings("ignore")
import dagster as dg
sys.path.insert(0, "src")
from dagster_trial.defs import dataset_pipelines as dp

assets = [dp.fetch_raw_data, dp.process_data, dp.save_processed_data, dp.load_and_analyze, dp.publish_results]
consumers = [dp.load_and_analyze.key, dp.publish_results.key]
defs = dg.Definitions(assets=assets)
inst = dg.DagsterInstance.ephemeral()

def show(label, r):
    print(f"{label:58} " + ", ".join(f"{k.to_user_string().split('/')[-1]}={r.get_num_requested(k)}" for k in consumers))

r1 = dg.evaluate_automation_conditions(defs=defs, instance=inst)
show("tick 1: nothing has run", r1)
dg.materialize([dp.fetch_raw_data, dp.process_data], instance=inst)
r2 = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=r1.cursor)
show("tick 2: producer ran but did NOT update the dataset", r2)
dg.materialize([dp.save_processed_data], instance=inst, selection=None) if False else dg.materialize(
    [dp.fetch_raw_data, dp.process_data, dp.save_processed_data], instance=inst)
r3 = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=r2.cursor)
show("tick 3: producer updated the dataset", r3)
dg.materialize([dp.fetch_raw_data, dp.process_data, dp.save_processed_data, dp.load_and_analyze], instance=inst)
r4 = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=r3.cursor)
show("tick 4: load_and_analyze just ran", r4)
r5 = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=r4.cursor)
show("tick 5: nothing new", r5)
