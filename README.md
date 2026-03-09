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

## Docker

### Option A — Pull from the registry (recommended, no compilation needed)

```bash
docker pull ghcr.io/marcgj/jurassic-mcp:latest
docker run --rm -i \
  -e JURASSIC_MCP_CONFIG=/app/config.yml \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  ghcr.io/marcgj/jurassic-mcp:latest
```

Available tags: `latest` (main branch), `vX.Y.Z` (releases), and the SHA of each commit.

### Option B — Load from tarball (GitHub Releases)

1. Download the `.tar` file from the [releases page](https://github.com/marcgj/jurassic-mcp/releases)
2. Load the image:

```bash
docker load -i jurassic-mcp-*.tar
docker run --rm -i \
  -e JURASSIC_MCP_CONFIG=/app/config.yml \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  ghcr.io/marcgj/jurassic-mcp:<sha>
```

### Build locally

```bash
docker build -t jurassic-mcp .
docker run --rm -i \
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

## MCP integration in VS Code/Copilot

Configure your MCP client to run the `jurassic-mcp` command in this project, making sure `JURASSIC_MCP_CONFIG` points to your `config.yml`.
