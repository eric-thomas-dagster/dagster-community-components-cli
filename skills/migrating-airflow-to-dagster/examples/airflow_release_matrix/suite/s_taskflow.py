from compat_lib import *
with dag("s_taskflow") as d:
    @task
    def extract():
        return [1, 2, 3]
    @task
    def total(xs):
        return sum(xs)
    total(extract())
