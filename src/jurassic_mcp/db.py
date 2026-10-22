"""Informix database connection management (IfxPy / legacy SQLI only).

Library code here never writes to stdout (MCP STDIO transport).
"""

from __future__ import annotations

import atexit
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator

import IfxPy as ifx  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from .config import InformixConfig


# --- Process-wide environment setup ------------------------------------------

_ENV_LOCK = threading.Lock()
# Serialises ifx.connect(): the Informix client reads process env vars and
# shared driver state while establishing a connection.
_CONNECT_LOCK = threading.Lock()
_env_key: tuple[Any, ...] | None = None
_own_sqlhosts: str | None = None
_user_sqlhosts: bool | None = None


def _remove_own_sqlhosts() -> None:
    if _own_sqlhosts:
        try:
            os.unlink(_own_sqlhosts)
        except OSError:
            pass


def _ensure_sqlhosts(cfg: InformixConfig) -> None:
    """Point INFORMIXSQLHOSTS at a valid sqlhosts file for IfxPy.

    A user-provided INFORMIXSQLHOSTS (present before our first call) is
    respected. Otherwise a private per-process file is created with mkstemp
    and removed at interpreter exit.
    """
    global _own_sqlhosts, _user_sqlhosts

    if _user_sqlhosts is None:
        _user_sqlhosts = bool(os.environ.get("INFORMIXSQLHOSTS"))
    if _user_sqlhosts:
        return

    line = f"{cfg.server} {cfg.protocol} {cfg.host} {cfg.port}\n"
    if _own_sqlhosts is None:
        fd, path = tempfile.mkstemp(prefix="jurassic_mcp_", suffix=".sqlhosts")
        _own_sqlhosts = path
        atexit.register(_remove_own_sqlhosts)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(line)
    else:
        Path(_own_sqlhosts).write_text(line, encoding="utf-8")
    os.environ["INFORMIXSQLHOSTS"] = _own_sqlhosts


def _apply_env_options(cfg: InformixConfig) -> None:
    """Export Informix client environment once per process (per config).

    Some options like ``GL_DATE``, ``GL_DATETIME``, ``DBDATE`` are only
    honoured as environment variables, so all ``driver_options`` are exported.

    Precedence: values in ``config.yml`` ``driver_options`` are assigned
    explicitly and therefore WIN over pre-existing environment variables.
    Built-in defaults (INFORMIXCONTIME, INFORMIXCONRETRY, INFORMIXSERVER,
    INFORMIXDIR) only apply when neither the environment nor driver_options
    set them.
    """
    global _env_key

    key = (
        cfg.server,
        cfg.protocol,
        cfg.host,
        str(cfg.port),
        getattr(cfg, "connect_timeout", 10),
        tuple(sorted(cfg.driver_options.items())),
    )
    with _ENV_LOCK:
        if _env_key == key:
            return

        os.environ.setdefault("INFORMIXSERVER", cfg.server)
        _ensure_sqlhosts(cfg)
        if "INFORMIXDIR" not in os.environ:
            ifx_path = getattr(ifx, "__file__", "")
            if ifx_path:
                bundled = Path(ifx_path).resolve().parent / "onedb-odbc-driver"
                if bundled.exists():
                    os.environ["INFORMIXDIR"] = str(bundled)

        # Fail fast on unreachable hosts (driver default is 60s x retries).
        timeout = str(getattr(cfg, "connect_timeout", 10))
        if "INFORMIXCONTIME" not in cfg.driver_options:
            os.environ.setdefault("INFORMIXCONTIME", timeout)
        if "INFORMIXCONRETRY" not in cfg.driver_options:
            os.environ.setdefault("INFORMIXCONRETRY", "0")

        # config.yml wins over the inherited environment (explicit assignment).
        for opt_key, value in cfg.driver_options.items():
            os.environ[opt_key] = str(value)

        _env_key = key


# --- Connection string ---------------------------------------------------------

def _quote(value: Any) -> str:
    """ODBC-quote a connection-string value when it needs it.

    Values containing ``;``, ``{``, ``}``, ``=`` or surrounding whitespace are
    wrapped in braces, with ``}`` escaped as ``}}``. Plain values are left
    untouched.
    """
    text = str(value)
    if text and not any(c in text for c in ";{}=") and text == text.strip():
        return text
    return "{" + text.replace("}", "}}") + "}"


