#!/usr/bin/env bash
# Set up a local development environment for jurassic-mcp.
#
# 1. Downloads the HCL OneDB ODBC driver (the same tarball IfxPy's setup.py
#    would fetch) into ./vendor/onedb-odbc-driver, unless already present.
# 2. Builds IfxPy against it (INFORMIXDIR/CSDK_HOME) and installs all deps
#    with `uv sync`.
# 3. Verifies the driver's libthcli.so is in place.
#
# Idempotent: re-running skips the download if the driver is already there.
# Override the driver location with ONEDB_DRIVER_DIR=/path/to/onedb-odbc-driver.
set -euo pipefail

DRIVER_URL="${ONEDB_DRIVER_URL:-https://hcl-onedb.github.io/odbc/OneDB-Linux64-ODBC-Driver.tar}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRIVER_DIR="${ONEDB_DRIVER_DIR:-$PROJECT_ROOT/vendor/onedb-odbc-driver}"
LIBTHCLI="$DRIVER_DIR/lib/cli/libthcli.so"

log() { printf '[setup] %s\n' "$*" >&2; }
die() { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

command -v uv >/dev/null 2>&1 || die "uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/"
[[ "$(uname -s)" == "Linux" && "$(uname -m)" == "x86_64" ]] \
    || die "Only Linux x86_64 is supported by the OneDB ODBC driver (got $(uname -s) $(uname -m))."

if [[ -f "$LIBTHCLI" ]]; then
    log "OneDB ODBC driver already present at $DRIVER_DIR"
else
    command -v curl >/dev/null 2>&1 || die "curl is required to download the driver."
    log "Downloading OneDB ODBC driver from $DRIVER_URL"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fSL --retry 3 -o "$tmp/driver.tar" "$DRIVER_URL" || die "Download failed: $DRIVER_URL"
    mkdir -p "$tmp/extract"
    tar -xf "$tmp/driver.tar" -C "$tmp/extract" || die "Could not extract $tmp/driver.tar"
    [[ -f "$tmp/extract/onedb-odbc-driver/lib/cli/libthcli.so" ]] \
        || die "Unexpected tarball layout: onedb-odbc-driver/lib/cli/libthcli.so not found."
    mkdir -p "$(dirname "$DRIVER_DIR")"
    rm -rf "$DRIVER_DIR"
    mv "$tmp/extract/onedb-odbc-driver" "$DRIVER_DIR"
    log "Driver installed at $DRIVER_DIR"
fi

export INFORMIXDIR="$DRIVER_DIR"
export CSDK_HOME="$DRIVER_DIR"
export LD_LIBRARY_PATH="$DRIVER_DIR/lib:$DRIVER_DIR/lib/cli:$DRIVER_DIR/lib/esql${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# IfxPy's C extension does not compile cleanly with gcc >= 14 without these.
export CFLAGS="${CFLAGS:-} -w -Wno-incompatible-pointer-types"

log "Running uv sync (building IfxPy against $DRIVER_DIR)"
cd "$PROJECT_ROOT"
uv sync "$@"

[[ -f "$LIBTHCLI" ]] || die "Driver library missing after setup: $LIBTHCLI"

if JURASSIC_MCP_SKIP_REEXEC=1 uv run --no-sync python -c "import IfxPy" >/dev/null 2>&1; then
    log "IfxPy imports correctly."
else
    die "IfxPy failed to import. Try: rm -rf .venv && uv cache clean ifxpy && scripts/setup.sh"
fi

log "Done. Run the server with:"
log "  JURASSIC_MCP_CONFIG=./config.yml uv run jurassic-mcp"
log "(the driver is found automatically in vendor/; or set INFORMIXDIR=$DRIVER_DIR)"
