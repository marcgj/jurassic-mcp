"""jurassic-mcp package entry point."""

from .config import load_config
from .server import init_server, mcp


def main() -> None:
    """Load configuration and run the MCP server over STDIO."""
    config = load_config()
    init_server(config)
    mcp.run()
