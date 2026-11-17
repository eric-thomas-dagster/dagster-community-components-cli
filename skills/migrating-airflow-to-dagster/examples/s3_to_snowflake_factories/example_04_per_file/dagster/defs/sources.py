"""Landed files in S3: a shared upstream asset. Any number of definitions may depend on it; none produces it."""
import dagster as dg


@dg.definitions
def sources() -> dg.Definitions:
    return dg.Definitions(assets=[
        dg.AssetSpec(key=["s3", "example-landing-bucket", "orders"], kinds={"s3"},
                     description="Files landed under s3://example-landing-bucket/orders/<yyyymmdd>/ by upstream systems")
    ])
