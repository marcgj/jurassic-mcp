# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: download the OneDB ODBC driver and build the venv (IfxPy is
# compiled from source against the driver).
# ---------------------------------------------------------------------------
FROM python:3.11-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /bin/

ARG ONEDB_DRIVER_URL=https://hcl-onedb.github.io/odbc/OneDB-Linux64-ODBC-Driver.tar

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    INFORMIXDIR=/opt/onedb-odbc-driver \
    CSDK_HOME=/opt/onedb-odbc-driver

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gcc \
        libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Driver at a fixed path. With INFORMIXDIR/CSDK_HOME set, IfxPy's setup.py
# builds against it instead of downloading its own copy.
RUN curl -fSL --retry 3 -o /tmp/onedb-odbc-driver.tar "${ONEDB_DRIVER_URL}" \
    && tar -xf /tmp/onedb-odbc-driver.tar -C /opt \
    && rm /tmp/onedb-odbc-driver.tar \
    && test -f /opt/onedb-odbc-driver/lib/cli/libthcli.so

WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock .python-version ./
RUN CFLAGS="-w -Wno-incompatible-pointer-types" \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim-trixie

# Runtime libraries required by the OneDB ODBC driver.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libaio1t64 \
        libncurses6 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    INFORMIXDIR=/opt/onedb-odbc-driver \
    LD_LIBRARY_PATH=/opt/onedb-odbc-driver/lib:/opt/onedb-odbc-driver/lib/cli:/opt/onedb-odbc-driver/lib/esql \
    JURASSIC_MCP_CONFIG=/app/config.yml \
    JURASSIC_MCP_TRANSPORT=streamable-http \
    JURASSIC_MCP_HOST=0.0.0.0 \
    JURASSIC_MCP_PORT=8000

COPY --from=builder /opt/onedb-odbc-driver /opt/onedb-odbc-driver
COPY --from=builder /app/.venv /app/.venv

WORKDIR /app

# Fail the build if the driver or the IfxPy extension cannot be loaded.
RUN test -f /opt/onedb-odbc-driver/lib/cli/libthcli.so \
    && python -c "import IfxPy"

EXPOSE 8000

CMD ["jurassic-mcp"]
