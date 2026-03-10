"""Informix system catalog queries and type mappings."""

from __future__ import annotations

from typing import Any

import IfxPy as ifx  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Informix coltype numeric code → human-readable name
# The raw coltype value from syscolumns may have +256 to indicate NOT NULL.
# ---------------------------------------------------------------------------
COLTYPE_MAP: dict[int, str] = {
    0: "CHAR",
    1: "SMALLINT",
    2: "INTEGER",
    3: "FLOAT",
    4: "SMALLFLOAT",
    5: "DECIMAL",
    6: "SERIAL",
    7: "DATE",
    8: "MONEY",
    9: "NULL",
    10: "DATETIME",
    11: "BYTE",
    12: "TEXT",
    13: "VARCHAR",
    14: "INTERVAL",
    15: "NCHAR",
    16: "NVARCHAR",
    17: "INT8",
    18: "SERIAL8",
    19: "SET",
    20: "MULTISET",
    21: "LIST",
    22: "ROW",
    23: "COLLECTION",
    40: "LVARCHAR",
    41: "BOOLEAN",
    43: "BIGINT",
    44: "BIGSERIAL",
    52: "IDSSECURITYLABEL",
    256: "CHAR NOT NULL (alias)",  # placeholder — handled via bitmask
}


def _safe_str(value: Any) -> str:
    """Return a stripped string representation for mixed driver values."""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value)


