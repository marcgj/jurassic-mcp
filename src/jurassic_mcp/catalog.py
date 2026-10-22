"""Informix system catalog queries, type decoding and SQL validation."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import IfxPy as ifx  # type: ignore[import-untyped]

log = logging.getLogger("jurassic_mcp.sql")

# ---------------------------------------------------------------------------
# Informix syscolumns.coltype decoding
#
# The low byte of ``coltype`` is the base type code. Flag bits on top:
#   0x100 (256)  -> NOT NULL
#   0x800 (2048) -> DISTINCT type (name in sysxtdtypes)
#   0x1000(4096) -> named ROW type (name in sysxtdtypes)
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
    40: "OPAQUE_VARIABLE",  # variable-length opaque/UDT, real name in sysxtdtypes
    41: "OPAQUE_FIXED",  # fixed-length opaque/UDT (e.g. BLOB, CLOB), see sysxtdtypes
    43: "LVARCHAR",
    45: "BOOLEAN",
    52: "BIGINT",
    53: "BIGSERIAL",
}

_NOT_NULL_FLAG = 0x100
_DISTINCT_FLAG = 0x800
_NAMED_ROW_FLAG = 0x1000

# DATETIME / INTERVAL qualifier codes (encoded in collength nibbles)
_DT_QUALIFIERS: dict[int, str] = {
    0: "YEAR",
    2: "MONTH",
    4: "DAY",
    6: "HOUR",
    8: "MINUTE",
    10: "SECOND",
    11: "FRACTION(1)",
    12: "FRACTION(2)",
    13: "FRACTION(3)",
    14: "FRACTION(4)",
    15: "FRACTION(5)",
}

# systables.tabtype values exposed by list_tables
TABTYPE_NAMES: dict[str, str] = {
    "T": "table",
    "V": "view",
    "P": "private_synonym",
    "S": "synonym",
    "E": "external_table",
}

USER_TABID_MIN = 100  # tabid < 100 are system catalog tables


def _safe_str(value: Any) -> str:
    """Return a stripped string representation for mixed driver values."""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _safe_int(value: Any, default: int = 0) -> int:
    """Safely coerce mixed driver values to int with a default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Type decoding (pure functions)
# ---------------------------------------------------------------------------


def decode_coltype(raw_coltype: int) -> tuple[str, bool]:
    """Decode an Informix ``coltype`` value into ``(base_type_name, nullable)``."""
    nullable = (raw_coltype & _NOT_NULL_FLAG) == 0
    base = raw_coltype & 0xFF
    return COLTYPE_MAP.get(base, f"UNKNOWN({base})"), nullable


def _qualifier(code: int) -> str:
    return _DT_QUALIFIERS.get(code, f"?{code}")


def _qualifier_unit(code: int) -> str:
    """Qualifier name without the FRACTION(n) precision (for a leading field)."""
    return "FRACTION" if code >= 11 else _qualifier(code)


