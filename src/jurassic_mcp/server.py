"""FastMCP server definition with all Informix tools."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP

from .catalog import (
    describe_table as _describe_table,
    list_databases as _list_databases,
    list_foreign_keys as _list_foreign_keys,
    list_indexes as _list_indexes,
    list_tables as _list_tables,
    validate_sql as _validate_sql,
)
from .config import AppConfig
from .db import connect

mcp = FastMCP("jurassic-mcp")

# The config is injected at startup via ``init_server``.
_config: AppConfig | None = None


def init_server(config: AppConfig) -> None:
    """Store the loaded configuration so that tools can access it."""
    global _config  # noqa: PLW0603
    _config = config


def _cfg() -> AppConfig:
    """Return the current config, raising if not initialized."""
    if _config is None:
        raise RuntimeError("Server not initialized — call init_server(config) first")
    return _config


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={"readOnlyHint": True},
    tags={"informix", "schema"},
)
def list_databases() -> str:
    """List all databases available in the Informix instance.

    Returns a JSON array with each database's name, owner, and an optional
    user-provided description from the configuration file.
    """
    cfg = _cfg()
    with connect(cfg.informix, "sysmaster") as conn:
        rows = _list_databases(conn)

    # Enrich with descriptions from config
    result: list[dict[str, Any]] = []
    for row in rows:
        db_name = row["name"].strip() if isinstance(row["name"], str) else row["name"]
        entry: dict[str, Any] = {
            "name": db_name,
            "owner": row.get("owner", "").strip()
            if isinstance(row.get("owner"), str)
            else row.get("owner"),
        }
        desc = cfg.get_database_description(db_name)
        if desc:
            entry["description"] = desc
        result.append(entry)

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool(
    annotations={"readOnlyHint": True},
    tags={"informix", "schema"},
)
def list_tables(database: str) -> str:
    """List user tables in an Informix database.

    Args:
        database: Name of the Informix database to query.

    Returns a JSON array with table name, owner, column count, row count,
    and an optional user-provided description.
    """
    cfg = _cfg()
    with connect(cfg.informix, database) as conn:
        rows = _list_tables(conn)

    result: list[dict[str, Any]] = []
    for row in rows:
        tabname = (
            row["tabname"].strip()
            if isinstance(row["tabname"], str)
            else row["tabname"]
        )
        entry: dict[str, Any] = {
            "name": tabname,
            "owner": row.get("owner", "").strip()
            if isinstance(row.get("owner"), str)
            else row.get("owner"),
            "num_columns": row.get("ncols"),
            "num_rows": row.get("nrows"),
        }
        desc = cfg.get_table_description(database, tabname)
        if desc:
            entry["description"] = desc
        result.append(entry)

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool(
    annotations={"readOnlyHint": True},
    tags={"informix", "schema"},
)
def describe_table(database: str, table: str) -> str:
    """Describe the structure (columns) of a table in an Informix database.

    Args:
        database: Name of the Informix database.
        table: Name of the table to describe.

    Returns a JSON object with the table description and a list of columns
    including name, type, length, nullable flag, and optional user-provided
    column descriptions.
    """
    cfg = _cfg()
    with connect(cfg.informix, database) as conn:
        columns = _describe_table(conn, table)

    # Enrich with user descriptions
    col_descs = cfg.get_column_descriptions(database, table)
    for col in columns:
        user_desc = col_descs.get(col["name"], "")
        if user_desc:
            col["description"] = user_desc

    result: dict[str, Any] = {
        "table": table,
        "database": database,
        "columns": columns,
    }
    table_desc = cfg.get_table_description(database, table)
    if table_desc:
        result["description"] = table_desc

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool(
    annotations={"readOnlyHint": True},
    tags={"informix", "schema"},
)
def list_indexes(database: str, table: str) -> str:
    """List indexes defined on a table in an Informix database.

    Args:
        database: Name of the Informix database.
        table: Name of the table.

    Returns a JSON array of indexes with name, type (unique/duplicates),
    and the columns they cover with sort direction.
    """
    cfg = _cfg()
    with connect(cfg.informix, database) as conn:
        indexes = _list_indexes(conn, table)

    return json.dumps(indexes, indent=2, ensure_ascii=False)


@mcp.tool(
    annotations={"readOnlyHint": True},
    tags={"informix", "schema"},
)
def list_foreign_keys(database: str, table: str) -> str:
    """List foreign key constraints on a table in an Informix database.

    Args:
        database: Name of the Informix database.
        table: Name of the table.

    Returns a JSON array of foreign keys with constraint name, referenced table,
    delete rule, and the columns involved on both sides.
    """
    cfg = _cfg()
    with connect(cfg.informix, database) as conn:
        fks = _list_foreign_keys(conn, table)

    return json.dumps(fks, indent=2, ensure_ascii=False)


@mcp.tool(
    annotations={"readOnlyHint": True},
    tags={"informix", "sql"},
)
def validate_sql(database: str, sql: str) -> str:
    """Validate SQL syntax against an Informix database without executing it.

    The SQL statement is compiled (prepared) but never run. This checks both
    syntax and semantic validity (table/column existence, types, etc.).

    Only single statements are accepted — multiple statements separated by
    semicolons will be rejected.

    Args:
        database: Name of the Informix database to validate against.
        sql: The SQL statement to validate.

    Returns a JSON object with ``valid`` (bool) and, when invalid, an
    ``error`` message from Informix.
    """
    cfg = _cfg()
    with connect(cfg.informix, database) as conn:
        result = _validate_sql(conn, sql)

    return json.dumps(result, indent=2, ensure_ascii=False)
