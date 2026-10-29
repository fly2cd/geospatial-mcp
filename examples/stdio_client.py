"""No-LLM, subprocess stdio smoke test against an installed geospatial-mcp package."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def run(data_root: Path):
    transport = StdioTransport(command=sys.executable, args=["-m", "geospatial_mcp", "--transport", "stdio"], env={"GEO_DATA_ROOT": str(data_root.resolve())}, keep_alive=False)
    async with Client(transport, timeout=30, init_timeout=30) as client:
        tools = await client.list_tools()
        assert {"geo_vector_info", "geo_raster_info"} <= {tool.name for tool in tools}
        print("Tools:", ", ".join(tool.name for tool in tools))
        for tool, dataset in [("geo_vector_info", "roads_demo"), ("geo_raster_info", "dem_demo")]:
            result = await client.call_tool(tool, {"dataset_id": dataset})
            assert not result.is_error and result.structured_content is not None
            print(json.dumps(result.structured_content, ensure_ascii=False, indent=2, allow_nan=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.data_root))


if __name__ == "__main__":
    main()
