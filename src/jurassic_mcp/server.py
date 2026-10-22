"""FastMCP server definition with all Informix tools.

Every tool is read-only, opens a fresh connection per call and returns a
JSON-serialisable ``dict`` so FastMCP emits structured output. Failures are
reported as :class:`fastmcp.exceptions.ToolError`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import catalog
from .config import AppConfig
from .config import save_descriptions as _save_descriptions
from .db import connect

mcp = FastMCP(
    "jurassic-mcp",
    instructions=(
        "Read-only exploration of a legacy IBM Informix instance. Typical flow: "
        "list_databases -> search_schema (find tables/columns by keyword, "
        "including user-written descriptions) or list_tables -> describe_table "
        "-> list_foreign_keys / list_indexes to understand joins -> "
        "validate_sql to check a SELECT before handing it to the user. "
        "Nothing is ever executed against the data. To document an "
        "undocumented database with the user, use the `autodiscover` prompt "
        "(discovery_overview -> describe tables -> save_descriptions)."
    ),
)

# The config is injected at startup via ``init_server``.
_config: AppConfig | None = None

SEARCH_LIMIT = 200

T = TypeVar("T")


def init_server(config: AppConfig) -> None:
    """Store the loaded configuration so that tools can access it."""
    global _config  # noqa: PLW0603
    _config = config


def _cfg() -> AppConfig:
    if _config is None:
        raise ToolError("Server not initialized — call init_server(config) first")
    return _config


def _require(value: str | None, field: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ToolError(f"'{field}' cannot be empty")
    return normalized


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _run(database: str, fn: Callable[[Any], T]) -> T:
    """Open a connection to *database*, run ``fn(conn)``, map errors to ToolError."""
    cfg = _cfg()
    try:
        with connect(cfg.informix, database) as conn:
            return fn(conn)
    except ToolError:
        raise
    except catalog.CatalogLookupError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Informix error on database '{database}': {exc}") from exc


def _table_desc(database: str, info: dict[str, Any]) -> str:
    cfg = _cfg()
    desc = cfg.get_table_description(database.lower(), info["name"].lower())
    if not desc and info.get("via_synonym"):
        desc = cfg.get_table_description(database.lower(), info["via_synonym"].lower())
    return desc


def _table_header(database: str, info: dict[str, Any]) -> dict[str, Any]:
    header: dict[str, Any] = {
        "database": database,
        "table": info["name"],
        "owner": info["owner"],
        "type": catalog.TABTYPE_NAMES.get(info["tabtype"], info["tabtype"]),
    }
    if info.get("via_synonym"):
        header["resolved_from_synonym"] = info["via_synonym"]
    desc = _table_desc(database, info)
    if desc:
        header["description"] = desc
    return header


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_RO = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}


@mcp.tool(annotations=_RO, tags={"informix", "schema"})
def list_databases() -> dict[str, Any]:
    """List all databases in the Informix instance.

    Start here when you do not know which database holds the data.

    Returns ``{"databases": [{"name", "owner", "description"?}]}``; the
    optional ``description`` comes from the server's configuration file and
    explains what the (often cryptically named) database contains.
    """
    cfg = _cfg()
    rows = _run("sysmaster", catalog.list_databases)
    for row in rows:
        desc = cfg.get_database_description(row["name"].lower())
        if desc:
            row["description"] = desc
    return {"databases": rows}


@mcp.tool(annotations=_RO, tags={"informix", "schema"})
def list_tables(
    database: str,
    name_filter: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """List user tables, views and synonyms in a database (system catalog excluded).

    Use ``search_schema`` instead when looking for a concept (e.g. "customer")
    across table *and* column names and descriptions.

    Args:
        database: Informix database name.
        name_filter: Optional case-insensitive name filter. Plain text matches
            as a substring; ``%``/``_`` (or ``*``/``?``) act as wildcards,
            e.g. ``cli%`` for names starting with "cli".
        owner: Optional owner (schema) to restrict to.

    Returns ``{"database", "count", "tables": [...]}`` where each entry has
    ``name``, ``owner``, ``type`` (table | view | synonym | private_synonym |
    external_table), ``num_columns`` (tables/views), ``num_rows_estimate``
    (tables, from UPDATE STATISTICS — may be stale), ``synonym_for``
    (synonyms) and an optional configured ``description``.
    """
    cfg = _cfg()
    db = _require(database, "database")
    flt, own = _optional(name_filter), _optional(owner)
    rows = _run(db, lambda conn: catalog.list_tables(conn, flt, own))
    for row in rows:
        desc = cfg.get_table_description(db.lower(), row["name"].lower())
        if desc:
            row["description"] = desc
    return {"database": db, "count": len(rows), "tables": rows}


@mcp.tool(annotations=_RO, tags={"informix", "schema"})
def describe_table(
    database: str, table: str, owner: str | None = None
) -> dict[str, Any]:
    """Describe the columns of a table or view (synonyms are followed).

    Args:
        database: Informix database name.
        table: Table/view/synonym name (case-insensitive). ``owner.table``
            notation is accepted.
        owner: Optional owner; required only when several owners have a
            table with the same name (the error lists them).

    Returns ``{"database", "table", "owner", "type", "description"?,
    "columns": [...]}``. Each column has ``name``, ``position``, ``type``
    (full SQL type such as ``DECIMAL(12,2)``, ``VARCHAR(40)``,
    ``DATETIME YEAR TO SECOND``), ``base_type``, ``nullable``, the raw
    catalog values ``raw_coltype``/``raw_length``, and an optional configured
    ``description`` explaining the column's meaning.
    """
    cfg = _cfg()
    db = _require(database, "database")
    tbl = _require(table, "table")
    own = _optional(owner)
    data = _run(db, lambda conn: catalog.describe_table(conn, tbl, own))
    info = data["table"]

    col_descs = cfg.get_column_descriptions(db.lower(), info["name"].lower())
    if not col_descs and info.get("via_synonym"):
        col_descs = cfg.get_column_descriptions(
            db.lower(), info["via_synonym"].lower()
        )
    for col in data["columns"]:
        desc = col_descs.get(col["name"].lower(), "")
        if desc:
            col["description"] = desc

    result = _table_header(db, info)
    result["columns"] = data["columns"]
    return result


@mcp.tool(annotations=_RO, tags={"informix", "schema"})
def list_indexes(database: str, table: str, owner: str | None = None) -> dict[str, Any]:
    """List the indexes of a table, with their columns and backing constraints.

    Useful to find the primary key / unique keys (good join and lookup
    columns) of legacy tables.

    Args:
        database: Informix database name.
        table: Table name (case-insensitive, ``owner.table`` accepted).
        owner: Optional owner to disambiguate.

    Returns ``{"database", "table", "owner", "type", "indexes": [...]}``;
    each index has ``name``, ``unique``, ``clustered``, ``system_generated``
    (engine-created for a constraint), ``columns`` (``column`` + ``direction``)
    and ``constraints`` (``name`` + ``type``: primary_key | unique |
    foreign_key) that use the index.
    """
    db = _require(database, "database")
    tbl = _require(table, "table")
    own = _optional(owner)
    data = _run(db, lambda conn: catalog.list_indexes(conn, tbl, own))
    result = _table_header(db, data["table"])
    result["indexes"] = data["indexes"]
    return result


@mcp.tool(annotations=_RO, tags={"informix", "schema"})
def list_foreign_keys(
    database: str, table: str, owner: str | None = None
) -> dict[str, Any]:
    """List declared foreign keys of a table, in both directions.

    Use this to learn how to JOIN tables. Note that many legacy schemas have
    few or no declared foreign keys; if empty, look for matching column names
    with ``search_schema``.

    Args:
        database: Informix database name.
        table: Table name (case-insensitive, ``owner.table`` accepted).
        owner: Optional owner to disambiguate.

    Returns ``{"database", "table", "owner", "type", "foreign_keys": [...],
    "referenced_by": [...]}``. ``foreign_keys`` are this table's outgoing
    references; ``referenced_by`` are other tables pointing at it. Each entry
    has ``name``, ``table``, ``table_owner``, ``columns``,
    ``referenced_table``, ``referenced_owner``, ``referenced_columns``
    (positionally paired with ``columns``), ``referenced_constraint`` and
    ``delete_rule`` (CASCADE | RESTRICT).
    """
    db = _require(database, "database")
    tbl = _require(table, "table")
    own = _optional(owner)
    data = _run(db, lambda conn: catalog.list_foreign_keys(conn, tbl, own))
    result = _table_header(db, data["table"])
    result["foreign_keys"] = data["foreign_keys"]
    result["referenced_by"] = data["referenced_by"]
    return result


def _like_regex(pattern: str) -> re.Pattern[str]:
    """Compile a LIKE pattern (as produced by catalog.like_pattern) to a regex."""
    parts = []
    for ch in catalog.like_pattern(pattern):
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts), re.IGNORECASE | re.DOTALL)


@mcp.tool(annotations=_RO, tags={"informix", "schema"})
def search_schema(database: str, pattern: str) -> dict[str, Any]:
    """Find tables and columns by keyword — the best first step in legacy schemas.

    Searches, case-insensitively, (1) user table/view/synonym names and column
    names in the database catalog and (2) the human-written table and column
    descriptions in the server configuration (so searching "invoice" can find
    a table named ``fctcab`` if it was documented).

    Args:
        database: Informix database name.
        pattern: Text to look for. Plain text is a substring match; ``%``/``_``
            (or ``*``/``?``) act as wildcards.

    Returns ``{"database", "pattern", "tables": [...], "columns": [...],
    "truncated"}``. Table hits have ``table``, ``owner``, ``type``,
    ``matched_in`` (name | description) and optional ``description``; column
    hits have ``table``, ``owner``, ``column``, ``type`` (when found in the
    catalog), ``matched_in`` and optional ``description``. At most 200 hits
    per list are returned; ``truncated`` is true when more exist — refine the
    pattern.
    """
    cfg = _cfg()
    db = _require(database, "database")
    pat = _require(pattern, "pattern")
    tables, columns, truncated = _run(
        db, lambda conn: catalog.search_schema(conn, pat, SEARCH_LIMIT)
    )
    dbl = db.lower()
    rx = _like_regex(pat)

    for t in tables:
        t["matched_in"] = "name"
        desc = cfg.get_table_description(dbl, t["table"].lower())
        if desc:
            t["description"] = desc
    for c in columns:
        c["matched_in"] = "name"
        desc = cfg.get_column_descriptions(dbl, c["table"].lower()).get(
            c["column"].lower(), ""
        )
        if desc:
            c["description"] = desc

    # Description matches from configuration (not already found by name).
    db_desc = cfg.descriptions.get(dbl)
    if db_desc is not None:
        seen_tables = {t["table"].lower() for t in tables}
        seen_cols = {(c["table"].lower(), c["column"].lower()) for c in columns}
        for tname, tdesc in db_desc.tables.items():
            tkey = str(tname).lower()
            if (
                tkey not in seen_tables
                and tdesc.description
                and rx.search(tdesc.description)
            ):
                if len(tables) >= SEARCH_LIMIT:
                    truncated = True
                else:
                    tables.append(
                        {
                            "table": tkey,
                            "matched_in": "description",
                            "description": tdesc.description,
                        }
                    )
                    seen_tables.add(tkey)
            for cname, cdesc in tdesc.columns.items():
                ckey = str(cname).lower()
                if (tkey, ckey) in seen_cols or not (
                    rx.search(cdesc) or rx.fullmatch(ckey)
                ):
                    continue
                if len(columns) >= SEARCH_LIMIT:
                    truncated = True
                    continue
                columns.append(
                    {
                        "table": tkey,
                        "column": ckey,
                        "matched_in": "description",
                        "description": cdesc,
                    }
                )
                seen_cols.add((tkey, ckey))

    return {
        "database": db,
        "pattern": pat,
        "tables": tables,
        "columns": columns,
        "truncated": truncated,
    }


@mcp.tool(annotations=_RO, tags={"informix", "sql"})
def validate_sql(database: str, sql: str) -> dict[str, Any]:
    """Check that a single read-only SELECT compiles on Informix, without running it.

    The statement is only PREPAREd and its result set described on the
    server — it is never executed, so no rows are read, locked or changed.
    PREPARE checks syntax, that tables/columns/functions exist, types and the
    connecting user's privileges. Runtime errors (e.g. conversion errors on
    specific data values) cannot be detected this way.

    Restrictions (checked locally first): one statement only (a trailing
    ``;`` is fine); it must start with SELECT (or WITH, which requires
    Informix 14.10+), optionally after comments or ``(``; ``SELECT ... INTO``
    (TEMP/STANDARD/RAW/EXTERNAL tables or host variables) is rejected.

    Args:
        database: Informix database name to compile against.
        sql: The SELECT statement.

    Returns ``{"valid": true, "result_columns": [{"name", "type"}]}`` or
    ``{"valid": false, "error": "...", "stage": "read_only_check" | "prepare"
    | "describe"}`` where ``error`` is the Informix message to fix.
    """
    db = _require(database, "database")
    text = _require(sql, "sql")
    try:  # reject locally before opening a connection
        catalog.check_read_only_sql(text)
    except catalog.SqlCheckError as exc:
        return {"valid": False, "error": str(exc), "stage": "read_only_check"}
    return _run(db, lambda conn: catalog.validate_sql(conn, text))


# ---------------------------------------------------------------------------
# Discovery: document a legacy database interactively (user <-> LLM <-> MCP)
# ---------------------------------------------------------------------------


def _split_patterns(text: str | None) -> list[re.Pattern[str]]:
    return [_like_regex(p.strip()) for p in (text or "").split(",") if p.strip()]


@mcp.tool(annotations=_RO, tags={"informix", "discovery"})
def discovery_overview(
    database: str,
    limit: int = 25,
    name_filter: str | None = None,
    exclude: str | None = None,
    only_undocumented: bool = False,
) -> dict[str, Any]:
    """Rank a database's tables by row count and show how well each is documented.

    Use it to decide which tables to document first (the biggest tables are
    usually the core business entities) and to track documentation progress.
    Reads only catalog metadata, never table data.

    Args:
        database: Informix database name.
        limit: Maximum number of tables to return (largest first).
        name_filter: Optional comma-separated name patterns to include
            (substring, or ``%``/``*`` wildcards), e.g. ``"fac%,alb%"``.
        exclude: Optional comma-separated name patterns to skip, e.g.
            ``"s_%,tmp%"`` for framework/temporary tables.
        only_undocumented: Return only tables that still lack a table
            description or some column descriptions.

    Returns ``{"database", "database_description"?, "row_count_source"
    (live | statistics), "summary": {...}, "tables": [...],
    "descriptions_file"}``. Each table has ``name``, ``owner``, ``rows``,
    ``num_columns``, ``relationships`` (declared FKs in/out),
    ``has_description``, ``documented_columns`` and ``status``
    (undocumented | partial | documented).
    """
    cfg = _cfg()
    db = _require(database, "database")
    limit = max(1, min(int(limit), 500))
    include_rx, exclude_rx = _split_patterns(name_filter), _split_patterns(exclude)

    def work(conn: Any) -> tuple[list[dict[str, Any]], str, dict[int, int]]:
        tables, source = catalog.table_row_counts(conn)
        try:
            rels = catalog.table_relationship_counts(conn)
        except RuntimeError:
            rels = {}
        return tables, source, rels

    tables, source, rels = _run(db, work)
    dbl = db.lower()
    entries: list[dict[str, Any]] = []
    counts = {"undocumented": 0, "partial": 0, "documented": 0}
    empty = 0
    for t in tables:
        name = t["name"]
        if include_rx and not any(rx.fullmatch(name) for rx in include_rx):
            continue
        if any(rx.fullmatch(name) for rx in exclude_rx):
            continue
        has_desc = bool(cfg.get_table_description(dbl, name.lower()))
        documented_cols = sum(
            1 for v in cfg.get_column_descriptions(dbl, name.lower()).values() if v
        )
        ncols = t["num_columns"] or 0
        if has_desc and documented_cols >= ncols:
            status = "documented"
        elif has_desc or documented_cols:
            status = "partial"
        else:
            status = "undocumented"
        counts[status] += 1
        if t["rows"] == 0:
            empty += 1
        if only_undocumented and status == "documented":
            continue
        entries.append(
            {
                "name": name,
                "owner": t["owner"],
                "rows": t["rows"],
                "num_columns": t["num_columns"],
                "relationships": rels.get(t["tabid"], 0),
                "has_description": has_desc,
                "documented_columns": documented_cols,
                "status": status,
            }
        )

    entries.sort(key=lambda e: (-e["rows"], -e["relationships"], e["name"]))
    result: dict[str, Any] = {"database": db}
    db_desc = cfg.get_database_description(dbl)
    if db_desc:
        result["database_description"] = db_desc
    result["row_count_source"] = source
    result["summary"] = {
        "tables_considered": sum(counts.values()),
        "empty_tables": empty,
        **counts,
        "returned": min(limit, len(entries)),
    }
    result["tables"] = entries[:limit]
    result["descriptions_file"] = str(cfg.descriptions_file or "")
    return result


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    tags={"discovery"},
)
def save_descriptions(
    database: str,
    table: str | None = None,
    description: str | None = None,
    columns: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Save human-confirmed descriptions for a database, a table and/or its columns.

    Writes only to the server's descriptions file (never to the database or
    the main config). Saved text is immediately returned by describe_table,
    list_tables and search_schema. Save only what the user has confirmed or
    what is certain from the schema; existing text is overwritten, and empty
    values are ignored (they never erase anything).

    Args:
        database: Informix database name.
        table: Table name. Omit to set the database's own description.
        description: One-line description of the table (or of the database
            when ``table`` is omitted), in business terms.
        columns: ``{column_name: description}`` for the table's columns. The
            table and every column must exist (checked against the catalog).

    Returns ``{"saved": true, "database", "table"?, "columns_saved",
    "descriptions_file"}``.
    """
    cfg = _cfg()
    db = _require(database, "database")
    tbl = _optional(table)
    desc = _optional(description)
    cols = {str(k).strip(): str(v) for k, v in (columns or {}).items() if k}
    if tbl is None and cols:
        raise ToolError("'columns' requires 'table'")
    if not desc and not cols:
        raise ToolError("Nothing to save: give 'description' and/or 'columns'")

    table_name: str | None = None
    if tbl is not None:
        data = _run(db, lambda conn: catalog.describe_table(conn, tbl))
        table_name = data["table"]["name"]
        known = {c["name"].lower() for c in data["columns"]}
        unknown = sorted(c for c in cols if c.lower() not in known)
        if unknown:
            raise ToolError(
                f"Unknown column(s) in {table_name}: {', '.join(unknown)}. "
                "Nothing was saved; check the names with describe_table."
            )

    try:
        path = _save_descriptions(cfg, db, table_name, desc, cols)
    except (OSError, ValueError) as exc:
        raise ToolError(f"Could not save descriptions: {exc}") from exc

    result: dict[str, Any] = {"saved": True, "database": db}
    if table_name:
        result["table"] = table_name
    result["columns_saved"] = len([v for v in cols.values() if v.strip()])
    result["descriptions_file"] = str(path)
    return result


_AUTODISCOVER = """\
Let's document the Informix database `{database}` together, so that this and \
future sessions understand its cryptic legacy table and column names. You \
drive the process with the jurassic-mcp tools; I confirm or correct your \
guesses. Talk to me in the language I write in, and keep messages short.

Rules:
- Only use metadata tools (discovery_overview, describe_table, list_indexes, \
list_foreign_keys, search_schema) and save_descriptions. Do not query table data.
- Call the metadata tools yourself without asking permission.
- Never present a guess as fact. Only save what I confirmed or what is \
unambiguous from the schema.

Steps:
1. Call discovery_overview(database="{database}", limit={limit}, \
exclude="{exclude}", only_undocumented=true). Show me a compact ranking \
(table, rows, columns, FK relationships, status) and the row_count_source. \
Point out tables that look like framework/menu/temp/log tables and ask \
whether to exclude them; let me reorder or pick tables. If the database has \
no description, ask me in one question what the database is for and save it \
(save_descriptions without table).
2. For each chosen table, largest first:
   a. Call describe_table, list_indexes and list_foreign_keys.
   b. Infer the meaning of the table and each column from names, types, \
keys, relationships, naming prefixes shared with other tables and \
descriptions already saved for related tables. Legacy names are often \
abbreviations, frequently Spanish/Catalan (e.g. cod=código, n_/num=número, \
fec=fecha, imp=importe, cli=cliente, prv=proveedor, art=artículo, \
alb=albarán, fac=factura, ped=pedido, alm=almacén, emp=empresa, \
eje=ejercicio, ser=serie, cab=cabecera, lin=línea); treat these as hints, \
not facts.
   c. Show one proposal: a one-line table description plus a list of \
columns with proposed description and confidence (high / medium / low). \
Then ask me at most 5 short questions, only about medium/low items, each \
with your best guess so I can answer "ok".
   d. When I answer, call save_descriptions with the table description and \
every confirmed column (high-confidence ones included unless I rejected \
them). Leave out anything still unclear.
   e. Report progress (e.g. "3/{limit} tables done") and continue with the \
next table. I can say "skip", "stop", or name another table at any time.
3. When finished or stopped, summarise what was saved and where \
(descriptions_file), and which tables remain undocumented.
"""


@mcp.prompt(
    name="autodiscover",
    title="Autodiscover database",
    description=(
        "Interactively document a legacy Informix database: ranks tables by "
        "row count, proposes table/column descriptions from the schema, asks "
        "you to confirm, and saves them for future sessions."
    ),
    tags={"discovery"},
)
def autodiscover(database: str, max_tables: str = "15", exclude: str = "") -> str:
    """Build the autodiscover conversation starter.

    Args:
        database: Informix database to document.
        max_tables: How many of the largest undocumented tables to go through.
        exclude: Comma-separated table name patterns to skip (e.g. "s_%,tmp%").
    """
    db = _require(database, "database")
    try:
        limit = max(1, min(int(str(max_tables).strip() or "15"), 200))
    except ValueError:
        limit = 15
    return _AUTODISCOVER.format(
        database=db, limit=limit, exclude=(exclude or "").replace('"', "")
    )