def _safe_int(value: Any, default: int = 0) -> int:
    """Safely coerce mixed driver values to int with a default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ensure_stmt(stmt: Any, sql: str) -> Any:
    """Raise a clear error when statement preparation/execution fails."""
    if not stmt:
        err_msg = ""
        try:
            err_msg = str(ifx.stmt_errormsg())
        except Exception:
            err_msg = ""
        if err_msg:
            raise RuntimeError(
                f"Informix statement error: {err_msg} (SQL: {sql.strip()})"
            )
        raise RuntimeError(
            f"Informix statement returned empty handle (SQL: {sql.strip()})"
        )
    return stmt


def decode_coltype(raw_coltype: int) -> tuple[str, bool]:
    """Decode an Informix ``coltype`` value.

    Returns ``(type_name, nullable)``.
    If the high bit (256) is set the column is NOT NULL.
    """
    nullable = (raw_coltype & 256) == 0
    base = raw_coltype & 0xFF
    type_name = COLTYPE_MAP.get(base, f"UNKNOWN({base})")
    return type_name, nullable


# ---------------------------------------------------------------------------
# Helper to fetch all rows from a statement as list[dict]
# ---------------------------------------------------------------------------


def _fetch_all(stmt) -> list[dict[str, Any]]:
    """Fetch all rows from an executed IfxPy statement as dicts."""
    rows: list[dict[str, Any]] = []
    row = ifx.fetch_assoc(stmt)
    while isinstance(row, dict):
        rows.append(dict(row))
        row = ifx.fetch_assoc(stmt)
    return rows


# ---------------------------------------------------------------------------
# Catalog queries
# ---------------------------------------------------------------------------


def list_databases(conn) -> list[dict[str, Any]]:
    """List databases in the Informix instance.

    Must be connected to ``sysmaster``.
    """
    sql = """
        SELECT name, owner
        FROM sysdatabases
        ORDER BY name
    """
    stmt = _ensure_stmt(ifx.exec_immediate(conn, sql), sql)
    return _fetch_all(stmt)


def list_tables(conn) -> list[dict[str, Any]]:
    """List user tables (tabid >= 100, tabtype = 'T') in the current database."""
    sql = """
        SELECT tabname, owner, tabid, ncols, nrows
        FROM systables
        WHERE tabid >= 100 AND tabtype = 'T'
        ORDER BY tabname
    """
    stmt = _ensure_stmt(ifx.exec_immediate(conn, sql), sql)
    return _fetch_all(stmt)


def describe_table(conn, table: str) -> list[dict[str, Any]]:
    """Return column metadata for *table*."""
    sql = """
        SELECT c.colname, c.colno, c.coltype, c.collength
        FROM syscolumns c
        INNER JOIN systables t ON c.tabid = t.tabid
        WHERE LOWER(t.tabname) = LOWER(?)
        ORDER BY c.colno
    """
    stmt = _ensure_stmt(ifx.prepare(conn, sql), sql)
    ifx.execute(stmt, (table,))
    raw_rows = _fetch_all(stmt)

    columns: list[dict[str, Any]] = []
    for row in raw_rows:
        type_name, nullable = decode_coltype(int(row["coltype"]))
        columns.append(
            {
                "name": _safe_str(row.get("colname")),
                "position": _safe_int(row.get("colno")),
                "type": type_name,
                "length": _safe_int(row.get("collength")),
                "nullable": nullable,
            }
        )
    return columns


def list_indexes(conn, table: str) -> list[dict[str, Any]]:
    """Return indexes for *table*, resolving column numbers to names."""
    # 1) Get columns for this table to resolve part1..part16
    col_sql = """
        SELECT c.colno, c.colname
        FROM syscolumns c
        INNER JOIN systables t ON c.tabid = t.tabid
        WHERE LOWER(t.tabname) = LOWER(?)
    """
    col_stmt = _ensure_stmt(ifx.prepare(conn, col_sql), col_sql)
    ifx.execute(col_stmt, (table,))
    col_rows = _fetch_all(col_stmt)
    col_map = {_safe_int(r.get("colno")): _safe_str(r.get("colname")) for r in col_rows}

    # 2) Get indexes
    idx_sql = """
        SELECT i.idxname, i.idxtype,
               i.part1, i.part2, i.part3, i.part4,
               i.part5, i.part6, i.part7, i.part8,
               i.part9, i.part10, i.part11, i.part12,
               i.part13, i.part14, i.part15, i.part16
        FROM sysindexes i
        INNER JOIN systables t ON i.tabid = t.tabid
        WHERE LOWER(t.tabname) = LOWER(?)
    """
    idx_stmt = _ensure_stmt(ifx.prepare(conn, idx_sql), idx_sql)
    ifx.execute(idx_stmt, (table,))
    raw_indexes = _fetch_all(idx_stmt)

    indexes: list[dict[str, Any]] = []
    for idx in raw_indexes:
        idx_name = _safe_str(idx.get("idxname"))
        idx_type = "unique" if _safe_str(idx.get("idxtype")) == "U" else "duplicates"

        # Resolve part columns
        idx_columns: list[dict[str, str]] = []
        for i in range(1, 17):
            part_val = _safe_int(idx.get(f"part{i}"), 0)
            if part_val == 0:
                break
            direction = "ASC" if part_val > 0 else "DESC"
            col_no = abs(part_val)
            col_name = col_map.get(col_no, f"col#{col_no}")
            idx_columns.append({"column": col_name, "direction": direction})

        indexes.append(
            {
                "name": idx_name,
                "type": idx_type,
                "columns": idx_columns,
            }
        )
    return indexes


def list_foreign_keys(conn, table: str) -> list[dict[str, Any]]:
    """Return foreign key constraints for *table*."""
    sql = """
        SELECT c.constrname, c.constrid, c.tabid,
               pt.tabname AS ref_table,
               r.delrule,
               r.primary AS pk_constrid
        FROM sysreferences r
        INNER JOIN sysconstraints c ON r.constrid = c.constrid
        INNER JOIN systables t ON c.tabid = t.tabid
        INNER JOIN sysconstraints pc ON r.primary = pc.constrid
        INNER JOIN systables pt ON pc.tabid = pt.tabid
        WHERE LOWER(t.tabname) = LOWER(?)
    """
    stmt = _ensure_stmt(ifx.prepare(conn, sql), sql)
    ifx.execute(stmt, (table,))
    raw_rows = _fetch_all(stmt)

    fks: list[dict[str, Any]] = []
    for row in raw_rows:
        delrule = _safe_str(row.get("delrule"))
        delete_rule = "CASCADE" if delrule == "C" else "RESTRICT"
        fk_name = _safe_str(row.get("constrname"))
        ref_table = _safe_str(row.get("ref_table"))

        # Try to resolve FK columns via the index associated with the constraint
        fk_columns = _resolve_constraint_columns(
            conn, _safe_int(row.get("constrid")), _safe_int(row.get("tabid"))
        )
        ref_columns = _resolve_constraint_columns(
            conn, _safe_int(row.get("pk_constrid")), None
        )

        fks.append(
            {
                "name": fk_name,
                "referenced_table": ref_table,
                "delete_rule": delete_rule,
                "columns": fk_columns,
                "referenced_columns": ref_columns,
            }
        )
    return fks


def _resolve_constraint_columns(conn, constrid: int, tabid: int | None) -> list[str]:
    """Resolve column names for a constraint via its associated index."""
    # Get the index name from sysconstraints
    sql = """
        SELECT c.idxname, c.tabid
        FROM sysconstraints c
        WHERE c.constrid = ?
    """
    stmt = _ensure_stmt(ifx.prepare(conn, sql), sql)
    ifx.execute(stmt, (constrid,))
    row = ifx.fetch_assoc(stmt)
    if not isinstance(row, dict):
        return []

    idx_name = _safe_str(row.get("idxname"))
    effective_tabid = tabid if tabid is not None else _safe_int(row.get("tabid"))

    # Get part columns from sysindexes
    idx_sql = """
        SELECT part1, part2, part3, part4,
               part5, part6, part7, part8,
               part9, part10, part11, part12,
               part13, part14, part15, part16
        FROM sysindexes
        WHERE idxname = ?
    """
    idx_stmt = _ensure_stmt(ifx.prepare(conn, idx_sql), idx_sql)
    ifx.execute(idx_stmt, (idx_name,))
    idx_row = ifx.fetch_assoc(idx_stmt)
    if not isinstance(idx_row, dict):
        return []

    # Build column map for the table
    col_sql = """
        SELECT colno, colname FROM syscolumns WHERE tabid = ?
    """
    col_stmt = _ensure_stmt(ifx.prepare(conn, col_sql), col_sql)
    ifx.execute(col_stmt, (effective_tabid,))
    col_rows = _fetch_all(col_stmt)
    col_map = {_safe_int(r.get("colno")): _safe_str(r.get("colname")) for r in col_rows}

    columns: list[str] = []
    for i in range(1, 17):
        part_val = _safe_int(idx_row.get(f"part{i}"), 0)
        if part_val == 0:
            break
        col_no = abs(part_val)
        columns.append(col_map.get(col_no, f"col#{col_no}"))

    return columns


def validate_sql(conn, sql: str) -> dict[str, Any]:
    """Validate SQL syntax by preparing without executing.

    Returns ``{"valid": True}`` or ``{"valid": False, "error": "..."}``
    """
    # Safety: reject multiple statements
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        return {
            "valid": False,
            "error": "Multiple SQL statements are not allowed. Submit one statement at a time.",
        }

    if not stripped:
        return {"valid": False, "error": "SQL cannot be empty."}

    first_token = stripped.split(None, 1)[0].upper()
    if first_token not in {"SELECT", "WITH", "VALUES"}:
        return {
            "valid": False,
            "error": (
                "Only read-only statements are allowed in validate_sql "
                "(SELECT, WITH, VALUES)."
            ),
        }

    try:
        stmt = ifx.prepare(conn, sql)
        if not stmt or isinstance(stmt, bool):
            return {"valid": False, "error": str(ifx.stmt_errormsg())}

        # For ibm_db + Informix, errors are often raised on execute(), not prepare().
        # We only allow read-only statements above to avoid side effects.
        ifx.execute(stmt)
        ifx.free_stmt(stmt)
        return {"valid": True}
    except Exception as exc:
        return {"valid": False, "error": str(exc)}
