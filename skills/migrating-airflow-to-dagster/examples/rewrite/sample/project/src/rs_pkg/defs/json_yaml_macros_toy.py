"""Rewrite of Airflow DAG `json_yaml_macros_toy` (parse a JSON string and a YAML string given as params, print them).

Airflow tasks:
- print_json_macro: folded into asset `json_yaml_macros_toy/parsed_configs` (`{{ macros.json.loads(my_json) }}` becomes `json.loads` of the run-config string)
- print_yaml_macro: replaced by asset `json_yaml_macros_toy/parsed_configs` (`{{ macros.yaml.safe_load(my_yaml) }}` becomes `yaml.safe_load` of the run-config string)

Shape: one asset parsing both strings and returning both parsed values. The strings are the DAG's `user_defined_macros` (`my_json`, `my_yaml`), kept as run-config defaults with their real values from the spec. The converter stubs both tasks because its runtime has no `macros.json` or `macros.yaml`. Registry: nothing fits (parsing two strings). No schedule. Not preserved: two task records.
"""
import json

import dagster as dg
import yaml


class ConfigStrings(dg.Config):
    my_json: str = '{"a":"hello", "b":4, "c":true, "d": [ 1, 2, 3, 4, 5 ], "e" : {"e1" : 1, "e2" : "bye"}}'
    my_yaml: str = "a: hello\nb: 4\nc: true\nd:\n  - 1\n  - 2\n  - 3\n  - 4\n  - 5\ne:\n  e1: 1\n  e2: bye"


@dg.asset(key=["json_yaml_macros_toy", "parsed_configs"], group_name="json_yaml_macros_toy", kinds={"python"}, description="The JSON and the YAML run-config strings, parsed.")
def parsed_configs(context: dg.AssetExecutionContext, config: ConfigStrings) -> dict:
    out = {"json": json.loads(config.my_json), "yaml": yaml.safe_load(config.my_yaml)}
    context.log.info(str(out["json"]))
    context.log.info(str(out["yaml"]))
    return out


@dg.asset_check(asset=parsed_configs, name="both_parsed", blocking=False, description="Structural check (not a business check): both strings parsed to something.")
def both_parsed(value: dict) -> dg.AssetCheckResult:
    return dg.AssetCheckResult(passed=all(v is not None for v in value.values()), metadata={"kind": "structural"})


json_yaml_macros_toy_job = dg.define_asset_job("json_yaml_macros_toy", selection=[parsed_configs])
