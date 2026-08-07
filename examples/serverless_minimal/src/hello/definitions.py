"""The absolute minimum for a Dagster+ Serverless code location.

Two assets. Zero components. Deploys with one CLI command. This is
Dagster's floor — the least surface area you need to ship SOMETHING
to Dagster+ Serverless. Compare against the sibling `agentic_tour/`
project for what the pattern looks like at production scale.
"""
import dagster as dg


@dg.asset
def hello() -> str:
    return "hello from dagster+ serverless"


@dg.asset
def shout(hello: str) -> str:
    return hello.upper()


defs = dg.Definitions(assets=[hello, shout])
