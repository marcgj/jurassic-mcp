# jurassic-mcp

FastMCP-based MCP server for IBM Informix, designed for legacy environments where table and column names are often not self-explanatory.

Includes tools to:
- List databases
- List tables by database
- Describe table structure (columns and types)
- List indexes
- List foreign keys
- Validate SQL without executing it (`prepare`)

## Requirements

- Python 3.11
- `uv` installed
- Access to an Informix instance

This project is optimized for legacy Informix TCP/SQLI using `IfxPy` by default.

## Configuration

1. Copy the example file:

```bash
cp config.example.yml config.yml
```

2. Edit `config.yml` with your host/port/credentials.

For legacy remote Informix over TCP, set:

```yaml
informix:
  protocol: "onsoctcp"
```

The server resolves the config path from:
- `JURASSIC_MCP_CONFIG` (if set)
- `./config.yml` (default)

### Free-form driver options

You can pass extra driver variables under `informix.driver_options` (for example date formats):

```yaml
driver_options:
  GL_DATE: "%d/%m/%Y"
  GL_DATETIME: "%Y-%m-%d %H:%M:%S"
```

These options are applied both to the connection string and to process environment variables, covering Informix setups that read values from environment variables.

## Run locally

```bash
uv sync
JURASSIC_MCP_CONFIG=./config.yml uv run jurassic-mcp
```

## Transport modes

The server supports two transport modes controlled by the `JURASSIC_MCP_TRANSPORT` env var:

| Mode | Value | Use case |
|------|-------|----------|
| STDIO (default) | `stdio` | Local use — MCP client spawns the process |
| HTTP | `streamable-http` | Remote/Docker — server listens on a port |
| SSE (legacy) | `sse` | Legacy clients that only support SSE |

Additional env vars for HTTP/SSE modes:

| Variable | Default | Description |
|----------|---------|-------------|
| `JURASSIC_MCP_HOST` | `0.0.0.0` | Bind address |
| `JURASSIC_MCP_PORT` | `8000` | Listen port |

The HTTP endpoint is `http://<host>:<port>/mcp` and the SSE endpoint is `http://<host>:<port>/sse`.

## Docker

The Docker image defaults to `streamable-http` on port `8000`.

### Option A — Pull from the registry (recommended, no compilation needed)

```bash
docker pull ghcr.io/marcgj/jurassic-mcp:latest
```

**HTTP mode** (recommended for remote access):

```bash
docker run --rm -p 8000:8000 \
  -e JURASSIC_MCP_CONFIG=/app/config.yml \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  ghcr.io/marcgj/jurassic-mcp:latest
```

**STDIO mode** (for local MCP client spawning):

```bash
docker run --rm -i \
  -e JURASSIC_MCP_TRANSPORT=stdio \
  -e JURASSIC_MCP_CONFIG=/app/config.yml \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  ghcr.io/marcgj/jurassic-mcp:latest
```

Available tags: `latest` (main branch), `vX.Y.Z` (releases), and the SHA of each commit.

### Option B — Load from tarball (GitHub Releases)

1. Download the `.tar` file from the [releases page](https://github.com/marcgj/jurassic-mcp/releases)
2. Load and run:

```bash
docker load -i jurassic-mcp-*.tar
docker run --rm -p 8000:8000 \
  -e JURASSIC_MCP_CONFIG=/app/config.yml \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  ghcr.io/marcgj/jurassic-mcp:<sha>
```

### Build locally

```bash
docker build -t jurassic-mcp .
docker run --rm -p 8000:8000 \
  -e JURASSIC_MCP_CONFIG=/app/config.yml \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  jurassic-mcp
```

## Informix test environment (docker compose)

`docker-compose.yml` starts:
- `informix`: Informix Developer instance with the demo schema
- `mcp-server`: local image of this MCP server

Start:

```bash
docker compose up --build
```

The script `init-db/init.sql` creates the `stores_demo` database and demo tables (`customers`, `orders`, `order_items`) with indexes and foreign keys.

`config.test.yml` is intended for this compose setup (host `informix`) and is git-ignored.

## CI

Workflow in [.github/workflows/build.yml](.github/workflows/build.yml):
- runs on every commit to `main` and on `v*` tags
- builds and pushes the Docker image to `ghcr.io/marcgj/jurassic-mcp`
- exports a tarball and uploads it as a GitHub Actions artifact
- on `v*` tags: creates a GitHub Release with the tarball attached

## MCP client configuration

### HTTP mode (recommended for Docker / remote server)

If the server is already running (e.g. via Docker with `-p 8000:8000`), point your client directly at the HTTP endpoint. No process spawning needed.

**Claude Code** — add to `.claude/mcp.json` or `~/.claude.json`:

```json
{
  "mcpServers": {
    "jurassic-mcp": {
      "type": "http",
      "url": "http://<host>:8000/mcp"
    }
  }
}
```

**VS Code Copilot** — add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "jurassic-mcp": {
      "type": "http",
      "url": "http://<host>:8000/mcp"
    }
  }
}
```

Replace `<host>` with `localhost` if running locally, or the remote machine's IP/hostname.

---

### STDIO mode (local, client spawns the process)

**Claude Code** — via Docker:

```json
{
  "mcpServers": {
    "jurassic-mcp": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "JURASSIC_MCP_TRANSPORT=stdio",
        "-e", "JURASSIC_MCP_CONFIG=/app/config.yml",
        "-v", "/absolute/path/to/config.yml:/app/config.yml:ro",
        "ghcr.io/marcgj/jurassic-mcp:latest"
      ]
    }
  }
}
```

**Claude Code** — via `uv` (no Docker):

```json
{
  "mcpServers": {
    "jurassic-mcp": {
      "command": "uv",
      "args": ["run", "jurassic-mcp"],
      "env": {
        "JURASSIC_MCP_CONFIG": "/absolute/path/to/config.yml"
      }
    }
  }
}
```

**VS Code Copilot** — via Docker:

```json
{
  "servers": {
    "jurassic-mcp": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "JURASSIC_MCP_TRANSPORT=stdio",
        "-e", "JURASSIC_MCP_CONFIG=/app/config.yml",
        "-v", "${workspaceFolder}/config.yml:/app/config.yml:ro",
        "ghcr.io/marcgj/jurassic-mcp:latest"
      ]
    }
  }
}
```

**VS Code Copilot** — via `uv` (no Docker):

```json
{
  "servers": {
    "jurassic-mcp": {
      "command": "uv",
      "args": ["run", "jurassic-mcp"],
      "env": {
        "JURASSIC_MCP_CONFIG": "${workspaceFolder}/config.yml"
      }
    }
  }
}
```
