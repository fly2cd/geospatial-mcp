import asyncio
import base64
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import fiona
from PIL import Image
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from analysis_fixtures import make_analysis_fixture
from geospatial_mcp.server import create_server
from geospatial_mcp.settings import Settings

EXPECTED = {"geo_vector_info", "geo_raster_info", "geo_list_datasets", "geo_get_task_info", "geo_get_artifact_info",
            "geo_vector_buffer", "geo_vector_clip", "geo_spatial_join", "geo_render_vector_map"}


class AnalysisMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data = self.root / "data"
        make_analysis_fixture(data)
        self.settings = Settings(data, data / "datasets.json", workspace_root=self.root / "workspace")
        self.cwd = self.root / "unrelated"
        self.cwd.mkdir()
        self.env = {"GEO_DATA_ROOT": str(data), "GEO_WORKSPACE_ROOT": str(self.settings.workspace_root), "GEO_LOG_LEVEL": "ERROR", "PYTHONUTF8": "1"}

    async def test_explicit_tool_contracts_and_resource_template(self):
        async with Client(create_server(self.settings)) as client:
            tools = await client.list_tools()
            self.assertEqual({tool.name for tool in tools}, EXPECTED)
            for tool in tools:
                self.assertIsNotNone(tool.output_schema)
                self.assertFalse(tool.annotations.destructive_hint)
                if tool.name in {"geo_vector_buffer", "geo_vector_clip", "geo_spatial_join", "geo_render_vector_map"}:
                    self.assertFalse(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.idempotent_hint)
            templates = await client.list_resource_templates()
            self.assertEqual([item.uri_template for item in templates], ["artifact://{artifact_id}"])
            listing = await client.call_tool("geo_list_datasets", {})
            self.assertEqual({item["dataset_id"] for item in listing.structured_content["datasets"]}, {"mask", "points"})
            self.assertNotIn(str(self.root), str(listing.structured_content))

    async def _workflow(self, client):
        async def call(name, args):
            response = await client.call_tool(name, args)
            self.assertFalse(response.is_error)
            return response.structured_content
        buffered = await call("geo_vector_buffer", {"source": {"dataset_id": "points"}, "distance_meters": 10})
        clipped = await call("geo_vector_clip", {"source": {"artifact_id": buffered["artifacts"][0]["artifact_id"]}, "mask": {"dataset_id": "mask"}})
        joined = await call("geo_spatial_join", {"left": {"artifact_id": clipped["artifacts"][0]["artifact_id"]}, "right": {"dataset_id": "mask"}})
        mapped = await call("geo_render_vector_map", {"source": {"artifact_id": joined["artifacts"][0]["artifact_id"]}, "value_field": "left_value", "title": "Coverage"})
        self.assertEqual([result["summary"]["feature_count"] for result in [buffered, clipped, joined, mapped]], [3, 2, 3, 3])
        for result in [buffered, clipped, joined, mapped]:
            artifact = result["artifacts"][0]
            info = await call("geo_get_artifact_info", {"artifact_id": artifact["artifact_id"]})
            self.assertEqual(info["sha256"], artifact["sha256"])
            contents = await client.read_resource(artifact["uri"])
            self.assertEqual(len(contents), 1)
            data = base64.b64decode(contents[0].blob)
            self.assertEqual(contents[0].mime_type, artifact["mime_type"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), artifact["sha256"])
            if artifact["format"] == "png":
                with Image.open(io.BytesIO(data)) as image:
                    self.assertEqual(image.size, (1000, 700))
            else:
                path = self.cwd / (artifact["artifact_id"] + ".gpkg")
                path.write_bytes(data)
                with fiona.open(path, layer="result") as src:
                    self.assertEqual(len(src), result["summary"]["feature_count"])
        status = await call("geo_get_task_info", {"task_id": mapped["task_id"]})
        self.assertEqual(status["status"], "succeeded")

    async def test_in_memory_full_analysis_download_workflow(self):
        async with Client(create_server(self.settings), timeout=60) as client:
            await self._workflow(client)

    async def test_stdio_full_workflow_outside_repository(self):
        transport = StdioTransport(command=sys.executable, args=["-m", "geospatial_mcp"], env=self.env, cwd=str(self.cwd), keep_alive=False)
        async with Client(transport, timeout=60, init_timeout=30) as client:
            await self._workflow(client)

    async def test_official_sdk_actual_binary_resource_and_restart(self):
        params = StdioServerParameters(command=sys.executable, args=["-m", "geospatial_mcp"], env=self.env, cwd=self.cwd)
        async with asyncio.timeout(60):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    self.assertEqual(len((await client.list_tools()).tools), 9)
                    result = await client.call_tool("geo_vector_buffer", {"source": {"dataset_id": "points"}, "distance_meters": 10})
                    self.assertFalse(result.is_error)
                    artifact = result.structured_content["artifacts"][0]
                    resource = await client.read_resource(artifact["uri"])
                    payload = base64.b64decode(resource.contents[0].blob)
                    self.assertEqual(hashlib.sha256(payload).hexdigest(), artifact["sha256"])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    resource = await client.read_resource(artifact["uri"])
                    self.assertEqual(base64.b64decode(resource.contents[0].blob), payload)

    async def test_schema_errors_and_task_failure_not_reported_as_success(self):
        async with Client(create_server(self.settings)) as client:
            for args in [{"source": {}, "distance_meters": 10}, {"source": {"dataset_id": "points"}, "distance_meters": -1},
                         {"source": {"dataset_id": "../secret"}, "distance_meters": 10}, {"source": {"dataset_id": "points", "path": "x"}, "distance_meters": 10}]:
                result = await client.call_tool("geo_vector_buffer", args, raise_on_error=False)
                self.assertTrue(result.is_error)
            failed = await client.call_tool("geo_vector_clip", {"source": {"dataset_id": "points"}, "mask": {"dataset_id": "points"}}, raise_on_error=False)
            self.assertTrue(failed.is_error)
            text = " ".join(getattr(item, "text", "") for item in failed.content)
            self.assertIn("POLYGON_MASK_REQUIRED", text)
            self.assertNotIn(str(self.root), text)
            task_id = re.search(r"task_[a-f0-9]{32}", text).group()
            task = await client.call_tool("geo_get_task_info", {"task_id": task_id})
            self.assertEqual(task.structured_content["status"], "failed")
            self.assertEqual(task.structured_content["artifacts"], [])
            with self.assertRaises(Exception) as caught:
                await client.read_resource("artifact://art_" + task_id.removeprefix("task_"))
            self.assertNotIn(str(self.root), str(caught.exception))

    async def test_concurrent_mcp_calls_do_not_collide(self):
        async with Client(create_server(self.settings), timeout=60) as client:
            results = await asyncio.gather(*[client.call_tool("geo_vector_buffer", {"source": {"dataset_id": "points"}, "distance_meters": distance}) for distance in [5, 10, 15]])
            self.assertEqual(len({r.structured_content["task_id"] for r in results}), 3)
            for response in results:
                self.assertFalse(response.is_error)

    def test_analysis_import_has_no_mcp_or_model_dependency(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GEO_", "OPENAI_", "ANTHROPIC_")) and key != "PYTHONPATH"}
        result = subprocess.run([sys.executable, "-c", "import geospatial_mcp.operations; import sys; assert 'fastmcp' not in sys.modules; assert 'geopandas' not in sys.modules; assert 'toolbox.Vector' not in sys.modules"], cwd=self.cwd, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
