"""The code that runs inside the task's own interpreter (it imports only dagster_pipes, plus whatever the function needs).

Parameters arrive as Pipes extras (JSON, no pickling). Logs and metadata stream back while it runs; the return value goes back as a custom message
(the same wire shape as the converter's generated wrapper script: `{"return_value": ...}`)."""
from dagster_pipes import open_dagster_pipes


def summarize(n: int, label: str = "rows") -> dict:
    return {"label": label, "count": n, "total": sum(range(n))}


if __name__ == "__main__":
    with open_dagster_pipes() as pipes:
        n = int(pipes.get_extra("n"))
        pipes.log.info(f"running in the task's own interpreter with n={n}")
        result = summarize(n, pipes.get_extra("label") or "rows")
        pipes.report_asset_materialization(metadata={"rows": n})            # streamed back as it happens
        pipes.report_custom_message({"return_value": result})               # JSON only: Airflow pickled this, Pipes does not
