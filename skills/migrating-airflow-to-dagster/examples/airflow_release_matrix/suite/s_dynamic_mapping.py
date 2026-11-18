from compat_lib import *
if VERSION >= (2, 3):
    with dag("s_dynamic_mapping") as d:
        @task
        def items():
            return ["a", "b"]
        @task
        def one(x):
            return x
        @task
        def collect(xs):
            return len(list(xs))
        collect(one.expand(x=items()))
