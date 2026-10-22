# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`jurassic-mcp` is a [FastMCP](https://github.com/jlowin/fastmcp) 3.x MCP server for IBM Informix, designed for legacy environments where table and column names are often not self-explanatory. It exposes read-only schema exploration, schema search and SQL validation tools over STDIO (default) or streamable HTTP (Docker default).

## Commands

```bash
# First-time setup: downloads the OneDB ODBC driver to vendor/, builds IfxPy, uv sync
./scripts/setup.sh

# Run the server locally (STDIO)
JURASSIC_MCP_CONFIG=./config.yml uv run jurassic-mcp

# Docker (image default transport is streamable-http on :8000)
docker build -t jurassic-mcp .
docker run --rm -i -e JURASSIC_MCP_TRANSPORT=stdio -v $(pwd)/config.yml:/app/config.yml:ro jurassic-mcp

# Full test environment (Informix + init job + MCP server over HTTP)
docker compose up --build
FORCE_REINIT=1 docker compose up   # recreate stores_demo
```

There are no automated tests. `config.yml` holds real credentials and is git-ignored; use `config.test.yml` (compose) or `config.example.yml` for experiments.

## Driver setup (the usual source of startup problems)

IfxPy is the only driver (`ibm_db` was removed). It compiles from source and needs the HCL OneDB ODBC driver (`libthcli.so`):
- IfxPy's `setup.py` does not copy the driver into the venv, so `scripts/setup.sh` puts it in `vendor/onedb-odbc-driver` and builds with `INFORMIXDIR`/`CSDK_HOME` pointing there.
- gcc 14+ needs `CFLAGS=-Wno-incompatible-pointer-types` to build IfxPy (setup.sh and the Dockerfile set it).
- The build needs `setuptools<58` (`use_2to3`), enforced via `build-constraint-dependencies` in `pyproject.toml`.
- **The driver's license forbids redistribution**: never commit `vendor/` (not even via Git LFS), attach it to releases, or make CI publish/cache an image containing it. CI only builds and smoke-tests.
- `__init__.py` finds the driver (`$INFORMIXDIR` → `$JURASSIC_MCP_DRIVER_DIR` → site-packages → `vendor/` → uv cache) and re-execs with `LD_LIBRARY_PATH`/`INFORMIXDIR` set. `JURASSIC_MCP_SKIP_REEXEC=1` disables this. The Docker image sets those variables directly.

## Architecture

Package under `src/jurassic_mcp/`:

- **`__init__.py`** — Entry point `main()`: driver discovery + re-exec, transport/host/port validation (HTTP host defaults to 127.0.0.1), config loading with clear stderr errors, `init_server`, `mcp.run()`. Never writes to stdout.
- **`config.py`** — `load_config()`: `JURASSIC_MCP_CONFIG` → `./config.yml` (missing explicit path or a directory raises; missing default warns and uses defaults). Dataclasses `AppConfig`, `InformixConfig` (incl. `connect_timeout`, `port` int or service name, locales empty by default), `DatabaseDescription`, `TableDescription`. Description keys are str-coerced and lower-cased; `get_*_description` helpers are case-insensitive.
- **`db.py`** — `connect(cfg, database)` context manager (IfxPy only). Process-wide env setup once under a lock (per-process `sqlhosts` via mkstemp unless `INFORMIXSQLHOSTS` is set; `INFORMIXCONTIME`/`INFORMIXCONRETRY` defaults; `driver_options` override env). Brace-quotes UID/PWD/DATABASE when needed. Retries without locales only on locale SQLCODEs. Raises a single `ConnectionError` with SQLCODE + hint.
- **`catalog.py`** — System catalog queries. `resolve_table` (case-insensitive, optional owner, follows local synonyms), `list_tables` (T/V/P/S/E), `describe_table`, `list_indexes`, `list_foreign_keys` (+ `referenced_by`), `search_schema`, `validate_sql`. `COLTYPE_MAP` + `format_column_type` decode `coltype` (`& 256` = NOT NULL, `& 0xFF` base type, 40/41 opaque via `sysxtdtypes`) and `collength` (DECIMAL p/s, VARCHAR max/min, DATETIME/INTERVAL qualifiers). Note: system-generated index names start with a space — never strip `idxname` before using it in a query.
- **`server.py`** — FastMCP tools. Each call opens a fresh connection, delegates to `catalog.py`, attaches config descriptions (lower-cased lookups) and returns a dict (structured output). Errors are raised as `ToolError`.

## Discovery (part 1) and roadmap (part 2)

- `discovery_overview` ranks tables by live row count (`sysmaster:sysptnhdr`, falling back to `systables.nrows`) with documentation status. `save_descriptions` validates names against the catalog and writes via `config.save_descriptions()`. It writes only to the descriptions file (`descriptions.yml` next to the config, `descriptions_file:` or `JURASSIC_MCP_DESCRIPTIONS`), atomically and under a lock, and updates the in-memory config. It never writes to `config.yml` or the database.
- The `autodiscover` MCP prompt (`/mcp__jurassic-mcp__autodiscover <db> [max_tables] [exclude]` in Claude Code) drives the interactive documentation loop. It is metadata-only.
- Part 2 (data queries + making clients call the tools reliably) is planned in [docs/roadmap-part2.md](docs/roadmap-part2.md).

## validate_sql safety

`validate_sql` must never execute. `check_read_only_sql` masks literals/comments (`--`, `/* */`, `{ }`), allows one `SELECT`/`WITH` statement and rejects any `INTO`. Then only `ifx.prepare` + describe; the statement is always freed. Do not add `execute` or `SET EXPLAIN` (writes files on the server).

## Configuration

Copy `config.example.yml` to `config.yml` (every field is documented there). `descriptions` uses `_description` for the entity and `columns:` for column annotations; these enrich tool output and `search_schema`.
