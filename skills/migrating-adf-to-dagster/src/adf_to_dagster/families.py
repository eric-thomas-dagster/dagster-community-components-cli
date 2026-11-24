"""Which ADF source, sink and linked-service types are reached as SQL (through a SQLAlchemy URL the user supplies, with the driver they install)."""

# SQL Server family (seen in real exports) plus databases SQLAlchemy reaches the same way. The non-Microsoft names are from ADF's connector list, not from
# an export, except AzurePostgreSql and Oracle, which appear in the public repos.
SQL_SOURCES = {"AzureSqlSource", "SqlServerSource", "SqlDWSource", "SqlMISource", "SqlSource",
               "AzurePostgreSqlSource", "PostgreSqlSource", "PostgreSqlV2Source", "AzureMySqlSource", "MySqlSource", "OracleSource", "SnowflakeSource",
               "SnowflakeV2Source"}
MSSQL_SOURCES = {"AzureSqlSource", "SqlServerSource", "SqlDWSource", "SqlMISource", "SqlSource"}      # the sources whose stored procedures are called with EXEC
SQL_SINKS = {"AzureSqlSink", "SqlServerSink", "SqlMISink", "SqlSink", "SqlDWSink",
             "AzurePostgreSqlSink", "AzureMySqlSink", "OracleSink", "SnowflakeSink", "SnowflakeV2Sink"}
SQL_LS = {"AzureSqlDatabase", "SqlServer", "AzureSqlDW", "AzureSqlMI", "AzureSynapseAnalytics",
          "AzurePostgreSql", "PostgreSql", "PostgreSqlV2", "AzureMySql", "MySql", "Oracle", "OracleV2", "Snowflake", "SnowflakeV2"}


def source_query(src: dict):
    """The query a SQL source carries (SQL Server family: sqlReaderQuery; the others: query)."""
    return src.get("sqlReaderQuery") or src.get("query")