def _build_connection_string(
    cfg: InformixConfig, database: str | None, *, with_locales: bool = True
) -> str:
    """Build an IfxPy connection string from config + target database."""
    parts = [f"SERVER={cfg.server}"]
    if database:
        parts.append(f"DATABASE={_quote(database)}")
    parts += [
        f"HOST={cfg.host}",
        f"SERVICE={cfg.port}",
        f"PROTOCOL={cfg.protocol}",
        f"UID={_quote(cfg.user)}",
        f"PWD={_quote(cfg.password)}",
    ]
    if with_locales:
        if cfg.db_locale:
            parts.append(f"DB_LOCALE={cfg.db_locale}")
        if cfg.client_locale:
            parts.append(f"CLIENT_LOCALE={cfg.client_locale}")
    return ";".join(parts) + ";"


# --- Error handling ------------------------------------------------------------

_LOCALE_CODES = {-23197, -23101, -23104}
_SQLCODE_RE = re.compile(r"SQLCODE\s*[=:]?\s*(-\d+)", re.IGNORECASE)
_BARE_CODE_RE = re.compile(r"(?<![\w.])(-\d{3,5})\b")


def _sqlcode(text: str) -> int | None:
    match = _SQLCODE_RE.search(text) or _BARE_CODE_RE.search(text)
    return int(match.group(1)) if match else None


def _hint(code: int | None, cfg: InformixConfig, database: str) -> str:
    if code in (-908, -930, -25596, -25572, -25573):
        return (
            "host unreachable or server name/host/port mismatch: check the "
            "host is reachable and listening, that 'server' equals the "
            "DBSERVERNAME/DBSERVERALIASES of the instance and that host:port "
            "matches its onsoctcp listener (`onstat -g ntt` on the server). "
            "Note 9089 is usually the DRDA port, not onsoctcp (9088)."
        )
    if code in (-951, -952, -956, -1809):
        return (
            "authentication failed: check informix.user/password "
            "(or INFORMIX_USER/INFORMIX_PASSWORD) and that the user may "
            "connect from this host."
        )
    if code == -329 or code == -387:
        return (
            f"database '{database}' not found or no CONNECT permission: "
            "list databases with `SELECT name FROM sysmaster:sysdatabases`."
        )
    if code in _LOCALE_CODES:
        return (
            "locale mismatch: set informix.db_locale to the database locale, "
            f"see `SELECT dbs_collate FROM sysmaster:sysdbslocale "
            f"WHERE dbs_dbsname='{database}'`."
        )
    return ""


def _error_text(exc: Exception | None) -> str:
    text = str(exc).strip() if exc else ""
    if not text:
        try:
            text = str(ifx.conn_errormsg() or "").strip()
        except Exception:
            text = ""
    return text or "unknown error"


def _connection_error(
    cfg: InformixConfig, database: str, exc: Exception | None
) -> ConnectionError:
    detail = _error_text(exc)
    code = _sqlcode(detail)
    msg = (
        f"Cannot connect to Informix database '{database}' at "
        f"{cfg.host}:{cfg.port} (server={cfg.server}, protocol={cfg.protocol})"
    )
    if code is not None:
        msg += f" [SQLCODE {code}]"
    msg += f": {detail}"
    hint = _hint(code, cfg, database)
    if hint:
        msg += f"\nHint: {hint}"
    return ConnectionError(msg)


def _try_connect(conn_str: str) -> tuple[Any, Exception | None]:
    try:
        with _CONNECT_LOCK:
            conn = ifx.connect(conn_str, "", "")
    except Exception as exc:  # IfxPy raises plain Exception
        return None, exc
    if conn is None or conn is False:
        return None, None
    return conn, None


def _open(cfg: InformixConfig, database: str) -> Any:
    _apply_env_options(cfg)
    conn, err = _try_connect(_build_connection_string(cfg, database))
    if conn is None and (cfg.db_locale or cfg.client_locale):
        if _sqlcode(_error_text(err)) in _LOCALE_CODES:
            conn, err = _try_connect(
                _build_connection_string(cfg, database, with_locales=False)
            )
    if conn is None:
        raise _connection_error(cfg, database, err)
    return conn


@contextmanager
def connect(cfg: InformixConfig, database: str) -> Generator:
    """Context manager that yields an IfxPy connection and closes it on exit.

    Connection failures raise a single ``ConnectionError`` with a hint;
    exceptions raised inside the ``with`` body propagate unchanged.

    Usage::

        with connect(cfg, "mydb") as conn:
            stmt = IfxPy.exec_immediate(conn, "SELECT ...")
    """
    conn = _open(cfg, database)
    try:
        yield conn
    finally:
        try:
            ifx.close(conn)
        except Exception:
            pass
