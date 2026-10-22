"""jurassic-mcp package entry point.

Nothing in this module may write to stdout: with the STDIO transport stdout
is the JSON-RPC channel. All diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
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
    if Path(sys.argv[0]).name == "__main__.py":  # started as `python -m jurassic_mcp`
        argv = [sys.executable, "-m", "jurassic_mcp", *sys.argv[1:]]
    else:
        argv = [sys.executable, *sys.argv]
    os.execvpe(sys.executable, argv, env)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jurassic-mcp",
        description=(
            "MCP server for IBM Informix. Without options it speaks MCP over "
            "STDIO (for clients that launch it). Use --http to run it as a "
            "local server you connect Claude Code / VS Code to."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve streamable HTTP (same as JURASSIC_MCP_TRANSPORT=streamable-http)",
    )
    parser.add_argument("--host", help=f"HTTP bind address (default {_DEFAULT_HTTP_HOST})")
    parser.add_argument("--port", type=int, help=f"HTTP port (default {_DEFAULT_HTTP_PORT})")
    parser.add_argument("--config", help="config file (same as JURASSIC_MCP_CONFIG)")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log startup details, every MCP request/tool call and every SQL "
        "statement to stderr (same as JURASSIC_MCP_VERBOSE=1)",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="skip the Informix connection check at HTTP startup",
    )
    args = parser.parse_args()
    # Options win over the environment; mirror them into it so the re-exec'd
    # process (same argv) and load_config() see the same values.
    if args.http:
        os.environ["JURASSIC_MCP_TRANSPORT"] = "streamable-http"
    if args.verbose:
        os.environ["JURASSIC_MCP_VERBOSE"] = "1"
    if args.host:
        os.environ["JURASSIC_MCP_HOST"] = args.host
    if args.port is not None:
        os.environ["JURASSIC_MCP_PORT"] = str(args.port)
    if args.config:
        os.environ["JURASSIC_MCP_CONFIG"] = str(Path(args.config).expanduser().resolve())
    return args


def _check_connection(config) -> str:  # noqa: ANN001
    """One-line Informix status for the startup summary (never raises)."""
    from . import catalog
    from .db import connect

    ifx = config.informix
    target = f"{ifx.user}@{ifx.host}:{ifx.port} (server={ifx.server})"
    try:
        with connect(ifx, "sysmaster") as conn:
            dbs = catalog.list_databases(conn)
    except Exception as exc:  # noqa: BLE001
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
        return f"{target}  FAILED: {first}"
    names = ", ".join(d["name"] for d in dbs[:6]) + (", ..." if len(dbs) > 6 else "")
    return f"{target}  OK, {len(dbs)} databases ({names})"


def _print_summary(transport: str, url: str | None, config, status: str | None) -> None:  # noqa: ANN001
    """Print what is needed to connect a client. stderr only (stdout is MCP)."""
    project_root = Path(__file__).resolve().parents[2]
    config_path = os.environ.get("JURASSIC_MCP_CONFIG")
    config_abs = str(Path(config_path).resolve()) if config_path else "(defaults, no config file)"
    lines = ["", "=" * 72, " jurassic-mcp is running", "=" * 72]
    if status:
        lines.append(f" Informix      {status}")
    lines.append(f" Config        {config_abs}")
    lines.append(f" Descriptions  {config.descriptions_file}")

    if url:
        vscode = {"servers": {"jurassic-mcp": {"type": "http", "url": url}}}
        lines += [
            f" Endpoint      {url}",
            "",
            " Claude Code (run once, in any terminal):",
            f"   claude mcp add --transport http jurassic-mcp {url}",
            "",
            " VS Code (.vscode/mcp.json):",
            "   " + json.dumps(vscode),
        ]
    elif (project_root / "pyproject.toml").is_file():
        # STDIO: clients launch the process themselves.
        args = ["run", "--directory", str(project_root), "jurassic-mcp"]
        env = {"JURASSIC_MCP_CONFIG": config_abs} if config_path else {}
        env_flags = " ".join(f"-e {k}={shlex.quote(v)}" for k, v in env.items())
        vscode = {"servers": {"jurassic-mcp": {"command": "uv", "args": args, "env": env}}}
        lines += [
            "",
            " Claude Code:",
            f"   claude mcp add jurassic-mcp {env_flags} -- uv {shlex.join(args)}".replace("  ", " "),
            "",
            " VS Code (.vscode/mcp.json):",
            "   " + json.dumps(vscode),
        ]
    lines += [
        "",
        " Then try:     /mcp__jurassic-mcp__autodiscover <database>   (Claude Code)",
        "               /mcp.jurassic-mcp.autodiscover                (VS Code)",
        "=" * 72,
        "",
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)


def _log_startup_details(config, transport: str) -> None:  # noqa: ANN001
    """Verbose mode: everything useful to diagnose a startup/connection issue."""
    import asyncio
    import importlib.metadata as md
    import logging

    from .server import mcp

    log = logging.getLogger("jurassic_mcp")

    def version(pkg: str) -> str:
        try:
            return md.version(pkg)
        except md.PackageNotFoundError:
            return "?"

    ifx = config.informix
    log.info("python %s · fastmcp %s · IfxPy %s · pid %d",
             sys.version.split()[0], version("fastmcp"), version("IfxPy"), os.getpid())
    log.info("executable %s", sys.executable)
    log.info("INFORMIXDIR=%s", os.environ.get("INFORMIXDIR", "(unset)"))
    log.info("LD_LIBRARY_PATH=%s", os.environ.get("LD_LIBRARY_PATH", "(unset)"))
    log.info("informix host=%s port=%s server=%s protocol=%s user=%s password=%s",
             ifx.host, ifx.port, ifx.server, ifx.protocol, ifx.user,
             "***" if ifx.password else "(empty)")
    log.info("informix db_locale=%s client_locale=%s connect_timeout=%ss driver_options=%s",
             ifx.db_locale or "(default)", ifx.client_locale or "(default)",
             ifx.connect_timeout, ifx.driver_options or "{}")
    n_db = len(config.descriptions)
    n_tab = sum(len(d.tables) for d in config.descriptions.values())
    n_col = sum(len(t.columns) for d in config.descriptions.values() for t in d.tables.values())
    exists = config.descriptions_file and Path(config.descriptions_file).exists()
    log.info("descriptions: %d databases, %d tables, %d columns · file %s (%s)",
             n_db, n_tab, n_col, config.descriptions_file, "exists" if exists else "will be created")
    try:
        tools = asyncio.run(mcp.list_tools())
        prompts = asyncio.run(mcp.list_prompts())
        log.info("tools: %s", ", ".join(t.name for t in tools))
        log.info("prompts: %s", ", ".join(p.name for p in prompts))
    except Exception as exc:  # noqa: BLE001
        log.info("could not list tools/prompts: %s", exc)
    log.info("transport=%s · verbose on: logging MCP requests, tool calls, connections and SQL",
             transport)


def main() -> None:
    """Load configuration and run the MCP server."""
    args = _parse_args()
    _maybe_reexec_with_ifxpy_runtime()

    transport = os.environ.get("JURASSIC_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in _VALID_TRANSPORTS:
        _fail(
            f"Invalid JURASSIC_MCP_TRANSPORT={transport!r}; "
            f"expected one of: {', '.join(_VALID_TRANSPORTS)}"
        )

    from .config import load_config
    from .server import init_server, mcp
    from .verbose import VerboseMiddleware, is_verbose, setup_logging

    setup_logging()

    config_source = os.environ.get("JURASSIC_MCP_CONFIG") or "./config.yml (default)"
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 - any config problem is fatal at startup
        _fail(f"Could not load configuration from {config_source}: {exc}")
    init_server(config)
    if is_verbose():
        _log_startup_details(config, transport)
        mcp.add_middleware(VerboseMiddleware())  # after the self-listing above

    if transport == "stdio":
        _log(f"Starting (transport=stdio, config={config_source})")
        if sys.stderr.isatty():  # started by hand: show how to register it
            _print_summary(transport, None, config, None)
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

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    path = "/sse" if transport == "sse" else "/mcp"
    url = f"http://{shown_host}:{port}{path}"
    status = None if args.no_check else _check_connection(config)
    _print_summary(transport, url, config, status)
    mcp.run(transport=transport, host=host, port=port, show_banner=False)
