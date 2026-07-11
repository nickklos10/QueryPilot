from __future__ import annotations

BLOCKED_POSTGRES_FUNCTIONS = {
    "nextval",
    "pg_cancel_backend",
    "pg_ls_dir",
    "pg_read_binary_file",
    "pg_read_file",
    "pg_sleep",
    "pg_terminate_backend",
    "setval",
}
