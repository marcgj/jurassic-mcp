"""jurassic-mcp package entry point.

Nothing in this module may write to stdout: with the STDIO transport stdout
is the JSON-RPC channel. All diagnostics go to stderr.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

_DRIVER_NAME = "onedb-odbc-driver"
_LIBTHCLI = Path("lib") / "cli" / "libthcli.so"
_VALID_TRANSPORTS = ("stdio", "streamable-http", "http", "sse")
_DEFAULT_HTTP_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = 8000


def _log(message: str) -> None:
    print(f"[jurassic-mcp] {message}", file=sys.stderr, flush=True)


def _fail(message: str) -> None:
    print(f"[jurassic-mcp] ERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def _is_driver_dir(path: Path) -> bool:
    return (path / _LIBTHCLI).is_file()


def _uv_cache_driver_dir() -> Path | None:
    """Last resort: the driver IfxPy's setup.py extracted inside the uv sdist cache."""
    if os.environ.get("UV_CACHE_DIR"):
        cache_root = Path(os.environ["UV_CACHE_DIR"])
    elif os.environ.get("XDG_CACHE_HOME"):
        cache_root = Path(os.environ["XDG_CACHE_HOME"]) / "uv"
    else:
        cache_root = Path.home() / ".cache" / "uv"
    if not cache_root.is_dir():
        return None

    # setup.py builds into build/lib.linux-<platform.processor()>-<X.Y>/ and
    # platform.processor() is often '' on Linux, hence the '*' in the middle.
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    pattern = f"sdists-v*/pypi/ifxpy/*/*/src/build/lib.linux-*-{pyver}/{_DRIVER_NAME}"
    candidates = [p for p in cache_root.glob(pattern) if _is_driver_dir(p)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p / _LIBTHCLI).stat().st_mtime)


def _find_driver_dir() -> Path | None:
    """Locate the OneDB ODBC driver root (the dir that contains lib/cli/libthcli.so)."""
    for var in ("INFORMIXDIR", "JURASSIC_MCP_DRIVER_DIR"):
        value = os.environ.get(var)
        if not value:
            continue
        path = Path(value).expanduser()
        if _is_driver_dir(path):
            return path.resolve()
        _log(f"WARNING: {var}={value} does not contain {_LIBTHCLI}; ignoring it.")

    site_dirs = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
    for site_dir in site_dirs:
        path = Path(site_dir) / _DRIVER_NAME
        if _is_driver_dir(path):
            return path.resolve()

    # <project root>/vendor/onedb-odbc-driver (populated by scripts/setup.sh).
    project_root = Path(__file__).resolve().parents[2]
    path = project_root / "vendor" / _DRIVER_NAME
    if _is_driver_dir(path):
        return path

    path = _uv_cache_driver_dir()
    if path is not None:
        _log(
            f"WARNING: using the OneDB ODBC driver from the uv cache ({path}). "
            "This breaks if the cache is cleaned; run scripts/setup.sh or set INFORMIXDIR."
        )
        return path.resolve()
    return None


def _ifxpy_importable() -> bool:
    try:
        import IfxPy  # noqa: F401
    except ImportError:
        return False
    return True


def _maybe_reexec_with_ifxpy_runtime() -> None:
    """Re-exec the interpreter with LD_LIBRARY_PATH pointing at the driver.

    The dynamic loader reads LD_LIBRARY_PATH only at process start, so setting
    it from Python is not enough: the process has to be replaced.
    """
    if os.environ.get("JURASSIC_MCP_SKIP_REEXEC") == "1":
        return

    driver_dir = _find_driver_dir()
    if driver_dir is None:
        if _ifxpy_importable():
            # Driver libs are resolvable some other way (ldconfig, system CSDK).
            return
        _fail(
            "Informix driver not found: IfxPy cannot load libthcli.so.\n"
            "  Fix one of:\n"
            "    - run scripts/setup.sh (downloads the OneDB ODBC driver into vendor/ and runs uv sync)\n"
            "    - set INFORMIXDIR (or JURASSIC_MCP_DRIVER_DIR) to a OneDB ODBC driver / Informix CSDK\n"
            f"      directory that contains {_LIBTHCLI}"
        )

    lib_dirs = [driver_dir / "lib", driver_dir / "lib" / "cli", driver_dir / "lib" / "esql"]
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    current_parts = [p for p in current_ld.split(os.pathsep) if p]
    current_resolved = {str(Path(p).resolve()) for p in current_parts}
    missing = [str(d) for d in lib_dirs if str(d.resolve()) not in current_resolved]

    if not missing:
        os.environ.setdefault("INFORMIXDIR", str(driver_dir))
        return

    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = os.pathsep.join([*missing, *current_parts])
    env["INFORMIXDIR"] = str(driver_dir)
    env["JURASSIC_MCP_SKIP_REEXEC"] = "1"
    os.execvpe(sys.executable, [sys.executable, *sys.argv], env)


def main() -> None:
    """Load configuration and run the MCP server."""
    _maybe_reexec_with_ifxpy_runtime()

    transport = os.environ.get("JURASSIC_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in _VALID_TRANSPORTS:
        _fail(
            f"Invalid JURASSIC_MCP_TRANSPORT={transport!r}; "
            f"expected one of: {', '.join(_VALID_TRANSPORTS)}"
        )

    from .config import load_config
    from .server import init_server, mcp

    config_source = os.environ.get("JURASSIC_MCP_CONFIG") or "./config.yml (default)"
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 - any config problem is fatal at startup
        _fail(f"Could not load configuration from {config_source}: {exc}")
    init_server(config)

    if transport == "stdio":
        _log(f"Starting (transport=stdio, config={config_source})")
        mcp.run(transport="stdio")
        return

    host = os.environ.get("JURASSIC_MCP_HOST") or _DEFAULT_HTTP_HOST
    port_raw = os.environ.get("JURASSIC_MCP_PORT") or str(_DEFAULT_HTTP_PORT)
    try:
        port = int(port_raw)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        _fail(f"Invalid JURASSIC_MCP_PORT={port_raw!r}; expected an integer 1-65535")
    _log(f"Starting (transport={transport}, host={host}, port={port}, config={config_source})")
    mcp.run(transport=transport, host=host, port=port)
