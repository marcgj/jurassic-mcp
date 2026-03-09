"""jurassic-mcp package entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_ifxpy_cli_dir() -> Path | None:
    site_pkg_candidate = (
        Path(sys.prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "onedb-odbc-driver"
        / "lib"
        / "cli"
        / "libthcli.so"
    )
    if site_pkg_candidate.exists():
        return site_pkg_candidate.parent

    cache_root = Path.home() / ".cache" / "uv" / "sdists-v9" / "pypi" / "ifxpy"
    if not cache_root.exists():
        return None

    candidates = sorted(
        cache_root.glob("**/onedb-odbc-driver/lib/cli/libthcli.so"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return candidates[0].parent


def _maybe_reexec_with_ifxpy_runtime() -> None:
    if os.environ.get("JURASSIC_MCP_SKIP_REEXEC") == "1":
        return

    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    if "onedb-odbc-driver" in current_ld:
        return

    cli_dir = _find_ifxpy_cli_dir()
    if cli_dir is None:
        return

    lib_dir = cli_dir.parent
    odbc_root = lib_dir.parent
    ld_parts = [str(lib_dir), str(lib_dir / "esql"), str(lib_dir / "cli")]
    if current_ld:
        ld_parts.append(current_ld)

    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = ":".join(ld_parts)
    env.setdefault("INFORMIXDIR", str(odbc_root))
    env["JURASSIC_MCP_SKIP_REEXEC"] = "1"

    os.execvpe(sys.executable, [sys.executable, *sys.argv], env)


def main() -> None:
    """Load configuration and run the MCP server."""
    _maybe_reexec_with_ifxpy_runtime()

    from .config import load_config
    from .server import init_server, mcp

    config = load_config()
    init_server(config)

    transport = os.environ.get("JURASSIC_MCP_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        host = os.environ.get("JURASSIC_MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("JURASSIC_MCP_PORT", "8000"))
        mcp.run(transport=transport, host=host, port=port)
