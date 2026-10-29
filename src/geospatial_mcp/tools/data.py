"""Exactly two explicitly registered, read-only metadata tools for phase A."""

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import GeoError
from ..schemas import DatasetId, RasterInfo, VectorInfo
from ..services.data import inspect_raster, inspect_vector
from ..storage import DatasetStore

ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def register_data_tools(mcp: FastMCP, datasets: DatasetStore) -> None:
    @mcp.tool(name="geo_vector_info", annotations=ANNOTATIONS)
    def geo_vector_info(
        dataset_id: DatasetId,
        layer: Annotated[str | None, Field(min_length=1, max_length=255)] = None,
    ) -> VectorInfo:
        """检查已注册矢量数据的图层、CRS、字段、几何类型、要素数量和原始坐标边界。

        dataset_id 来自管理员的数据目录，不接受文件路径或 URL。
        多图层数据必须指定 layer；缺失 CRS 不会被猜测，不计算面积或字段统计。
        """
        try:
            path = datasets.resolve_readable(dataset_id, "vector")
            return inspect_vector(path, layer=layer, max_features=datasets.settings.max_vector_features)
        except GeoError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(name="geo_raster_info", annotations=ANNOTATIONS)
    def geo_raster_info(dataset_id: DatasetId) -> RasterInfo:
        """检查已注册 GeoTIFF 的尺寸、波段、CRS、边界、像元大小及 NoData 元数据。

        只读元数据，不扫描像元统计。距离单位以源 CRS 为准；地理坐标系不估算米制分辨率。
        NaN 和无穷 NoData 使用字符串表示，以保证标准 JSON 可序列化。
        """
        try:
            path = datasets.resolve_readable(dataset_id, "raster")
            return inspect_raster(path)
        except GeoError as exc:
            raise ToolError(str(exc)) from None
