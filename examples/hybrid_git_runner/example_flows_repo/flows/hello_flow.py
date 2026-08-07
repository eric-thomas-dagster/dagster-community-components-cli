"""Example Prefect flow — HybridRunnerComponent picks this up from git."""
from prefect import flow, task


@task
def greet(name: str) -> str:
    return f"hello, {name}"


@flow
def hello_flow(name: str = "hybrid runner") -> str:
    return greet(name)


if __name__ == "__main__":
    hello_flow()
