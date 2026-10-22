# jurassic-mcp

FastMCP-based MCP server for IBM Informix, designed for legacy environments where table and column names are often not self-explanatory.

Schema tools (all read-only):

| Tool | What it does |
|------|--------------|
| `list_databases` | Databases on the instance |
| `list_tables` | Tables, views and synonyms (optional `name_filter`, `owner`) |
| `describe_table` | Columns with decoded types (`DECIMAL(12,2)`, `DATETIME YEAR TO SECOND`…), nullability and config descriptions |
| `list_indexes` | Indexes with columns, uniqueness and backing constraints |
| `list_foreign_keys` | Outgoing foreign keys and incoming references (`referenced_by`) |
| `search_schema` | Case-insensitive search over table/column names **and** config descriptions |
| `validate_sql` | Checks a `SELECT` with the server's `PREPARE` (syntax, tables, columns, permissions). **Never executes it.** Rejects anything that is not a single `SELECT`/`WITH`, and any `INTO` clause (`INTO TEMP`, `INTO EXTERNAL`…) |

Table arguments accept `table`, `owner.table`, or a separate `owner` parameter; local synonyms are followed.

Discovery tools (used by the `autodiscover` prompt):

| Tool | What it does |
|------|--------------|
| `discovery_overview` | Ranks tables by real row count (live from `sysmaster` partition headers, falls back to `UPDATE STATISTICS` values) with FK relationship count and documentation status (`undocumented` / `partial` / `documented`) |
| `save_descriptions` | Saves confirmed table/column/database descriptions to the descriptions file (validates that table and columns exist). The only tool that writes, and it never writes to the database |

## Autodiscover: document a legacy database interactively

The server exposes an MCP prompt, `autodiscover`, that turns the client into a guided documentation session:

1. It ranks the tables by row count (biggest = usually the core business entities) and lets you exclude framework/temp tables (e.g. `s_%`).
2. For each table it reads columns, indexes and foreign keys, proposes a table description and column descriptions with a confidence level, and asks you only about the uncertain ones.
3. What you confirm is saved with `save_descriptions` and is used from then on by `describe_table`, `list_tables` and `search_schema` (so searching "albarán" finds `albcab`).

Invoke it from the client's `/` menu:
- **Claude Code**: `/mcp__jurassic-mcp__autodiscover <database> [max_tables] [exclude]`, e.g. `/mcp__jurassic-mcp__autodiscover agromodol 15 s_%,tmp%`
- **VS Code Copilot**: `/mcp.jurassic-mcp.autodiscover`

It only reads catalog metadata, never table data. You can stop at any time and run it again later: `only_undocumented` makes it continue where you left off.

Descriptions are stored in `descriptions.yml` next to the config file (override with `descriptions_file:` in the config or `JURASSIC_MCP_DESCRIPTIONS`). It uses the same format as the `descriptions` section of `config.yml`, overrides it, and can be edited by hand. It is git-ignored because it describes real databases. In docker compose it lives in `./data/`.

## Requirements

- Linux x86_64 (the OneDB ODBC driver used by IfxPy is only available there)
- `uv`, `gcc`, `curl`
- Network access to an Informix instance (SQLI / `onsoctcp` listener, usually port 9088)

## Quick start

```bash
./scripts/setup.sh                 # once: downloads the driver to vendor/, builds IfxPy, runs uv sync
cp config.example.yml config.yml   # once: edit host/port/server/credentials
uv run jurassic-mcp --http         # start a local server on http://127.0.0.1:8000/mcp
```

Or in VS Code press **F5** and pick **jurassic-mcp: HTTP (config.yml)** ([.vscode/launch.json](.vscode/launch.json)).

On start it checks the Informix connection and prints everything needed to connect a client:

```
 Informix      user@host:9088 (server=...)  OK, 78 databases (agromodol, ...)
 Config        /path/to/config.yml
 Descriptions  /path/to/descriptions.yml
 Endpoint      http://127.0.0.1:8000/mcp

 Claude Code (run once, in any terminal):
   claude mcp add --transport http jurassic-mcp http://127.0.0.1:8000/mcp

 VS Code (.vscode/mcp.json):
   {"servers": {"jurassic-mcp": {"type": "http", "url": "http://127.0.0.1:8000/mcp"}}}
```