def format_column_type(
    raw_coltype: int, collength: int, extended_name: str | None = None
) -> str:
    """Build a human-readable SQL type string from ``coltype``/``collength``.

    Examples: ``CHAR(10)``, ``VARCHAR(40)``, ``VARCHAR(40,10)``,
    ``DECIMAL(12,2)``, ``DECIMAL(16)``, ``DATETIME YEAR TO SECOND``,
    ``INTERVAL DAY(3) TO HOUR``, ``LVARCHAR(2048)``, ``BOOLEAN``, ``BLOB``.
    """
    base = raw_coltype & 0xFF
    # collength is a SMALLINT on older catalogs: undo sign extension.
    length = collength & 0xFFFF if collength < 0 else collength
    ext = (extended_name or "").strip()

    # DISTINCT / named ROW / opaque types: the catalog name is authoritative.
    if raw_coltype & (_DISTINCT_FLAG | _NAMED_ROW_FLAG) and ext:
        return ext.upper()
    if base in (40, 41):
        return ext.upper() if ext else COLTYPE_MAP[base]

    name = COLTYPE_MAP.get(base, f"UNKNOWN({base})")

    if base in (0, 15):  # CHAR, NCHAR
        return f"{name}({length})"
    if base in (13, 16):  # VARCHAR, NVARCHAR: max = low byte, min = high byte
        max_len = length & 0xFF
        min_len = (length >> 8) & 0xFF
        return f"{name}({max_len},{min_len})" if min_len else f"{name}({max_len})"
    if base == 43:  # LVARCHAR
        return f"{name}({collength})" if collength > 0 else name
    if base in (5, 8):  # DECIMAL, MONEY
        precision = (length >> 8) & 0xFF
        scale = length & 0xFF
        if scale == 0xFF:  # floating-point decimal
            return f"{name}({precision})"
        return f"{name}({precision},{scale})"
    if base == 10:  # DATETIME
        start = (length >> 4) & 0xF
        end = length & 0xF
        return f"DATETIME {_qualifier_unit(start)} TO {_qualifier(end)}"
    if base == 14:  # INTERVAL
        start = (length >> 4) & 0xF
        end = length & 0xF
        total_digits = (length >> 8) & 0xFF
        leading = total_digits - (end - start)
        lead = _qualifier_unit(start)
        if start >= 11:  # INTERVAL FRACTION TO FRACTION(n)
            return f"INTERVAL FRACTION TO {_qualifier(end)}"
        return f"INTERVAL {lead}({leading}) TO {_qualifier(end)}"
    return name


# ---------------------------------------------------------------------------
# Low-level statement helpers
# ---------------------------------------------------------------------------


def _stmt_error() -> str:
    try:
        return str(ifx.stmt_errormsg())
    except Exception:  # noqa: BLE001
        return ""


def _free(stmt: Any) -> None:
    if stmt and not isinstance(stmt, bool):
        try:
            ifx.free_stmt(stmt)
        except Exception:  # noqa: BLE001
            pass


def _fetch_all(stmt: Any) -> list[dict[str, Any]]:
    """Fetch all rows from an executed IfxPy statement as dicts."""
    rows: list[dict[str, Any]] = []
    row = ifx.fetch_assoc(stmt)
    while isinstance(row, dict):
        rows.append(dict(row))
        row = ifx.fetch_assoc(stmt)
    return rows


