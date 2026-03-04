"""Informix database connection management."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator

try:
    import IfxPy as ifx  # type: ignore[import-untyped]

    _USING_IFXPY = True
except ImportError:  # pragma: no cover - fallback for newer Python runtimes
    import ibm_db as ifx  # type: ignore[import-untyped]

    _USING_IFXPY = False

if TYPE_CHECKING:
    from .config import InformixConfig


def _build_connection_string(
    cfg: InformixConfig,
    database: str,
    *,
    host_override: str | None = None,
    port_override: int | None = None,
) -> str:
    """Build an IfxPy connection string from config + target database."""
    host = host_override or cfg.host
    port = port_override or cfg.port

    if _USING_IFXPY:
        parts = [
            f"SERVER={cfg.server}",
            f"DATABASE={database}",
            f"HOST={host}",
            f"SERVICE={port}",
            f"UID={cfg.user}",
            f"PWD={cfg.password}",
            f"DB_LOCALE={cfg.db_locale}",
            f"CLIENT_LOCALE={cfg.client_locale}",
        ]
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
    # Append any free-form driver options
    for key, value in cfg.driver_options.items():
        parts.append(f"{key}={value}")

    return ";".join(parts) + ";"


def _apply_env_options(cfg: InformixConfig) -> None:
    """Export driver_options that Informix reads from environment variables.

    Some options like ``GL_DATE``, ``GL_DATETIME``, ``DBDATE`` are only
    honoured when set as env-vars rather than connection-string parameters.
    We export *all* driver_options to the environment so that both
    connection-string and env-var based settings are covered.
    """
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
                conn_str = _build_connection_string(
                    cfg,
                    database,
                    host_override=host,
                    port_override=port,
                )
                try:
                    conn = ifx.connect(conn_str, "", "")
                except Exception as exc:  # pragma: no cover - depends on driver/runtime
                    last_error = exc
                    conn = False
                    continue
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
