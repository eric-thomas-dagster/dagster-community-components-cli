# XCom

A small value the next asset needs: return it and take it as an ordinary asset argument (`xcom.py`). A value that is only for observability: `context.add_output_metadata` / `Output(metadata=...)`: shown in
the UI, not passed on. Tasks folded into one asset use local variables. Keep a value out of metadata if it is large or sensitive. XCom pulled by `key` other than `return_value` or by `task_ids` of several
tasks: return a dict keyed by those names. Decision record has no XCom line unless a pull cannot be mapped (then `not preserved: <why>`).
