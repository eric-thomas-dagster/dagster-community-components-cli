"""Pattern: TaskGroup. A group of tasks that each persist a distinct output: distinct assets with `group_name` (and `key_prefix` for the group's namespace); the Airflow task id `group.task` becomes the key
`group/task`. A TaskGroup that is only a reusable template (the same shape instantiated several times): a Python factory that returns asset (or graph_asset) definitions. A group whose tasks only prepare one
output folds into that output's asset: no group is needed."""
import dagster as dg


@dg.asset(group_name="extract", key_prefix=["extract"])
def pull() -> list[int]:
    return [1, 2, 3]


@dg.asset(group_name="extract", key_prefix=["extract"], ins={"pull": dg.AssetIn(key=["extract", "pull"])})            # an argument name is looked up WITHOUT the prefix: name the key
def clean(pull: list[int]) -> list[int]:
    return [x for x in pull if x > 1]


def make_group(name: str, factor: int) -> list:
    """A TaskGroup used as a template: one call per instance."""
    @dg.asset(name=f"{name}_scaled", group_name=name, key_prefix=[name], ins={"clean": dg.AssetIn(key=["extract", "clean"])})
    def scaled(clean: list[int]) -> list[int]:
        return [x * factor for x in clean]
    return [scaled]


defs = dg.Definitions(assets=[pull, clean, *make_group("a", 2), *make_group("b", 3)])