def _query(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Prepare, execute, fetch all rows and free the statement."""
    stmt = None
    start = time.perf_counter()
    try:
        stmt = ifx.prepare(conn, sql)
        if not stmt or isinstance(stmt, bool):
            raise RuntimeError(f"Informix prepare failed: {_stmt_error()}")
        ok = ifx.execute(stmt, params) if params else ifx.execute(stmt)
        if ok is False:
            raise RuntimeError(f"Informix execute failed: {_stmt_error()}")
        rows = _fetch_all(stmt)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("%s %s → %d rows, %.0f ms", _one_line(sql), list(params) if params else "",
                      len(rows), (time.perf_counter() - start) * 1000)
        return rows
    except Exception as exc:
        log.debug("%s %s → FAILED: %s", _one_line(sql), list(params) if params else "", exc)
        raise
    finally:
        _free(stmt)


def _one_line(sql: str, limit: int = 220) -> str:
    text = " ".join(sql.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


_PART_COLS = ", ".join(f"{{a}}.part{i} AS {{p}}{i}" for i in range(1, 17))


def _parts(alias: str, prefix: str) -> str:
    return _PART_COLS.format(a=alias, p=prefix)


def _part_numbers(row: dict[str, Any], prefix: str) -> list[int]:
    out: list[int] = []
    for i in range(1, 17):
        val = _safe_int(row.get(f"{prefix}{i}"), 0)
        if val == 0:
            break
        out.append(val)
    return out


def _column_maps(conn: Any, tabids: set[int]) -> dict[int, dict[int, str]]:
    """Return ``{tabid: {colno: colname}}`` for the given tabids (one query)."""
    ids = sorted({int(t) for t in tabids})
    if not ids:
        return {}
    in_list = ",".join(str(t) for t in ids)  # ints from the catalog, safe to inline
    rows = _query(
        conn,
        f"SELECT tabid, colno, colname FROM syscolumns WHERE tabid IN ({in_list})",
    )
    maps: dict[int, dict[int, str]] = {t: {} for t in ids}
    for r in rows:
        maps.setdefault(_safe_int(r.get("tabid")), {})[_safe_int(r.get("colno"))] = (
            _safe_str(r.get("colname"))
        )
    return maps


# ---------------------------------------------------------------------------
# Table resolution
# ---------------------------------------------------------------------------


class CatalogLookupError(LookupError):
    """Raised when a table cannot be resolved unambiguously."""


def split_owner(table: str, owner: str | None) -> tuple[str, str | None]:
    """Accept ``owner.table`` / ``"owner".table`` notation when owner is not given."""
    table = table.strip()
    if owner is None and "." in table and ":" not in table:
        own, _, tab = table.partition(".")
        owner = own
        table = tab
    if owner is not None:
        owner = owner.strip().strip('"').strip() or None
    return table.strip().strip('"').strip(), owner


def resolve_table(
    conn: Any, table: str, owner: str | None = None, follow_synonyms: bool = True
) -> dict[str, Any]:
    """Resolve *table* (case-insensitive, optional owner) to a systables row.

    User objects (tabid >= 100) win over system catalog tables with the same
    name. Local synonyms are followed to their base table when requested.
    Raises :class:`CatalogLookupError` when not found or ambiguous.
    """
    table, owner = split_owner(table, owner)
    sql = (
        "SELECT tabid, tabname, owner, tabtype FROM systables "
        "WHERE LOWER(tabname) = LOWER(?)"
    )
    params: tuple[Any, ...] = (table,)
    if owner:
        sql += " AND LOWER(TRIM(owner)) = LOWER(?)"
        params += (owner,)
    rows = _query(conn, sql, params)
    if not rows:
        who = f" for owner '{owner}'" if owner else ""
        raise CatalogLookupError(
            f"Table '{table}' not found{who}. Use list_tables or search_schema "
            "to find the right name."
        )
    user_rows = [r for r in rows if _safe_int(r.get("tabid")) >= USER_TABID_MIN]
    candidates = user_rows or rows
    if len(candidates) > 1:
        owners = ", ".join(sorted(_safe_str(r.get("owner")) for r in candidates))
        raise CatalogLookupError(
            f"Table name '{table}' is ambiguous (owners: {owners}). "
            "Pass the 'owner' parameter."
        )
    row = candidates[0]
    info = {
        "tabid": _safe_int(row.get("tabid")),
        "name": _safe_str(row.get("tabname")),
        "owner": _safe_str(row.get("owner")),
        "tabtype": _safe_str(row.get("tabtype")),
    }
    if follow_synonyms and info["tabtype"] in ("P", "S"):
        syn = _query(
            conn,
            "SELECT btabid, servername, dbname, owner, tabname "
            "FROM syssyntable WHERE tabid = ?",
            (info["tabid"],),
        )
        target = syn[0] if syn else {}
        btabid = _opt_int(target.get("btabid"))
        if btabid is None or _safe_str(target.get("dbname")):
            remote = ":".join(
                p
                for p in (
                    _safe_str(target.get("dbname")),
                    _safe_str(target.get("owner")),
                    _safe_str(target.get("tabname")),
                )
                if p
            )
            raise CatalogLookupError(
                f"'{info['name']}' is a synonym for a remote object ({remote or '?'}); "
                "query that database directly."
            )
        base = _query(
            conn,
            "SELECT tabid, tabname, owner, tabtype FROM systables WHERE tabid = ?",
            (btabid,),
        )
        if not base:
            raise CatalogLookupError(f"Synonym '{info['name']}' target not found.")
        b = base[0]
        info = {
            "tabid": _safe_int(b.get("tabid")),
            "name": _safe_str(b.get("tabname")),
            "owner": _safe_str(b.get("owner")),
            "tabtype": _safe_str(b.get("tabtype")),
            "via_synonym": info["name"],
        }
    return info


# ---------------------------------------------------------------------------
# Catalog queries
# ---------------------------------------------------------------------------


def list_databases(conn: Any) -> list[dict[str, Any]]:
    """List databases in the instance (connection must be to ``sysmaster``)."""
    rows = _query(conn, "SELECT name, owner FROM sysdatabases ORDER BY name")
    return [
        {"name": _safe_str(r.get("name")), "owner": _safe_str(r.get("owner"))}
        for r in rows
    ]


def like_pattern(pattern: str) -> str:
    """Normalise a user pattern into a lower-case LIKE pattern.

    ``*`` and ``?`` are accepted as wildcards; a pattern without wildcards is
    treated as a substring search.
    """
    p = pattern.strip().lower().replace("*", "%").replace("?", "_")
    if "%" not in p:
        p = f"%{p}%"
    return p


def list_tables(
    conn: Any, name_filter: str | None = None, owner: str | None = None
) -> list[dict[str, Any]]:
    """List user tables, views and synonyms (tabid >= 100)."""
    sql = (
        "SELECT t.tabid, t.tabname, t.owner, t.tabtype, t.ncols, t.nrows, "
        "s.servername AS syn_server, s.dbname AS syn_db, s.owner AS syn_owner, "
        "s.tabname AS syn_tabname, bt.tabname AS syn_btabname, "
        "bt.owner AS syn_bowner "
        "FROM systables t "
        "LEFT OUTER JOIN syssyntable s ON s.tabid = t.tabid "
        "LEFT OUTER JOIN systables bt ON bt.tabid = s.btabid "
        "WHERE t.tabid >= 100 AND t.tabtype IN ('T', 'V', 'P', 'S', 'E')"
    )
    params: tuple[Any, ...] = ()
    if name_filter and name_filter.strip():
        sql += " AND LOWER(t.tabname) LIKE ?"
        params += (like_pattern(name_filter),)
    if owner and owner.strip():
        sql += " AND LOWER(TRIM(t.owner)) = LOWER(?)"
        params += (owner.strip().strip('"'),)
    sql += " ORDER BY t.tabname, t.owner"

    result: list[dict[str, Any]] = []
    for r in _query(conn, sql, params):
        tabtype = _safe_str(r.get("tabtype"))
        entry: dict[str, Any] = {
            "name": _safe_str(r.get("tabname")),
            "owner": _safe_str(r.get("owner")),
            "type": TABTYPE_NAMES.get(tabtype, tabtype),
        }
        if tabtype in ("T", "V", "E"):
            entry["num_columns"] = _opt_int(r.get("ncols"))
        if tabtype == "T":
            nrows = r.get("nrows")
            # nrows is a FLOAT statistic maintained by UPDATE STATISTICS
            entry["num_rows_estimate"] = int(float(nrows)) if nrows is not None else None
        if tabtype in ("P", "S"):
            target_tab = _safe_str(r.get("syn_btabname")) or _safe_str(
                r.get("syn_tabname")
            )
            target_owner = _safe_str(r.get("syn_bowner")) or _safe_str(
                r.get("syn_owner")
            )
            target = f"{target_owner}.{target_tab}" if target_owner else target_tab
            db = _safe_str(r.get("syn_db"))
            server = _safe_str(r.get("syn_server"))
            if db:
                target = f"{db}{'@' + server if server else ''}:{target}"
            entry["synonym_for"] = target
        result.append(entry)
    return result


# Live row counts from the partition headers: exact and cheap, independent of
# UPDATE STATISTICS. Fragmented tables (partnum = 0) sum their fragments.
_LIVE_ROWS_SQL = (
    "SELECT t.tabid, t.tabname, t.owner, t.ncols, SUM(p.nrows) AS nrows "
    "FROM systables t, sysmaster:sysptnhdr p "
    "WHERE t.tabid >= 100 AND t.tabtype = 'T' AND t.partnum <> 0 "
    "AND p.partnum = t.partnum "
    "GROUP BY 1, 2, 3, 4 "
    "UNION ALL "
    "SELECT t.tabid, t.tabname, t.owner, t.ncols, SUM(p.nrows) "
    "FROM systables t, sysfragments f, sysmaster:sysptnhdr p "
    "WHERE t.tabid >= 100 AND t.tabtype = 'T' AND t.partnum = 0 "
    "AND f.tabid = t.tabid AND f.fragtype = 'T' AND p.partnum = f.partn "
    "GROUP BY 1, 2, 3, 4"
)

_STAT_ROWS_SQL = (
    "SELECT tabid, tabname, owner, ncols, nrows FROM systables "
    "WHERE tabid >= 100 AND tabtype = 'T'"
)


def table_row_counts(conn: Any) -> tuple[list[dict[str, Any]], str]:
    """Return ``(tables, source)`` for every user table with its row count.

    ``source`` is ``"live"`` (sysmaster partition headers, exact) or
    ``"statistics"`` (``systables.nrows``, as of the last UPDATE STATISTICS)
    when sysmaster cannot be joined (e.g. permissions or logging mode).
    """
    try:
        rows, source = _query(conn, _LIVE_ROWS_SQL), "live"
    except RuntimeError:
        rows, source = _query(conn, _STAT_ROWS_SQL), "statistics"
    result = [
        {
            "tabid": _safe_int(r.get("tabid")),
            "name": _safe_str(r.get("tabname")),
            "owner": _safe_str(r.get("owner")),
            "num_columns": _opt_int(r.get("ncols")),
            "rows": int(float(r["nrows"])) if r.get("nrows") is not None else 0,
        }
        for r in rows
    ]
    return result, source


def table_relationship_counts(conn: Any) -> dict[int, int]:
    """Return ``{tabid: number of declared FKs in or out}`` for user tables."""
    sql = (
        "SELECT c.tabid AS tabid, COUNT(*) AS n FROM sysconstraints c "
        "WHERE c.constrtype = 'R' AND c.tabid >= 100 GROUP BY 1 "
        "UNION ALL "
        "SELECT pc.tabid, COUNT(*) FROM sysreferences r, sysconstraints pc "
        "WHERE pc.constrid = r.primary AND pc.tabid >= 100 GROUP BY 1"
    )
    counts: dict[int, int] = {}
    for r in _query(conn, sql):
        tabid = _safe_int(r.get("tabid"))
        counts[tabid] = counts.get(tabid, 0) + _safe_int(r.get("n"))
    return counts


def describe_table(conn: Any, table: str, owner: str | None = None) -> dict[str, Any]:
    """Return ``{"table": <resolved info>, "columns": [...]}`` for *table*."""
    info = resolve_table(conn, table, owner)
    rows = _query(
        conn,
        "SELECT c.colname, c.colno, c.coltype, c.collength, c.extended_id, "
        "x.name AS xtd_name "
        "FROM syscolumns c "
        "LEFT OUTER JOIN sysxtdtypes x "
        "ON x.extended_id = c.extended_id AND c.extended_id > 0 "
        "WHERE c.tabid = ? ORDER BY c.colno",
        (info["tabid"],),
    )
    columns: list[dict[str, Any]] = []
    for r in rows:
        coltype = _safe_int(r.get("coltype"))
        collength = _safe_int(r.get("collength"))
        base_name, nullable = decode_coltype(coltype)
        xtd = _safe_str(r.get("xtd_name")) or None
        columns.append(
            {
                "name": _safe_str(r.get("colname")),
                "position": _safe_int(r.get("colno")),
                "type": format_column_type(coltype, collength, xtd),
                "base_type": base_name,
                "raw_coltype": coltype,
                "raw_length": collength,
                "nullable": nullable,
            }
        )
    return {"table": info, "columns": columns}


def list_indexes(conn: Any, table: str, owner: str | None = None) -> dict[str, Any]:
    """Return ``{"table": info, "indexes": [...]}`` with resolved column names."""
    info = resolve_table(conn, table, owner)
    tabid = info["tabid"]
    rows = _query(
        conn,
        "SELECT i.idxname, i.idxtype, i.clustered, "
        f"{_parts('i', 'p')}, "
        "c.constrname, c.constrtype "
        "FROM sysindexes i "
        "LEFT OUTER JOIN sysconstraints c "
        "ON c.idxname = i.idxname AND c.tabid = i.tabid "
        "AND c.constrtype IN ('P', 'U', 'R') "
        "WHERE i.tabid = ? ORDER BY i.idxname",
        (tabid,),
    )
    col_map = _column_maps(conn, {tabid}).get(tabid, {})
    constr_types = {"P": "primary_key", "U": "unique", "R": "foreign_key"}

    by_name: dict[str, dict[str, Any]] = {}
    for r in rows:
        raw_name = r.get("idxname") or ""
        name = _safe_str(raw_name)
        idx = by_name.get(name)
        if idx is None:
            idx_columns = [
                {
                    "column": col_map.get(abs(p), f"col#{abs(p)}"),
                    "direction": "DESC" if p < 0 else "ASC",
                }
                for p in _part_numbers(r, "p")
            ]
            idx = {
                "name": name,
                "unique": _safe_str(r.get("idxtype")).upper() == "U",
                "clustered": _safe_str(r.get("clustered")).upper() == "C",
                # Names starting with a space are generated by the engine
                "system_generated": isinstance(raw_name, str)
                and raw_name.startswith(" "),
                "columns": idx_columns,
                "constraints": [],
            }
            by_name[name] = idx
        ctype = _safe_str(r.get("constrtype"))
        if ctype:
            idx["constraints"].append(
                {
                    "name": _safe_str(r.get("constrname")),
                    "type": constr_types.get(ctype, ctype),
                }
            )
    return {"table": info, "indexes": list(by_name.values())}


def list_foreign_keys(
    conn: Any, table: str, owner: str | None = None
) -> dict[str, Any]:
    """Return outgoing FKs of *table* and incoming FKs that reference it.

    Constraint index names are matched with their raw value (system-generated
    names start with a space) and the owning tabid.
    """
    info = resolve_table(conn, table, owner)
    tabid = info["tabid"]
    base_sql = (
        "SELECT c.constrname, c.tabid AS fk_tabid, ft.tabname AS fk_table, "
        "ft.owner AS fk_owner, pc.tabid AS pk_tabid, pt.tabname AS pk_table, "
        "pt.owner AS pk_owner, pc.constrname AS pk_constrname, r.delrule, "
        f"{_parts('fidx', 'fp')}, {_parts('pidx', 'rp')} "
        "FROM sysconstraints c "
        "INNER JOIN sysreferences r ON r.constrid = c.constrid "
        "INNER JOIN sysconstraints pc ON pc.constrid = r.primary "
        "INNER JOIN systables ft ON ft.tabid = c.tabid "
        "INNER JOIN systables pt ON pt.tabid = pc.tabid "
        "LEFT OUTER JOIN sysindexes fidx "
        "ON fidx.idxname = c.idxname AND fidx.tabid = c.tabid "
        "LEFT OUTER JOIN sysindexes pidx "
        "ON pidx.idxname = pc.idxname AND pidx.tabid = pc.tabid "
        "WHERE c.constrtype = 'R' AND "
    )
    outgoing = _query(conn, base_sql + "c.tabid = ? ORDER BY c.constrname", (tabid,))
    incoming = _query(conn, base_sql + "pc.tabid = ? ORDER BY ft.tabname", (tabid,))

    tabids = {tabid}
    for r in outgoing + incoming:
        tabids.add(_safe_int(r.get("fk_tabid")))
        tabids.add(_safe_int(r.get("pk_tabid")))
    maps = _column_maps(conn, tabids)

    def cols(r: dict[str, Any], tab_key: str, prefix: str) -> list[str]:
        cmap = maps.get(_safe_int(r.get(tab_key)), {})
        return [cmap.get(abs(p), f"col#{abs(p)}") for p in _part_numbers(r, prefix)]

    def build(r: dict[str, Any]) -> dict[str, Any]:
        delrule = _safe_str(r.get("delrule")).upper()
        return {
            "name": _safe_str(r.get("constrname")),
            "table": _safe_str(r.get("fk_table")),
            "table_owner": _safe_str(r.get("fk_owner")),
            "columns": cols(r, "fk_tabid", "fp"),
            "referenced_table": _safe_str(r.get("pk_table")),
            "referenced_owner": _safe_str(r.get("pk_owner")),
            "referenced_columns": cols(r, "pk_tabid", "rp"),
            "referenced_constraint": _safe_str(r.get("pk_constrname")),
            "delete_rule": "CASCADE" if delrule == "C" else "RESTRICT",
        }

    return {
        "table": info,
        "foreign_keys": [build(r) for r in outgoing],
        "referenced_by": [build(r) for r in incoming],
    }


def search_schema(
    conn: Any, pattern: str, limit: int = 200
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Search user table/view/synonym names and column names (case-insensitive).

    Returns ``(tables, columns, truncated)``.
    """
    like = like_pattern(pattern)
    fetch = limit + 1
    tables = _query(
        conn,
        f"SELECT FIRST {fetch} tabname, owner, tabtype FROM systables "
        "WHERE tabid >= 100 AND tabtype IN ('T', 'V', 'P', 'S', 'E') "
        "AND LOWER(tabname) LIKE ? ORDER BY tabname",
        (like,),
    )
    columns = _query(
        conn,
        f"SELECT FIRST {fetch} t.tabname, t.owner, t.tabtype, c.colname, "
        "c.coltype, c.collength, x.name AS xtd_name "
        "FROM syscolumns c INNER JOIN systables t ON t.tabid = c.tabid "
        "LEFT OUTER JOIN sysxtdtypes x "
        "ON x.extended_id = c.extended_id AND c.extended_id > 0 "
        "WHERE t.tabid >= 100 AND t.tabtype IN ('T', 'V', 'E') "
        "AND LOWER(c.colname) LIKE ? ORDER BY t.tabname, c.colno",
        (like,),
    )
    truncated = len(tables) > limit or len(columns) > limit
    table_hits = [
        {
            "table": _safe_str(r.get("tabname")),
            "owner": _safe_str(r.get("owner")),
            "type": TABTYPE_NAMES.get(
                _safe_str(r.get("tabtype")), _safe_str(r.get("tabtype"))
            ),
        }
        for r in tables[:limit]
    ]
    column_hits = [
        {
            "table": _safe_str(r.get("tabname")),
            "owner": _safe_str(r.get("owner")),
            "column": _safe_str(r.get("colname")),
            "type": format_column_type(
                _safe_int(r.get("coltype")),
                _safe_int(r.get("collength")),
                _safe_str(r.get("xtd_name")) or None,
            ),
        }
        for r in columns[:limit]
    ]
    return table_hits, column_hits, truncated


# ---------------------------------------------------------------------------
# SQL validation
# ---------------------------------------------------------------------------


class SqlCheckError(ValueError):
    """Raised by :func:`check_read_only_sql` when a statement is rejected."""


def _mask_sql(sql: str) -> str:
    """Return *sql* with comments blanked and string/quoted contents masked.

    The result has the same length as *sql* so positions map 1:1. Handles
    ``'...'`` and ``"..."`` (doubled quote escapes), ``-- ...``, ``/* ... */``
    and Informix ``{ ... }`` comments.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            out.append(ch)
            i += 1
            while True:
                if i >= n:
                    raise SqlCheckError("Unterminated quoted string.")
                if sql[i] == ch:
                    if i + 1 < n and sql[i + 1] == ch:  # escaped quote
                        out.append("xx")
                        i += 2
                        continue
                    out.append(ch)
                    i += 1
                    break
                out.append("x")
                i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise SqlCheckError("Unterminated /* */ comment.")
            out.append(" " * (end + 2 - i))
            i = end + 2
        elif ch == "{":
            end = sql.find("}", i + 1)
            if end == -1:
                raise SqlCheckError("Unterminated { } comment.")
            out.append(" " * (end + 1 - i))
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


_INTO_RE = re.compile(r"\bINTO\b", re.IGNORECASE)
_FIRST_WORD_RE = re.compile(r"[A-Za-z_]+")


def check_read_only_sql(sql: str) -> str:
    """Validate that *sql* is a single read-only SELECT (or WITH) statement.

    Returns the statement text to prepare (trailing ``;`` removed).
    Raises :class:`SqlCheckError` with a user-facing message otherwise.
    """
    if not sql or not sql.strip():
        raise SqlCheckError("SQL cannot be empty.")
    masked = _mask_sql(sql)

    # Strip trailing semicolons/whitespace (positions are aligned with sql).
    end = len(masked)
    while end > 0 and (masked[end - 1].isspace() or masked[end - 1] == ";"):
        end -= 1
    body_masked = masked[:end]
    if ";" in body_masked:
        raise SqlCheckError(
            "Multiple SQL statements are not allowed. Submit one statement at a time."
        )

    lead = body_masked.lstrip().lstrip("(").lstrip()
    while lead.startswith("("):
        lead = lead.lstrip("(").lstrip()
    m = _FIRST_WORD_RE.match(lead)
    first = m.group(0).upper() if m else ""
    if first not in ("SELECT", "WITH"):
        raise SqlCheckError(
            "Only read-only SELECT statements (or WITH ... SELECT on Informix "
            f"14.10+) can be validated; got '{first or lead[:20]}'."
        )
    into = _INTO_RE.search(body_masked)
    if into:
        raise SqlCheckError(
            "SELECT ... INTO (TEMP/STANDARD/RAW/EXTERNAL tables or host "
            "variables) is not allowed; remove the INTO clause."
        )
    return sql[:end].strip()


def validate_sql(conn: Any, sql: str) -> dict[str, Any]:
    """Compile (PREPARE) a read-only statement without executing it.

    Returns ``{"valid": True, "result_columns": [...]}`` or
    ``{"valid": False, "error": "..."}``.
    """
    try:
        statement = check_read_only_sql(sql)
    except SqlCheckError as exc:
        return {"valid": False, "error": str(exc), "stage": "read_only_check"}

    stmt = None
    try:
        try:
            log.debug("PREPARE only (validate_sql): %s", _one_line(statement))
            stmt = ifx.prepare(conn, statement)
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "error": str(exc), "stage": "prepare"}
        if not stmt or isinstance(stmt, bool):
            return {"valid": False, "error": _stmt_error(), "stage": "prepare"}

        result: dict[str, Any] = {"valid": True}
        # Describing the result set forces the server to resolve it fully.
        try:
            ncols = ifx.num_fields(stmt)
            cols: list[dict[str, Any]] = []
            for i in range(int(ncols or 0)):
                col: dict[str, Any] = {"name": _safe_str(ifx.field_name(stmt, i))}
                try:
                    col["type"] = _safe_str(ifx.field_type(stmt, i))
                except Exception:  # noqa: BLE001
                    pass
                cols.append(col)
            result["result_columns"] = cols
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "error": str(exc), "stage": "describe"}
        return result
    finally:
        _free(stmt)
