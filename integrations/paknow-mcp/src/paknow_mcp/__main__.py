"""CLI entry point for ``paknow-mcp`` and ``python -m paknow_mcp``."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import Settings
from .server import build_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paknow-mcp",
        description=(
            "MCP server for the PAknow price-action evidence base. "
            "Defaults to stdio for use by Pi, Claude Desktop, Cursor, and other MCP clients."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host.")
    parser.add_argument("--port", type=int, default=8765, help="HTTP bind port.")
    parser.add_argument(
        "--path",
        default="/mcp",
        help="Streamable HTTP path (default: /mcp).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="PAknow API base URL (default: PAKNOW_BASE_URL or http://127.0.0.1:8000).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="PAknow request timeout in seconds (default: 300).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token for a non-loopback PAknow service.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Diagnostic log level (stderr only).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    settings = Settings.from_env(
        base_url=args.base_url,
        token=args.token,
        timeout_seconds=args.timeout,
    )
    server = build_server(settings)
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            server.run(
                "streamable-http",
                host=args.host,
                port=args.port,
                streamable_http_path=args.path,
            )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