Inside this repo you don't even need that: [.mcp.json](.mcp.json) (Claude Code) and [.vscode/mcp.json](.vscode/mcp.json) (VS Code) already point at `http://127.0.0.1:8000/mcp`.

Options: `--http`, `--host`, `--port`, `--config`, `--no-check` (skip the connection check), `-v/--verbose` (log driver/config details, every MCP request and tool call with timing and result summary, every connection and every SQL statement; the VS Code launch configs enable it). Without `--http` it speaks STDIO, for clients that launch the process themselves (see [MCP client configuration](#mcp-client-configuration)); run by hand in a terminal, it prints the matching `claude mcp add` / VS Code snippet.

## Run locally

```bash
JURASSIC_MCP_CONFIG=./config.yml uv run jurassic-mcp          # STDIO
uv run jurassic-mcp --http --config ./config.yml --port 8000  # HTTP
python -m jurassic_mcp --http                                 # same, from the venv
```

Why the script: IfxPy compiles from source and needs the HCL OneDB ODBC driver (`libthcli.so`). Its own `setup.py` does not install the driver into the venv, and modern gcc (14+) fails without `-Wno-incompatible-pointer-types`. `setup.sh` handles both and is safe to re-run.

At startup the server looks for the driver in this order and re-execs itself with the right `LD_LIBRARY_PATH`:
`$INFORMIXDIR` → `$JURASSIC_MCP_DRIVER_DIR` → `site-packages/onedb-odbc-driver` → `vendor/onedb-odbc-driver` → uv cache (last resort, with a warning). If none is found it exits with an error explaining what to do.

All logs go to stderr; stdout carries only MCP JSON-RPC.

## Configuration

See [config.example.yml](config.example.yml) — every field is documented there. Highlights:

- **Config path**: `JURASSIC_MCP_CONFIG`, else `./config.yml`. If `JURASSIC_MCP_CONFIG` points to a missing file (or a directory) the server refuses to start. If it is unset and `./config.yml` is missing it warns on stderr and uses built-in defaults.
- **`server`** must match the instance's `DBSERVERNAME`/`DBSERVERALIASES` for that port (`onstat -g ntt`). Port 9089 is usually DRDA, not `onsoctcp`.
- **`connect_timeout`** (default 10 s) sets `INFORMIXCONTIME`; `INFORMIXCONRETRY` defaults to 0, so an unreachable server fails fast instead of hanging the MCP client.
- **`db_locale` / `client_locale`** are empty by default and only sent when set. If you get error -23197, set `db_locale` to the database's locale:
  `SELECT dbs_collate FROM sysmaster:sysdbslocale WHERE dbs_dbsname = 'mydb'`
- **`driver_options`** are exported as environment variables (e.g. `GL_DATE`, `DBDATE`). Values in `config.yml` override variables already present in the environment.
- Env overrides: `INFORMIX_HOST`, `INFORMIX_PORT`, `INFORMIX_SERVER`, `INFORMIX_USER`, `INFORMIX_PASSWORD`.

### Descriptions

The `descriptions` section annotates databases, tables and columns so AI clients understand cryptic legacy names. Keys are case-insensitive. Quote names YAML would read as booleans (`"on"`, `"yes"`, `"no"`).

```yaml
descriptions:
  stores_demo:
    _description: "Demo sales database"
    artmae:
      _description: "Article master"
      columns:
        am_cod: "Article code"
        am_pvp: "Retail price"
```

Connection errors include the SQLCODE and a hint (wrong server name/port, authentication, missing database, locale).

## Transport modes

| Mode | `JURASSIC_MCP_TRANSPORT` | Use case |
|------|-------|----------|
| STDIO (default) | `stdio` | Local use — MCP client spawns the process |
| HTTP | `streamable-http` (or `http`) | Remote/Docker — server listens on a port |
| SSE (legacy) | `sse` | Legacy clients that only support SSE |

| Variable | Default | Description |
|----------|---------|-------------|
| `JURASSIC_MCP_HOST` | `127.0.0.1` (`0.0.0.0` in the Docker image) | Bind address |
| `JURASSIC_MCP_PORT` | `8000` | Listen port |

The HTTP endpoint is `http://<host>:<port>/mcp`, the SSE endpoint `http://<host>:<port>/sse`. There is no authentication: do not expose the port publicly.

## Docker

No prebuilt image is published: the image contains the HCL OneDB ODBC driver, whose license does not allow redistribution. Build it yourself — the build downloads the driver directly from HCL into `/opt/onedb-odbc-driver`. The image defaults to `streamable-http` on port 8000.

```bash
docker build -t jurassic-mcp .

# HTTP
docker run --rm -p 127.0.0.1:8000:8000 \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  jurassic-mcp

# STDIO
docker run --rm -i -e JURASSIC_MCP_TRANSPORT=stdio \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  jurassic-mcp
```

Do not push this image to a public registry.

## Informix test environment (docker compose)

```bash
docker compose up --build
```

Services:
- `informix`: Informix Developer Edition (pinned tag), with a healthcheck; ports bound to 127.0.0.1.
- `informix-init`: one-shot job that waits for Informix and loads [init-db/init.sql](init-db/init.sql) into `stores_demo` (demo tables with PKs/FKs, including cryptic legacy-style names like `artmae`, `tabgen`). Skipped if the database exists; `FORCE_REINIT=1 docker compose up` recreates it.
- `mcp-server`: this server over HTTP at `http://localhost:8000/mcp`, using [config.test.yml](config.test.yml).

`docker compose down -v` wipes the Informix data volume. The first start of Informix takes a few minutes.

## CI

[.github/workflows/build.yml](.github/workflows/build.yml) checks that no driver files are committed, validates `docker-compose.yml`, builds the image and smoke-tests that IfxPy imports inside it. It never publishes or stores the image (no registry push, artifacts, releases or layer cache), because the image contains the non-redistributable driver.

## Driver license

The HCL OneDB ODBC driver is downloaded from HCL by `scripts/setup.sh` / `docker build` and is never committed (`vendor/` is in `.gitignore` and `.dockerignore`). Its license (see `vendor/onedb-odbc-driver/license/`) permits internal use only and forbids redistribution, so do not commit it (Git LFS included), attach it to releases, or publish images that contain it.

## MCP client configuration

### HTTP (server already running, e.g. Docker)

**Claude Code** (`.mcp.json` or `~/.claude.json`):

```json
{
  "mcpServers": {
    "jurassic-mcp": { "type": "http", "url": "http://localhost:8000/mcp" }
  }
}
```

**VS Code Copilot** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "jurassic-mcp": { "type": "http", "url": "http://localhost:8000/mcp" }
  }
}
```

### STDIO via `uv` (no Docker)

Run `./scripts/setup.sh` once first. `--directory` is required: MCP clients do not start in the project folder.

```json
{
  "mcpServers": {
    "jurassic-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/jurassic-mcp", "jurassic-mcp"],
      "env": { "JURASSIC_MCP_CONFIG": "/absolute/path/to/config.yml" }
    }
  }
}
```

(VS Code: same, under `"servers"`.)

### STDIO via Docker

```json
{
  "mcpServers": {
    "jurassic-mcp": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "JURASSIC_MCP_TRANSPORT=stdio",
        "-v", "/absolute/path/to/config.yml:/app/config.yml:ro",
        "jurassic-mcp"
      ]
    }
  }
}
```

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `libthcli.so: cannot open shared object file` / "OneDB ODBC driver not found" | Run `./scripts/setup.sh`, or set `INFORMIXDIR` to an existing `onedb-odbc-driver` |
| `uv sync` fails compiling `ifxpyc.c` (incompatible pointer types) | Use `./scripts/setup.sh` (sets the needed `CFLAGS`) |
| SQLCODE -908 / -930 / -25596 | Host unreachable, or `server` doesn't match the instance's name for that port, or using DRDA port 9089 with `onsoctcp` |
| SQLCODE -951 / -952 | Wrong user/password |
| SQLCODE -329 | Database doesn't exist or no permission |
| SQLCODE -23197 | Locale mismatch: set `db_locale` (see Configuration) |
| Client says the server failed to start | Check its stderr log; the server prints the config path, transport and any startup error there |
