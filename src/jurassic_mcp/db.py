"""Informix database connection management."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Generator

try:
    import IfxPy as ifx  # type: ignore[import-untyped]

    _USING_IFXPY = True
except (
    ImportError
):  # pragma: no cover - fallback for package naming/runtime differences
    try:
        import ifxpy as ifx  # type: ignore[import-untyped]

        _USING_IFXPY = True
    except ImportError:  # pragma: no cover - fallback for newer Python runtimes
        import ibm_db as ifx  # type: ignore[import-untyped]

        _USING_IFXPY = False

if TYPE_CHECKING:
    from .config import InformixConfig


def _ensure_sqlhosts(cfg: InformixConfig) -> None:
    """Ensure INFORMIXSQLHOSTS points to a valid sqlhosts file for legacy IfxPy."""
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


def _build_connection_string(
    cfg: InformixConfig,
    database: str | None,
    *,
    host_override: str | None = None,
    port_override: int | None = None,
) -> str:
    """Build a connection string from config + target database."""
    host = host_override or cfg.host
    port = port_override or cfg.port

    if _USING_IFXPY:
        parts = [
            f"SERVER={cfg.server}",
            f"HOST={host}",
            f"SERVICE={port}",
            f"PROTOCOL={cfg.protocol}",
            f"UID={cfg.user}",
            f"PWD={cfg.password}",
            f"DB_LOCALE={cfg.db_locale}",
            f"CLIENT_LOCALE={cfg.client_locale}",
        ]
        if database:
            parts.insert(1, f"DATABASE={database}")
    else:
        # ibm_db follows DB2 CLI keywords and works with Informix DRDA listener.
        parts = [
            f"DATABASE={database}",
            f"HOSTNAME={host}",
            f"PORT={port}",
            "PROTOCOL=TCPIP",
            f"UID={cfg.user}",
            f"PWD={cfg.password}",
        ]
    # Append any free-form driver options only for ibm_db connection strings.
    # For IfxPy, options like GL_DATE/DBDATE are expected as environment vars.
    if not _USING_IFXPY:
        for key, value in cfg.driver_options.items():
            parts.append(f"{key}={value}")

    return ";".join(parts) + ";"


def _build_ifxpy_attempt_strings(
    cfg: InformixConfig,
    database: str,
    *,
    host_override: str,
    port_override: int,
) -> list[tuple[str, bool]]:
    """Return IfxPy connection-string variants and whether DATABASE switch is needed."""
    variants: list[tuple[str, bool]] = []

    base = _build_connection_string(
        cfg,
        database,
        host_override=host_override,
        port_override=port_override,
    )
    variants.append((base, False))

    without_locales = base.replace(f"DB_LOCALE={cfg.db_locale};", "").replace(
        f"CLIENT_LOCALE={cfg.client_locale};", ""
    )
    if without_locales != base:
        variants.append((without_locales, False))

    no_db = _build_connection_string(
        cfg,
        None,
        host_override=host_override,
        port_override=port_override,
    )
    variants.append((no_db, True))

    no_db_no_locales = no_db.replace(f"DB_LOCALE={cfg.db_locale};", "").replace(
        f"CLIENT_LOCALE={cfg.client_locale};", ""
    )
    if no_db_no_locales != no_db:
        variants.append((no_db_no_locales, True))

    return list(dict.fromkeys(variants))


def _apply_env_options(cfg: InformixConfig) -> None:
    """Export driver_options that Informix reads from environment variables.

    Some options like ``GL_DATE``, ``GL_DATETIME``, ``DBDATE`` are only
    honoured when set as env-vars rather than connection-string parameters.
    We export *all* driver_options to the environment so that both
    connection-string and env-var based settings are covered.
    """
    if _USING_IFXPY:
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


def _candidate_hosts(primary_host: str) -> list[str]:
    """Return connection host candidates for mixed host/container runs."""
    hosts: list[str] = [primary_host]

    if primary_host in {"localhost", "127.0.0.1"}:
        hosts.append("informix")
    elif primary_host == "informix":
        hosts.extend(["localhost", "127.0.0.1"])

    # Keep order stable while removing duplicates.
    return list(dict.fromkeys(hosts))


def _candidate_ports(primary_port: int) -> list[int]:
    """Return connection port candidates for driver/protocol compatibility."""
    ports: list[int] = [primary_port]

    # ibm_db connects to Informix through DRDA in typical container setups.
    if not _USING_IFXPY:
        if primary_port == 9088:
            ports.append(9089)
        elif primary_port == 9089:
            ports.append(9088)

    return list(dict.fromkeys(ports))


@contextmanager
def connect(cfg: InformixConfig, database: str) -> Generator:
    """Context manager that yields an IfxPy connection and closes it on exit.

    Usage::

        with connect(cfg, "mydb") as conn:
            stmt = IfxPy.exec_immediate(conn, "SELECT ...")
    """
    _apply_env_options(cfg)
    conn = None
    attempted_endpoints: list[str] = []
    last_error: Exception | None = None
    try:
        for host in _candidate_hosts(cfg.host):
            for port in _candidate_ports(cfg.port):
                attempted_endpoints.append(f"{host}:{port}")
                if _USING_IFXPY:
                    attempt_variants = _build_ifxpy_attempt_strings(
                        cfg,
                        database,
                        host_override=host,
                        port_override=port,
                    )
                else:
                    conn_str = _build_connection_string(
                        cfg,
                        database,
                        host_override=host,
                        port_override=port,
                    )
                    attempt_variants = [(conn_str, False)]

                for conn_str, needs_database_switch in attempt_variants:
                    try:
                        conn = ifx.connect(conn_str, "", "")
                        if (
                            _USING_IFXPY
                            and needs_database_switch
                            and conn is not False
                            and conn is not None
                            and database
                        ):
                            ifx.exec_immediate(conn, f"DATABASE {database}")
                    except (
                        Exception
                    ) as exc:  # pragma: no cover - depends on driver/runtime
                        last_error = exc
                        conn = False
                        continue
                    if conn is not False:
                        break
                if conn is not False:
                    break
            if conn is not False:
                break

        if conn is False:
            detail = str(last_error) if last_error else ifx.conn_errormsg()
            raise ConnectionError(
                f"Failed to connect to Informix database '{database}' "
                f"(endpoints tried: {', '.join(attempted_endpoints)}): "
                f"{detail}"
            )
        yield conn
    except Exception as exc:
        # Re-raise with a friendlier message when it's a connection issue
        if conn is None or conn is False:
            raise ConnectionError(
                f"Cannot connect to Informix at {cfg.host}:{cfg.port} "
                f"(endpoints tried: {', '.join(attempted_endpoints) or f'{cfg.host}:{cfg.port}'}) "
                f"(server={cfg.server}, database={database}): {exc}"
            ) from exc
        raise
    finally:
        if conn and conn is not False:
            try:
                ifx.close(conn)
            except Exception:
                pass
