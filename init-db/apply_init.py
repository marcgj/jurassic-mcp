"""Init sidecar for the docker compose test environment.

Waits until the Informix server accepts connections, creates the
``stores_demo`` database and loads ``init.sql`` into it.

Idempotent: if the database already exists it does nothing, unless the
environment variable ``FORCE_REINIT=1`` is set, in which case the database
is dropped and recreated.

Environment variables:
  JURASSIC_MCP_CONFIG  config file (handled by jurassic_mcp.config)
  INIT_SQL             path of the SQL script (default /work/init.sql)
  INIT_DATABASE        database to create (default stores_demo)
  FORCE_REINIT         "1"/"true" to drop and recreate the database
  INIT_WAIT_ATTEMPTS   connection attempts before giving up (default 60)
  INIT_WAIT_SECONDS    seconds between attempts (default 5)
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import IfxPy as ifx  # type: ignore[import-untyped]

from jurassic_mcp.config import load_config
from jurassic_mcp.db import connect

_LOG_PREFIX = "[informix-init]"
_DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def log(msg: str) -> None:
    print(f"{_LOG_PREFIX} {msg}", file=sys.stderr, flush=True)


def split_sql_script(sql_text: str) -> list[str]:
    """Split an Informix SQL script into statements.

    Handles ``--`` line comments, ``/* */`` and ``{ }`` block comments, and
    single/double quoted strings (with doubled-quote escaping), so semicolons
    or comment markers inside literals do not break statements.
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql_text)

    while i < n:
        ch = sql_text[i]
        nxt = sql_text[i + 1] if i + 1 < n else ""

        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n:
                if sql_text[j] == quote:
                    if j + 1 < n and sql_text[j + 1] == quote:  # escaped ''
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql_text[i : j + 1])
            i = j + 1
        elif ch == "-" and nxt == "-":
            end = sql_text.find("\n", i)
            i = n if end == -1 else end  # keep the newline itself
        elif ch == "/" and nxt == "*":
            end = sql_text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            buf.append(" ")
        elif ch == "{":
            end = sql_text.find("}", i + 1)
            i = n if end == -1 else end + 1
            buf.append(" ")
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _exec(conn, sql: str) -> None:
    result = ifx.exec_immediate(conn, sql)
    if result is False:
        raise RuntimeError(ifx.stmt_errormsg() or "statement failed")


def wait_for_server(cfg, attempts: int, delay: float) -> None:
    """Block until a connection to sysmaster succeeds."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with connect(cfg.informix, "sysmaster") as conn:
                stmt = ifx.exec_immediate(
                    conn, "SELECT COUNT(*) FROM systables WHERE tabid = 1"
                )
                if stmt is False:
                    raise RuntimeError(ifx.stmt_errormsg() or "probe query failed")
            log(f"Informix is accepting connections (attempt {attempt})")
            return
        except Exception as exc:  # noqa: BLE001 - retry on anything
            last_error = exc
            log(f"waiting for Informix ({attempt}/{attempts}): {exc}")
            time.sleep(delay)
    raise TimeoutError(
        f"Informix not reachable after {attempts} attempts: {last_error}"
    )


def database_exists(cfg, db_name: str) -> bool:
    with connect(cfg.informix, "sysmaster") as conn:
        stmt = ifx.prepare(conn, "SELECT name FROM sysdatabases WHERE name = ?")
        if not stmt or not ifx.execute(stmt, (db_name,)):
            raise RuntimeError(ifx.stmt_errormsg() or "sysdatabases lookup failed")
        row = ifx.fetch_assoc(stmt)
        return isinstance(row, dict) and str(row.get("name", "")).strip() == db_name


def drop_database(cfg, db_name: str) -> None:
    with connect(cfg.informix, "sysmaster") as conn:
        _exec(conn, f"DROP DATABASE {db_name}")


def main() -> int:
    script_path = Path(os.environ.get("INIT_SQL", "/work/init.sql"))
    db_name = os.environ.get("INIT_DATABASE", "stores_demo")
    force = os.environ.get("FORCE_REINIT", "").strip().lower() in {"1", "true", "yes"}
    attempts = int(os.environ.get("INIT_WAIT_ATTEMPTS", "60"))
    delay = float(os.environ.get("INIT_WAIT_SECONDS", "5"))

    if not _DB_NAME_RE.match(db_name):
        log(f"invalid database name: {db_name!r}")
        return 2

    try:
        statements = split_sql_script(script_path.read_text(encoding="utf-8"))
    except OSError as exc:
        log(f"cannot read {script_path}: {exc}")
        return 2
    if not statements:
        log(f"{script_path} contains no statements")
        return 2

    cfg = load_config()
    log(
        f"target {cfg.informix.host}:{cfg.informix.port} "
        f"server={cfg.informix.server} protocol={cfg.informix.protocol}"
    )

    try:
        wait_for_server(cfg, attempts, delay)
    except TimeoutError as exc:
        log(str(exc))
        return 1

    try:
        if database_exists(cfg, db_name):
            if not force:
                log(f"database {db_name} already exists; skipping (set FORCE_REINIT=1 to recreate)")
                return 0
            log(f"FORCE_REINIT set: dropping existing database {db_name}")
            drop_database(cfg, db_name)

        log(f"creating database {db_name} WITH LOG")
        with connect(cfg.informix, "sysmaster") as conn:
            _exec(conn, f"CREATE DATABASE {db_name} WITH LOG")
    except Exception as exc:  # noqa: BLE001
        log(f"failed to prepare database {db_name}: {exc}")
        return 1

    log(f"applying {len(statements)} statements from {script_path}")
    current = ""
    try:
        with connect(cfg.informix, db_name) as conn:
            for idx, current in enumerate(statements, start=1):
                _exec(conn, current)
                log(f"  [{idx}/{len(statements)}] ok: {current.splitlines()[0][:70]}")
    except Exception as exc:  # noqa: BLE001
        log(f"statement failed: {exc}\n--- statement ---\n{current}\n-----------------")
        # Leave no half-initialised database behind, so the next run retries.
        try:
            drop_database(cfg, db_name)
            log(f"dropped partially initialised database {db_name}")
        except Exception as drop_exc:  # noqa: BLE001
            log(f"could not drop {db_name} after failure: {drop_exc}")
        return 1

    log(f"{db_name} initialised successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
