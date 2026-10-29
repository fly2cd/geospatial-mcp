"""No-LLM stdio workflow: buffer -> clip -> join -> PNG -> actual resource downloads."""

import argparse
import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import sys

import fiona
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from PIL import Image


async def run(data_root: Path, workspace_root: Path, download_root: Path):
    transport = StdioTransport(command=sys.executable, args=["-m", "geospatial_mcp"],
        env={"GEO_DATA_ROOT": str(data_root), "GEO_WORKSPACE_ROOT": str(workspace_root), "GEO_LOG_LEVEL": "ERROR"},
        cwd=str(download_root), keep_alive=False)
    report = {"tools": [], "steps": [], "downloads": []}
    async with Client(transport, timeout=60, init_timeout=30) as client:
        report["tools"] = sorted(tool.name for tool in await client.list_tools())
        if len(report["tools"]) != 9:
            raise RuntimeError("Expected the nine explicit stage A/B tools")
        async def call(tool, args):
            response = await client.call_tool(tool, args)
            if response.is_error or response.structured_content is None:
                raise RuntimeError("Tool did not return a structured success")
            result = response.structured_content
            report["steps"].append({"tool": tool, "result": result})
            return result
        buffered = await call("geo_vector_buffer", {"source": {"dataset_id": "points"}, "distance_meters": 10})
        clipped = await call("geo_vector_clip", {"source": {"artifact_id": buffered["artifacts"][0]["artifact_id"]}, "mask": {"dataset_id": "mask"}})
        joined = await call("geo_spatial_join", {"left": {"artifact_id": clipped["artifacts"][0]["artifact_id"]}, "right": {"dataset_id": "mask"}})
        mapped = await call("geo_render_vector_map", {"source": {"artifact_id": joined["artifacts"][0]["artifact_id"]}, "value_field": "left_value", "title": "Buffer coverage - spatial join"})
        counts = [result["summary"]["feature_count"] for result in [buffered, clipped, joined, mapped]]
        if counts != [3, 2, 3, 3]:
            raise RuntimeError(f"Unexpected fixture counts: {counts}")
        for name, result in zip(["buffer", "clip", "join", "map"], [buffered, clipped, joined, mapped]):
            artifact = result["artifacts"][0]
            contents = await client.read_resource(artifact["uri"])
            if len(contents) != 1 or contents[0].mime_type != artifact["mime_type"]:
                raise RuntimeError("Unexpected resource content type")
            payload = base64.b64decode(contents[0].blob, validate=True)
            if len(payload) != artifact["size_bytes"] or hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
                raise RuntimeError("Downloaded artifact failed integrity validation")
            filename = f"{name}.{artifact['format']}"
            path = download_root / filename
            with path.open("xb") as output:
                output.write(payload)
            if artifact["format"] == "gpkg":
                with fiona.open(path, layer="result") as src:
                    if len(src) != result["summary"]["feature_count"]:
                        raise RuntimeError("Downloaded GPKG failed feature count validation")
            else:
                with Image.open(io.BytesIO(payload)) as image:
                    if image.size != (1000, 700):
                        raise RuntimeError("Downloaded PNG has unexpected dimensions")
                    image.verify()
            report["downloads"].append({"filename": filename, "artifact_id": artifact["artifact_id"], "sha256": artifact["sha256"], "size_bytes": len(payload)})
            status = await client.call_tool("geo_get_task_info", {"task_id": result["task_id"]})
            if status.structured_content["status"] != "succeeded":
                raise RuntimeError("Task did not persist success")
    (download_root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--download-root", type=Path, required=True)
    args = parser.parse_args()
    download = args.download_root.resolve()
    if download.exists():
        parser.error("Choose a new download directory; existing directories are not overwritten.")
    download.mkdir(parents=True)
    asyncio.run(run(args.data_root.resolve(), args.workspace_root.resolve(), download))


if __name__ == "__main__":
    main()
