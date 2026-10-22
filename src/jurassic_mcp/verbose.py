"""Verbose diagnostics (``--verbose`` / ``JURASSIC_MCP_VERBOSE=1``).

Everything goes to stderr through the ``jurassic_mcp`` logger, never stdout
(the MCP STDIO channel). Without verbose mode only warnings are shown.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from fastmcp.server.middleware import Middleware

log = logging.getLogger("jurassic_mcp")

_COLOR = sys.stderr.isatty() or os.environ.get("JURASSIC_MCP_COLOR") == "1"
_RESET, _DIM, _RED, _GREEN, _CYAN, _YELLOW = (
    ("\033[0m", "\033[2m", "\033[31m", "\033[32m", "\033[36m", "\033[33m")
    if _COLOR
    else ("",) * 6
)


def dim(text: str) -> str:
    return f"{_DIM}{text}{_RESET}"


def ok(text: str) -> str:
    return f"{_GREEN}{text}{_RESET}"


def bad(text: str) -> str:
    return f"{_RED}{text}{_RESET}"


def cyan(text: str) -> str:
    return f"{_CYAN}{text}{_RESET}"


def yellow(text: str) -> str:
    return f"{_YELLOW}{text}{_RESET}"


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        stamp += f".{int(record.msecs):03d}"
        area = record.name.removeprefix("jurassic_mcp").lstrip(".") or "main"
        level = ""
        if record.levelno >= logging.ERROR:
            level = bad("ERROR ")
        elif record.levelno >= logging.WARNING:
            level = yellow("WARN ")
        return f"{dim(stamp)} {cyan(f'{area:<7}')} {level}{record.getMessage()}"


def is_verbose() -> bool:
    return os.environ.get("JURASSIC_MCP_VERBOSE", "").lower() in ("1", "true", "yes")


def setup_logging() -> None:
    """Configure the ``jurassic_mcp`` logger (idempotent)."""
    if getattr(log, "_jurassic_configured", False):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter())
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if is_verbose() else logging.WARNING)
    log.propagate = False
    log._jurassic_configured = True  # type: ignore[attr-defined]


def short(value: Any, limit: int = 160) -> str:
    """Compact one-line repr for logs."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summarise(data: Any) -> str:
    """Human summary of a tool's structured result: list sizes and flags."""
    if not isinstance(data, dict):
        return short(data, 120)
    parts = []
    for key, value in data.items():
        if isinstance(value, list):
            parts.append(f"{key}={len(value)}")
        elif isinstance(value, bool) or key in ("count", "row_count_source", "error", "stage"):
            parts.append(f"{key}={short(value, 120)}")
    return ", ".join(parts) or short(data, 120)


class VerboseMiddleware(Middleware):
    """Logs every MCP request: client handshake, listings, tool/prompt calls."""

    _log = logging.getLogger("jurassic_mcp.mcp")

    async def on_initialize(self, context, call_next):  # noqa: ANN001, ANN201
        params = context.message.params if hasattr(context.message, "params") else context.message
        info = getattr(params, "clientInfo", None) or getattr(params, "client_info", None)
        proto = getattr(params, "protocolVersion", None) or getattr(params, "protocol_version", "")
        who = f"{getattr(info, 'name', '?')} {getattr(info, 'version', '')}".strip()
        self._log.info("client connected: %s (protocol %s)", ok(who), proto)
        return await call_next(context)

    async def on_list_tools(self, context, call_next):  # noqa: ANN001, ANN201
        result = await call_next(context)
        self._log.info("client listed tools (%d)", len(result))
        return result

    async def on_list_prompts(self, context, call_next):  # noqa: ANN001, ANN201
        result = await call_next(context)
        self._log.info("client listed prompts (%d)", len(result))
        return result

    async def on_get_prompt(self, context, call_next):  # noqa: ANN001, ANN201
        msg = context.message
        self._log.info("→ prompt %s %s", cyan(msg.name), short(msg.arguments or {}))
        return await call_next(context)

    async def on_call_tool(self, context, call_next):  # noqa: ANN001, ANN201
        msg = context.message
        self._log.info("→ %s %s", cyan(msg.name), short(msg.arguments or {}, 300))
        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            ms = (time.perf_counter() - start) * 1000
            self._log.info(
                "%s %s %s · %s", bad("✗"), msg.name, dim(f"{ms:.0f} ms"), bad(short(str(exc), 400))
            )
            raise
        ms = (time.perf_counter() - start) * 1000
        data = getattr(result, "structured_content", None)
        self._log.info("%s %s %s · %s", ok("✓"), msg.name, dim(f"{ms:.0f} ms"), _summarise(data))
        return result
