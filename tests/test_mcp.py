import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from fixtures import make_fixture
from geospatial_mcp.server import create_server
from geospatial_mcp.settings import Settings


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        make_fixture(self.root)
        self.cwd = Path(self.tmp.name) / "unrelated-cwd"
        self.cwd.mkdir()
        self.settings = Settings(self.root, self.root / "datasets.json")
        self.env = {"GEO_DATA_ROOT": str(self.root), "GEO_LOG_LEVEL": "ERROR", "PYTHONUTF8": "1"}

    async def test_explicit_tools_and_schemas(self):
        async with Client(create_server(self.settings)) as client:
            tools = await client.list_tools()
            self.assertEqual({tool.name for tool in tools}, {"geo_vector_info", "geo_raster_info"})
            for tool in tools:
                self.assertEqual(tool.input_schema["required"], ["dataset_id"])
                self.assertIn("pattern", tool.input_schema["properties"]["dataset_id"])
                self.assertIsNotNone(tool.output_schema)
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)

    async def test_structured_outputs(self):
        async with Client(create_server(self.settings)) as client:
            for name, dataset, kind in [("geo_vector_info", "roads", "vector"), ("geo_raster_info", "dem", "raster")]:
                result = await client.call_tool(name, {"dataset_id": dataset})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["kind"], kind)
                json.dumps(result.structured_content, allow_nan=False)

    async def test_errors_are_marked_and_paths_not_leaked(self):
        async with Client(create_server(self.settings)) as client:
            for arguments, code in [({"dataset_id": "missing"}, "DATASET_NOT_FOUND"), ({"dataset_id": "dem"}, "DATASET_TYPE_MISMATCH"), ({"dataset_id": "../roads"}, None), ({}, None), ({"dataset_id": 123}, None), ({"dataset_id": "roads", "unexpected": 1}, None)]:
                with self.subTest(arguments=arguments):
                    result = await client.call_tool("geo_vector_info", arguments, raise_on_error=False)
                    self.assertTrue(result.is_error)
                    text = " ".join(getattr(block, "text", "") for block in result.content)
                    self.assertNotIn(str(self.root), text)
                    if code:
                        self.assertIn(code, text)

    async def test_fastmcp_stdio_outside_repository(self):
        transport = StdioTransport(command=sys.executable, args=["-m", "geospatial_mcp", "--transport", "stdio"], env=self.env, cwd=str(self.cwd), keep_alive=False)
        async with Client(transport, timeout=30, init_timeout=30) as client:
            tools = await client.list_tools()
            self.assertEqual(len(tools), 2)
            result = await client.call_tool("geo_vector_info", {"dataset_id": "roads"})
            self.assertEqual(result.structured_content["feature_count"], 3)

    async def test_official_sdk_stdio_and_error_flag(self):
        params = StdioServerParameters(command=sys.executable, args=["-m", "geospatial_mcp"], env=self.env, cwd=self.cwd)
        async with asyncio.timeout(40):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.server_info.name, "Geospatial Tools")
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 2)
                    result = await session.call_tool("geo_raster_info", {"dataset_id": "dem"})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content["width"], 4)
                    failed = await session.call_tool("geo_raster_info", {"dataset_id": "missing"})
                    self.assertTrue(failed.is_error)

    def test_import_and_help_need_no_model_or_data_config(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GEO_", "OPENAI_", "ANTHROPIC_")) and k not in {"PYTHONPATH", "API_KEY"}}
        code = "import geospatial_mcp.services.data; import sys; assert 'fastmcp' not in sys.modules; import geospatial_mcp.server; assert 'toolbox.Data' not in sys.modules; assert 'geopandas' not in sys.modules"
        for args in [["-c", code], ["-m", "geospatial_mcp", "--help"], ["-m", "geospatial_mcp", "--version"]]:
            result = subprocess.run([sys.executable, *args], cwd=self.cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, "-m", "geospatial_mcp"], cwd=self.cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("GEO_DATA_ROOT", result.stderr)
