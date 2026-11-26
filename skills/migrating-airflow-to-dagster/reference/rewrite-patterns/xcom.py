"""Pattern: XCom. A small value the NEXT asset needs: return it, and take it as an ordinary asset argument (the IO manager is the XCom). A value that is only for observability: attach it as metadata
(`dg.Output(value, metadata=...)`, `dg.MaterializeResult(metadata=...)` or `context.add_output_metadata`), which is shown in the UI and is not passed on. Tasks folded into one asset use local variables."""
import dagster as dg


@dg.asset
def producer() -> dict:
    return {"rows": 3}                                                  # xcom_push / return value consumed by the next asset


@dg.asset
def consumer(producer: dict) -> int:
    return producer["rows"] * 2                                         # xcom_pull -> an argument


@dg.asset
def observed_only(context: dg.AssetExecutionContext) -> None:
    context.add_output_metadata({"row_count": 3, "source": "api"})      # a value nobody consumes: metadata, not a return value


@dg.asset
def with_output_metadata() -> dg.Output:
    return dg.Output(7, metadata={"note": "also passed to consumers"})


defs = dg.Definitions(assets=[producer, consumer, observed_only, with_output_metadata])
