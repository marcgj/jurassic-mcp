"""Informix database connection management."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Generator

import IfxPy as ifx  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from .config import InformixConfig


def _ensure_sqlhosts(cfg: InformixConfig) -> None:
    """Ensure INFORMIXSQLHOSTS points to a valid sqlhosts file for IfxPy."""
    if os.environ.get("INFORMIXSQLHOSTS"):
        return

    sqlhosts_path = Path(tempfile.gettempdir()) / "jurassic_mcp.sqlhosts"
    sqlhosts_line = f"{cfg.server} {cfg.protocol} {cfg.host} {cfg.port}\n"

    if (
        not sqlhosts_path.exists()
        or sqlhosts_path.read_text(encoding="utf-8") != sqlhosts_line
    ):
        sqlhosts_path.write_text(sqlhosts_line, encoding="utf-8")

    os.environ.setdefault("INFORMIXSQLHOSTS", str(sqlhosts_path))


def _build_connection_string(cfg: InformixConfig, database: str | None) -> str:
    """Build an IfxPy connection string from config + target database."""
    parts = [
        f"SERVER={cfg.server}",
        f"HOST={cfg.host}",
        f"SERVICE={cfg.port}",
        f"PROTOCOL={cfg.protocol}",
        f"UID={cfg.user}",
        f"PWD={cfg.password}",
        f"DB_LOCALE={cfg.db_locale}",
        f"CLIENT_LOCALE={cfg.client_locale}",
    ]
    if database:
        parts.insert(1, f"DATABASE={database}")
    return ";".join(parts) + ";"


def _apply_env_options(cfg: InformixConfig) -> None:
    """Export driver_options that Informix reads from environment variables.

    Some options like ``GL_DATE``, ``GL_DATETIME``, ``DBDATE`` are only
    honoured when set as env-vars rather than connection-string parameters.
    We export *all* driver_options to the environment so that both
    connection-string and env-var based settings are covered.
    """
    os.environ.setdefault("INFORMIXSERVER", cfg.server)
    _ensure_sqlhosts(cfg)
    if "INFORMIXDIR" not in os.environ:
        ifx_path = getattr(ifx, "__file__", "")
        if ifx_path:
            site_packages = Path(ifx_path).resolve().parent
            bundled_driver_dir = site_packages / "onedb-odbc-driver"
            if bundled_driver_dir.exists():
                os.environ.setdefault("INFORMIXDIR", str(bundled_driver_dir))

    for key, value in cfg.driver_options.items():
        os.environ.setdefault(key, str(value))


@contextmanager
def connect(cfg: InformixConfig, database: str) -> Generator:
    """Context manager that yields an IfxPy connection and closes it on exit.

    Usage::

        with connect(cfg, "mydb") as conn:
            stmt = IfxPy.exec_immediate(conn, "SELECT ...")
    """
    _apply_env_options(cfg)
    conn = None
    last_error: Exception | None = None

    base = _build_connection_string(cfg, database)
    no_locales = base.replace(f"DB_LOCALE={cfg.db_locale};", "").replace(
        f"CLIENT_LOCALE={cfg.client_locale};", ""
    )
    variants = [base] if base == no_locales else [base, no_locales]

    try:
        for conn_str in variants:
            try:
                conn = ifx.connect(conn_str, "", "")
                if conn is not None and conn is not False:
                    break
            except Exception as exc:
                last_error = exc
                conn = None

        if not conn:
            detail = str(last_error) if last_error else ifx.conn_errormsg()
            raise ConnectionError(
                f"Failed to connect to Informix '{database}' "
                f"({cfg.host}:{cfg.port}): {detail}"
            )
        yield conn
    except Exception as exc:
        if not conn:
            raise ConnectionError(
                f"Cannot connect to Informix at {cfg.host}:{cfg.port} "
                f"(server={cfg.server}, database={database}): {exc}"
            ) from exc
        raise
    finally:
        if conn and conn is not False:
            try:
                ifx.close(conn)
            except Exception:
                pass
