"""Landed files in S3: shared upstream assets. Any number of definitions may depend on these; none produces them."""
import dagster as dg


@dg.definitions
def sources() -> dg.Definitions:
    return dg.Definitions(assets=[
        dg.AssetSpec(key=["s3", "example-landing-bucket", name], kinds={"s3"},
                     description=f"Files landed under s3://example-landing-bucket/{name}/<yyyymmdd>/ by upstream systems")
        for name in ("orders", "customers", "products")
    ])
