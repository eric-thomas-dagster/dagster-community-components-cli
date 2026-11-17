from pathlib import Path
from dagfactory import load_yaml_dags
load_yaml_dags(globals_dict=globals(), dags_folder=str(Path(__file__).resolve().parent))
