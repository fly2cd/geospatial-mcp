"""Standalone stdio entry point; no import-time config reads or tool discovery."""

import argparse
import logging
import sys

from fastmcp import FastMCP

from . import __version__
from .errors import ConfigurationError
from .settings import Settings
from .storage import DatasetStore
from .tools.data import register_data_tools


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings if settings is not None else Settings.from_environment()
    datasets = DatasetStore(settings)
    server = FastMCP(
        "Geospatial Tools",
        version=__version__,
        instructions="Use registered dataset IDs, never server paths. When analysis is enabled, vector artifacts can be reused as inputs and downloaded through resources/read.",
        mask_error_details=True,
        strict_input_validation=True,
        tasks=False,
    )
    register_data_tools(server, datasets)
    if settings.workspace_root is not None:
        from .workspace import TaskStore
        from .tools.analysis import register_analysis_tools
        register_analysis_tools(server, datasets, TaskStore(settings))
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent geospatial MCP service (stages A/B: stdio only).")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    parser.add_argument("--version", action="version", version=f"geospatial-mcp {__version__}")
    args = parser.parse_args()
    try:
        settings = Settings.from_environment()
        logging.basicConfig(level=settings.log_level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s", force=True)
        server = create_server(settings)
    except (ConfigurationError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    server.run(transport=args.transport, show_banner=False, log_level=settings.log_level)


if __name__ == "__main__":
    main()
