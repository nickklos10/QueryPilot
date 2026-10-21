"""Functions rejected by default: file/network access, side effects, sleeps and locks.

The default policy blocks the union of every dialect's set: a function name that is
dangerous on one engine is rarely needed for analytics on another.
"""
from __future__ import annotations

BLOCKED_POSTGRES_FUNCTIONS = {
    # Sequences and backend control
    "nextval",
    "setval",
    "pg_cancel_backend",
    "pg_terminate_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    # set_config can switch off statement_timeout for the transaction
    "set_config",
    # Server filesystem
    "pg_ls_dir",
    "pg_ls_logdir",
    "pg_ls_waldir",
    "pg_read_binary_file",
    "pg_read_file",
    "pg_stat_file",
    # Large objects read and write server files
    "lo_create",
    "lo_export",
    "lo_from_bytea",
    "lo_import",
    "lo_put",
    "lo_unlink",
    # Other databases
    "dblink",
    "dblink_connect",
    "dblink_exec",
    "dblink_open",
    "dblink_send_query",
    # Run a query string, bypassing table and column policies
    "cursor_to_xml",
    "database_to_xml",
    "query_to_xml",
    "query_to_xml_and_xmlschema",
    "query_to_xmlschema",
    "schema_to_xml",
    "table_to_xml",
    "table_to_xml_and_xmlschema",
    # Sleeps and session-level locks that outlive the rolled-back transaction
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_try_advisory_lock",
}

BLOCKED_DUCKDB_FUNCTIONS = {
    # Local, HTTP and S3 file readers
    "glob",
    "parquet_metadata",
    "parquet_scan",
    "parquet_schema",
    "read_blob",
    "read_csv",
    "read_csv_auto",
    "read_json",
    "read_json_auto",
    "read_json_objects",
    "read_json_objects_auto",
    "read_ndjson",
    "read_ndjson_auto",
    "read_ndjson_objects",
    "read_parquet",
    "read_text",
    "read_xlsx",
    "sniff_csv",
    "delta_scan",
    "iceberg_metadata",
    "iceberg_scan",
    # Other databases
    "mysql_execute",
    "mysql_query",
    "postgres_execute",
    "postgres_query",
    "postgres_scan",
    "sqlite_scan",
    # Run a query string, bypassing table and column policies
    "query",
    "query_table",
    # Process environment and stored credentials
    "duckdb_secrets",
    "getenv",
}

BLOCKED_MYSQL_FUNCTIONS = {
    "benchmark",
    "get_lock",
    "is_free_lock",
    "is_used_lock",
    "load_file",
    "master_pos_wait",
    "release_all_locks",
    "release_lock",
    "sleep",
    "source_pos_wait",
    # Common UDFs that run shell commands
    "sys_eval",
    "sys_exec",
}

BLOCKED_TSQL_FUNCTIONS = {
    "opendatasource",
    "openquery",
    "openrowset",
    "xp_cmdshell",
    "xp_dirtree",
    "xp_fileexist",
    "xp_regread",
}

BLOCKED_SQLITE_FUNCTIONS = {
    "edit",
    "fts3_tokenizer",
    "load_extension",
    "readfile",
    "writefile",
}

BLOCKED_FUNCTIONS_BY_DIALECT: dict[str, set[str]] = {
    "duckdb": BLOCKED_DUCKDB_FUNCTIONS,
    "mysql": BLOCKED_MYSQL_FUNCTIONS,
    "postgres": BLOCKED_POSTGRES_FUNCTIONS,
    "sqlite": BLOCKED_SQLITE_FUNCTIONS,
    "tsql": BLOCKED_TSQL_FUNCTIONS,
}

DEFAULT_BLOCKED_FUNCTIONS: set[str] = set().union(*BLOCKED_FUNCTIONS_BY_DIALECT.values())
