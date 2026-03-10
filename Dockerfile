FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gcc \
    g++ \
    make \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml .python-version ./
COPY src ./src

RUN CFLAGS="-w -Wno-error -Wno-incompatible-pointer-types" uv sync --no-dev && \
    find /root/.cache -path "*/ifxpy/*/onedb-odbc-driver" -type d | head -1 | \
    xargs -I{} cp -r {} /app/.venv/lib/python3.11/site-packages/onedb-odbc-driver

FROM python:3.11-slim

# Runtime libraries required by the bundled onedb-odbc-driver (IfxPy)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libncurses6 \
    libaio1t64 \
    && rm -rf /var/lib/apt/lists/*

# IfxPy bundles onedb-odbc-driver under site-packages (Linux x64)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    JURASSIC_MCP_CONFIG=/app/config.yml \
    JURASSIC_MCP_TRANSPORT=streamable-http \
    JURASSIC_MCP_HOST=0.0.0.0 \
    JURASSIC_MCP_PORT=8000

EXPOSE 8000

WORKDIR /app

COPY --from=builder /app /app

# Register onedb-odbc-driver shared libraries with ldconfig so IfxPy can
# find libthcli.so without a hardcoded LD_LIBRARY_PATH.
RUN find /app/.venv -path "*/onedb-odbc-driver/lib*" -name "*.so" -exec dirname {} \; \
    | sort -u > /etc/ld.so.conf.d/ifxpy.conf && \
    cat /etc/ld.so.conf.d/ifxpy.conf && \
    ldconfig

CMD ["jurassic-mcp"]
