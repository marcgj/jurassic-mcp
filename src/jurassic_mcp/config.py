"""Configuration loading and parsing for jurassic-mcp."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_CONFIG_PATH = "JURASSIC_MCP_CONFIG"
DEFAULT_CONFIG_PATH = "./config.yml"


@dataclass
class InformixConfig:
    """Informix connection settings."""

    host: str = "localhost"
    port: int = 9088
    protocol: str = "onsoctcp"
    server: str = "informix"
    user: str = "informix"
    password: str = "in4mix"
    db_locale: str = "en_us.utf8"
    client_locale: str = "en_us.utf8"
    driver_options: dict[str, str] = field(default_factory=dict)


@dataclass
class TableDescription:
    """User-provided description for a table and its columns."""

    description: str = ""
    columns: dict[str, str] = field(default_factory=dict)


@dataclass
class DatabaseDescription:
    """User-provided descriptions for a database and its tables."""

    description: str = ""
    tables: dict[str, TableDescription] = field(default_factory=dict)


@dataclass
class AppConfig:
    """Root application configuration."""

    informix: InformixConfig = field(default_factory=InformixConfig)
    descriptions: dict[str, DatabaseDescription] = field(default_factory=dict)

    # --- Lookup helpers ---------------------------------------------------

    def get_database_description(self, database: str) -> str:
        """Return the free-text description for a database, or empty string."""
        db_desc = self.descriptions.get(database)
        return db_desc.description if db_desc else ""

    def get_table_description(self, database: str, table: str) -> str:
        """Return the free-text description for a table, or empty string."""
        db_desc = self.descriptions.get(database)
        if db_desc is None:
            return ""
        tbl_desc = db_desc.tables.get(table)
        return tbl_desc.description if tbl_desc else ""

    def get_column_descriptions(self, database: str, table: str) -> dict[str, str]:
        """Return ``{column_name: description}`` for a table, or empty dict."""
        db_desc = self.descriptions.get(database)
        if db_desc is None:
            return {}
        tbl_desc = db_desc.tables.get(table)
        return tbl_desc.columns if tbl_desc else {}


def _parse_descriptions(raw: dict[str, Any] | None) -> dict[str, DatabaseDescription]:
    """Parse the ``descriptions`` section of the YAML config."""
    result: dict[str, DatabaseDescription] = {}
    for db_name, db_data in (raw or {}).items():
        if not isinstance(db_data, dict):
            continue
        db_desc = DatabaseDescription(description=db_data.get("_description", ""))
        for key, value in db_data.items():
            if key.startswith("_"):
                continue
            if isinstance(value, dict):
                columns_raw = value.get("columns", {})
                columns: dict[str, str] = {}
                if isinstance(columns_raw, dict):
                    for col_name, col_desc in columns_raw.items():
                        columns[str(col_name)] = str(col_desc)

                tbl = TableDescription(
                    description=str(value.get("_description", "")),
                    columns=columns,
                )
                db_desc.tables[key] = tbl
        result[db_name] = db_desc
    return result


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and return the application configuration from a YAML file.

    Resolution order for the config path:
    1. Explicit *path* argument
    2. ``JURASSIC_MCP_CONFIG`` environment variable
    3. ``./config.yml`` (cwd)
    """
    if path is None:
        path = os.environ.get(ENV_CONFIG_PATH, DEFAULT_CONFIG_PATH)
    path = Path(path)

    if not path.exists():
        # Return defaults when no config file is present
        return AppConfig()

    with open(path, encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    ifx_raw_any = raw.get("informix", {})
    ifx_raw = ifx_raw_any if isinstance(ifx_raw_any, dict) else {}

    driver_options_raw = ifx_raw.get("driver_options", {})
    driver_options: dict[str, str] = {}
    if isinstance(driver_options_raw, dict):
        for key, value in driver_options_raw.items():
            driver_options[str(key)] = str(value)

    informix = InformixConfig(
        host=str(ifx_raw.get("host", "localhost")),
        port=int(ifx_raw.get("port", 9088)),
        protocol=str(ifx_raw.get("protocol", "onsoctcp")),
        server=str(ifx_raw.get("server", "informix")),
        user=str(ifx_raw.get("user", "informix")),
        password=str(ifx_raw.get("password", "in4mix")),
        db_locale=str(ifx_raw.get("db_locale", "en_us.utf8")),
        client_locale=str(ifx_raw.get("client_locale", "en_us.utf8")),
        driver_options=driver_options,
    )

    descriptions = _parse_descriptions(raw.get("descriptions"))

    return AppConfig(informix=informix, descriptions=descriptions)
