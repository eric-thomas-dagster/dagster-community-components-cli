import dagster as dg

from ...runtime import rmj_caveat, rmj_config, run_dbt_cloud, run_mail, run_python, run_shell, run_sql  # noqa: F401


@dg.asset(
    name='nightly_sales_load__margin',
    group_name='nightly_sales_load',
    description='Compute margin using a custom script.',
    deps=['nightly_sales_load__sales_fact', 'nightly_sales_load__inventory_fact'],
    kinds={"python"},
    metadata={'runmyjobs_source': 'chain:NIGHTLY_SALES_LOAD/step:post/call:margin', 'runmyjobs_application': 'SALES'},
)
def nightly_sales_load__margin(context: dg.AssetExecutionContext) -> None:
    rmj_caveat('RedwoodScript is not auto-translated; port the source (kept in the asset body) by hand or with the planned LLM pass')
    # --- original RedwoodScript ---
    # Job j = jcsSession.getJobByName("x");
    # jcsOut.println("margin done");


@dg.asset(
    name='legacy_sap_extract',
    group_name='ops',
    kinds={"python"},
    metadata={'runmyjobs_source': 'process:LEGACY_SAP_EXTRACT', 'runmyjobs_application': 'OPS'},
)
def legacy_sap_extract(context: dg.AssetExecutionContext) -> None:
    rmj_caveat('SAP connector job (a RunMyJobs SAP built-in): it needs an SAP integration, in Dagster or through the RunMyJobs connector')
