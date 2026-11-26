# TaskGroup

Tasks that each persist a distinct output: separate assets with `group_name` and `key_prefix` (`task_groups.py`): `group.task` becomes the key `group/task`. A group that only prepares one output folds
into that asset. A group used as a template (the same shape several times, often in a loop): a factory function returning asset definitions (`make_group`). Header: `<group.task>: replaced by asset
`<group/task>``. Nothing is lost beyond the Airflow UI's collapsed group. Decision record (task groups): `preserved` as group_name/key_prefix, or `not preserved: <why>`.

Gotcha (found by running it): an asset's argument name is resolved WITHOUT the key prefix, so a consumer of a prefixed asset names the key: `ins={"pull": dg.AssetIn(key=["extract", "pull"])}`.
