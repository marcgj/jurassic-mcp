"""Configuration loading and parsing for jurassic-mcp.

Library code in this module never writes to stdout (it is the MCP STDIO
transport); warnings go to stderr.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_CONFIG_PATH = "JURASSIC_MCP_CONFIG"
DEFAULT_CONFIG_PATH = "./config.yml"
ENV_DESCRIPTIONS_PATH = "JURASSIC_MCP_DESCRIPTIONS"
DEFAULT_DESCRIPTIONS_NAME = "descriptions.yml"

DEFAULT_USER = "informix"
DEFAULT_PASSWORD = "in4mix"
DEFAULT_CONNECT_TIMEOUT = 10


def _warn(message: str) -> None:
    print(f"[jurassic-mcp] WARNING: {message}", file=sys.stderr, flush=True)


@dataclass
class InformixConfig:
    """Informix connection settings."""

    host: str = "localhost"
    # Numeric port or a service name from /etc/services (e.g. "sqlexec").
    port: int | str = 9088
    protocol: str = "onsoctcp"
    server: str = "informix"
    user: str = DEFAULT_USER
    password: str = DEFAULT_PASSWORD
    # Empty string = not sent; the driver/server defaults apply.
    db_locale: str = ""
    client_locale: str = ""
    driver_options: dict[str, str] = field(default_factory=dict)
    # Seconds for INFORMIXCONTIME (connection attempt timeout).
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT


@dataclass
class TableDescription:
    """User-provided description for a table and its columns.

    Column keys are stored lower-cased.
    """

    description: str = ""
    columns: dict[str, str] = field(default_factory=dict)


@dataclass
class DatabaseDescription:
    """User-provided descriptions for a database and its tables.

    Table keys are stored lower-cased.
    """

    description: str = ""
    tables: dict[str, TableDescription] = field(default_factory=dict)


@dataclass
class AppConfig:
    """Root application configuration.

    ``descriptions`` keys (database, table, column) are lower-cased; the
    lookup helpers below are case-insensitive.
    """

    informix: InformixConfig = field(default_factory=InformixConfig)
    descriptions: dict[str, DatabaseDescription] = field(default_factory=dict)
    # Writable YAML file (same format as the ``descriptions`` section) where
    # descriptions saved through the MCP tools are stored. It is overlaid on
    # top of the descriptions from the main config file.
    descriptions_file: Path | None = None

    # --- Lookup helpers ---------------------------------------------------

    def _db(self, database: str) -> DatabaseDescription | None:
        return self.descriptions.get(str(database).strip().lower())

    def _table(self, database: str, table: str) -> TableDescription | None:
        db_desc = self._db(database)
        if db_desc is None:
            return None
        return db_desc.tables.get(str(table).strip().lower())

    def get_database_description(self, database: str) -> str:
        """Return the free-text description for a database, or empty string."""
        db_desc = self._db(database)
        return db_desc.description if db_desc else ""

    def get_table_description(self, database: str, table: str) -> str:
        """Return the free-text description for a table, or empty string."""
        tbl_desc = self._table(database, table)
        return tbl_desc.description if tbl_desc else ""

    def get_column_descriptions(self, database: str, table: str) -> dict[str, str]:
        """Return ``{column_name: description}`` (lower-case keys) or empty dict."""
        tbl_desc = self._table(database, table)
        return tbl_desc.columns if tbl_desc else {}


def _parse_port(value: Any, source: str) -> int | str:
    """Return an int port, or a service name string (e.g. ``sqlexec``)."""
    if isinstance(value, bool):
        raise ValueError(f"Invalid Informix port {value!r} from {source}")
    if isinstance(value, int):
        port = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError(f"Empty Informix port from {source}")
        if not text.isdigit():
            # Service name resolved by the client via /etc/services.
            if any(c.isspace() or c in ";{}" for c in text):
                raise ValueError(
                    f"Invalid Informix port/service {text!r} from {source}: "
                    "expected a number or a service name like 'sqlexec'"
                )
            return text
        port = int(text)
    if not 0 < port < 65536:
        raise ValueError(f"Informix port {port} from {source} is out of range 1-65535")
    return port


def _parse_int(value: Any, source: str) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Expected an integer for {source}, got {value!r}") from None
    if result < 0:
        raise ValueError(f"Expected a non-negative integer for {source}, got {result}")
    return result


def _apply_env_overrides(informix: InformixConfig) -> None:
    """Override InformixConfig fields from environment variables."""
    if val := os.environ.get("INFORMIX_HOST"):
        informix.host = val
    if val := os.environ.get("INFORMIX_PORT"):
        informix.port = _parse_port(val, "INFORMIX_PORT environment variable")
    if val := os.environ.get("INFORMIX_USER"):
        informix.user = val
    if val := os.environ.get("INFORMIX_PASSWORD"):
        informix.password = val
    if val := os.environ.get("INFORMIX_SERVER"):
        informix.server = val


def _key(value: Any) -> str:
    """Normalise a YAML mapping key (may be bool/int) to a lower-case str."""
    return str(value).strip().lower()


def _parse_descriptions(raw: Any) -> dict[str, DatabaseDescription]:
    """Parse the ``descriptions`` section of the YAML config.

    YAML may turn keys like ``on``/``yes``/``no`` into bools and numeric keys
    into ints, so every key is coerced with ``str()`` and lower-cased.
    """
    result: dict[str, DatabaseDescription] = {}
    if not isinstance(raw, dict):
        return result
    for db_name, db_data in raw.items():
        if not isinstance(db_data, dict):
            continue
        db_desc = DatabaseDescription(
            description=str(db_data.get("_description", "") or "")
        )
        for key, value in db_data.items():
            tbl_key = str(key)
            if tbl_key.startswith("_") or not isinstance(value, dict):
                continue
            columns: dict[str, str] = {}
            columns_raw = value.get("columns", {})
            if isinstance(columns_raw, dict):
                for col_name, col_desc in columns_raw.items():
                    columns[_key(col_name)] = "" if col_desc is None else str(col_desc)
            db_desc.tables[_key(tbl_key)] = TableDescription(
                description=str(value.get("_description", "") or ""),
                columns=columns,
            )
        result[_key(db_name)] = db_desc
    return result


def _overlay_descriptions(
    base: dict[str, DatabaseDescription], extra: dict[str, DatabaseDescription]
) -> None:
    """Merge *extra* into *base* in place; non-empty values in *extra* win."""
    for db_key, db_extra in extra.items():
        db_base = base.setdefault(db_key, DatabaseDescription())
        if db_extra.description:
            db_base.description = db_extra.description
        for tbl_key, tbl_extra in db_extra.tables.items():
            tbl_base = db_base.tables.setdefault(tbl_key, TableDescription())
            if tbl_extra.description:
                tbl_base.description = tbl_extra.description
            for col_key, col_desc in tbl_extra.columns.items():
                if col_desc:
                    tbl_base.columns[col_key] = col_desc


def _resolve_descriptions_path(raw: dict[str, Any], config_path: Path | None) -> Path:
    """``JURASSIC_MCP_DESCRIPTIONS`` → ``descriptions_file`` key → next to config."""
    if env := os.environ.get(ENV_DESCRIPTIONS_PATH):
        return Path(env).expanduser()
    if configured := raw.get("descriptions_file"):
        path = Path(str(configured)).expanduser()
        if not path.is_absolute() and config_path is not None:
            path = config_path.parent / path
        return path
    base = config_path.parent if config_path is not None else Path(".")
    return base / DEFAULT_DESCRIPTIONS_NAME


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_dir():
        raise ValueError(f"Descriptions path {path} is a directory, not a file")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in descriptions file {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Descriptions file {path} must contain a YAML mapping")
    return data


_SAVE_LOCK = threading.Lock()


def save_descriptions(
    cfg: AppConfig,
    database: str,
    table: str | None = None,
    description: str | None = None,
    columns: dict[str, str] | None = None,
) -> Path:
    """Persist descriptions to ``cfg.descriptions_file`` and update *cfg*.

    With ``table=None`` *description* applies to the database itself.
    Empty/None values are ignored (they never erase existing text). The file
    uses the same format as the ``descriptions`` section of the config and is
    rewritten atomically; the main config file is never touched.
    """
    if cfg.descriptions_file is None:
        raise ValueError("No descriptions file configured")
    path = cfg.descriptions_file
    db_key = _key(database)
    tbl_key = _key(table) if table else None
    desc = (description or "").strip()
    cols = {
        _key(k): str(v).strip() for k, v in (columns or {}).items() if v and str(v).strip()
    }

    with _SAVE_LOCK:
        raw = _read_yaml_mapping(path)
        db_raw = raw.get(db_key)
        if not isinstance(db_raw, dict):
            db_raw = raw[db_key] = {}
        if tbl_key is None:
            if desc:
                db_raw["_description"] = desc
        else:
            tbl_raw = db_raw.get(tbl_key)
            if not isinstance(tbl_raw, dict):
                tbl_raw = db_raw[tbl_key] = {}
            if desc:
                tbl_raw["_description"] = desc
            if cols:
                col_raw = tbl_raw.get("columns")
                if not isinstance(col_raw, dict):
                    col_raw = tbl_raw["columns"] = {}
                col_raw.update(cols)

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".descriptions-", suffix=".yml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(
                    "# Written by jurassic-mcp (save_descriptions / autodiscover).\n"
                    "# Same format as the `descriptions` section of config.yml;\n"
                    "# entries here override it. Safe to edit by hand.\n"
                )
                yaml.safe_dump(raw, fh, allow_unicode=True, sort_keys=True, width=100)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

        update = DatabaseDescription(description=desc if tbl_key is None else "")
        if tbl_key is not None:
            update.tables[tbl_key] = TableDescription(description=desc, columns=cols)
        _overlay_descriptions(cfg.descriptions, {db_key: update})
    return path


def _resolve_config_path(path: str | Path | None) -> Path | None:
    """Return the config path to load, or ``None`` to use defaults."""
    if path is not None:
        source = "explicit path"
    elif os.environ.get(ENV_CONFIG_PATH):
        path = os.environ[ENV_CONFIG_PATH]
        source = f"{ENV_CONFIG_PATH} environment variable"
    else:
        default = Path(DEFAULT_CONFIG_PATH)
        if default.is_dir():
            raise ValueError(
                f"Config path {default.resolve()} is a directory, not a file "
                "(a Docker bind mount of a missing file creates a directory)"
            )
        if not default.exists():
            _warn(
                f"No config file found ({ENV_CONFIG_PATH} unset and "
                f"{default.resolve()} missing); using built-in defaults"
            )
            return None
        return default

    resolved = Path(path).expanduser()
    if resolved.is_dir():
        raise ValueError(
            f"Config path {resolved} (from {source}) is a directory, not a file. "
            "If running in Docker, the bind-mounted config.yml probably does not "
            "exist on the host, so Docker created a directory instead."
        )
    if not resolved.exists():
        raise FileNotFoundError(
            f"Config file {resolved} (from {source}) does not exist"
        )
    return resolved


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and return the application configuration from a YAML file.

    Resolution order for the config path:
    1. Explicit *path* argument (must exist)
    2. ``JURASSIC_MCP_CONFIG`` environment variable (must exist)
    3. ``./config.yml`` (cwd); if missing, defaults are used with a warning
    """
    resolved = _resolve_config_path(path)

    if resolved is None:
        cfg = AppConfig()
        _apply_env_overrides(cfg.informix)
        _warn_default_credentials(cfg.informix)
        cfg.descriptions_file = _resolve_descriptions_path({}, None)
        _overlay_descriptions(
            cfg.descriptions,
            _parse_descriptions(_read_yaml_mapping(cfg.descriptions_file)),
        )
        return cfg

    try:
        with open(resolved, encoding="utf-8") as fh:
            raw_any = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in config file {resolved}: {exc}") from exc

    if raw_any is None:
        raw: dict[str, Any] = {}
    elif isinstance(raw_any, dict):
        raw = raw_any
    else:
        raise ValueError(
            f"Config file {resolved} must contain a YAML mapping at top level"
        )

    ifx_raw_any = raw.get("informix", {})
    ifx_raw = ifx_raw_any if isinstance(ifx_raw_any, dict) else {}

    driver_options_raw = ifx_raw.get("driver_options", {})
    driver_options: dict[str, str] = {}
    if isinstance(driver_options_raw, dict):
        for key, value in driver_options_raw.items():
            if value is None:
                continue
            driver_options[str(key)] = str(value)

    def _str(name: str, default: str) -> str:
        value = ifx_raw.get(name)
        return default if value is None else str(value)

    informix = InformixConfig(
        host=_str("host", "localhost"),
        port=_parse_port(ifx_raw.get("port", 9088), f"informix.port in {resolved}"),
        protocol=_str("protocol", "onsoctcp"),
        server=_str("server", "informix"),
        user=_str("user", DEFAULT_USER),
        password=_str("password", DEFAULT_PASSWORD),
        db_locale=_str("db_locale", "").strip(),
        client_locale=_str("client_locale", "").strip(),
        driver_options=driver_options,
        connect_timeout=_parse_int(
            ifx_raw.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT),
            f"informix.connect_timeout in {resolved}",
        ),
    )
    _apply_env_overrides(informix)
    _warn_default_credentials(informix)

    descriptions = _parse_descriptions(raw.get("descriptions"))
    descriptions_file = _resolve_descriptions_path(raw, resolved)
    _overlay_descriptions(
        descriptions, _parse_descriptions(_read_yaml_mapping(descriptions_file))
    )

    return AppConfig(
        informix=informix,
        descriptions=descriptions,
        descriptions_file=descriptions_file,
    )


def _warn_default_credentials(informix: InformixConfig) -> None:
    if informix.user == DEFAULT_USER and informix.password == DEFAULT_PASSWORD:
        _warn(
            "using built-in default Informix credentials "
            f"(user={DEFAULT_USER!r}); set informix.user/password in config.yml "
            "or INFORMIX_USER/INFORMIX_PASSWORD"
        )
