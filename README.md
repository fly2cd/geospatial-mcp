# geospatial-mcp

面向 AI Agent 的地理空间 MCP(Model Context Protocol)服务。以 stdio 传输独立运行,不依赖任何特定 Agent、模型或外部配置。

## 能力概览

- **只读元数据检查**:矢量(GeoJSON / GeoPackage / Shapefile)与栅格(GeoTIFF)的图层、CRS、字段、几何与边界信息。
- **有界矢量分析**:平面缓冲区、裁剪、空间连接(需启用工作区)。
- **矢量制图**:渲染带图例、CRS 与坐标边界的 PNG 地图,不发起任何网络请求。
- **任务与成果管理**:每次分析生成隔离任务;成果(GPKG / PNG)通过 MCP resource 以真实字节交付,带 SHA256 校验。
- **受控数据访问**:仅通过管理员登记的 `dataset_id` 引用数据,不接受调用方提供的服务器路径或 URL。

## 工具一览

| 工具 | 说明 | 需要工作区 |
|---|---|---|
| `geo_vector_info` | 矢量元数据:图层、CRS、字段、几何类型、要素数、边界 | 否 |
| `geo_raster_info` | 栅格元数据:尺寸、波段、CRS、边界、像元大小、NoData | 否 |
| `geo_list_datasets` | 列出已登记数据集 ID 与类型,不暴露服务器路径 | 是 |
| `geo_vector_buffer` | 平面缓冲区,保留属性,输出 GPKG | 是 |
| `geo_vector_clip` | 用多边形掩膜裁剪,保留源属性 | 是 |
| `geo_spatial_join` | 一对多平面空间连接(intersects / within / contains) | 是 |
| `geo_render_vector_map` | 渲染 PNG 矢量地图,可选数值字段色带 | 是 |
| `geo_get_task_info` | 读取任务状态、参数、警告与输出元数据 | 是 |
| `geo_get_artifact_info` | 读取成果元数据(ID、URI、大小、SHA256) | 是 |

另提供 resource `artifact://{artifact_id}`,用于读取已完成成果的 GPKG / PNG 二进制内容。完整输入输出契约见 [docs/工具参考.md](docs/工具参考.md)。

## 安装

推荐在项目目录使用独立环境:

```powershell
uv sync --locked --python 3.12 --cache-dir .cache/uv
```

或安装为 Python 包:

```powershell
python -m pip install . --cache-dir .cache/pip
```

`pip install .` 使用声明的直接依赖;需要完整可复现依赖时使用 `uv sync --locked`。

## 快速开始

```powershell
# 生成小型合成演示数据(不下载外部数据)
.\.venv\Scripts\python.exe examples\create_demo_data.py --output examples\data

# 指向数据目录并以 stdio 启动
$env:GEO_DATA_ROOT = (Resolve-Path examples\data).Path
.\.venv\Scripts\geospatial-mcp.exe --transport stdio
```

stdio 服务通常由 MCP 客户端在连接时自动拉起;手动运行后等待输入属正常行为。也可执行 `python -m geospatial_mcp --transport stdio`。`--help` 与 `--version` 不需要配置数据目录。

## 配置

### 数据目录

默认读取 `$GEO_DATA_ROOT/datasets.json`,由管理员登记可对外暴露的数据集:

```json
{
  "version": 1,
  "datasets": {
    "roads_demo": {"path": "roads.geojson", "kind": "vector"},
    "dem_demo": {"path": "dem.tif", "kind": "raster"}
  }
}
```

路径必须相对于数据根目录,禁止 `..`、绝对路径、URL 与 Windows ADS。目录在启动时加载,变更后需重启;每次调用仍会重新校验实际路径、文件存在性与大小。**不要在数据目录中放置模型配置或凭证。**

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GEO_DATA_ROOT` | 必填 | 已存在的绝对数据目录 |
| `GEO_DATA_CATALOG` | `<GEO_DATA_ROOT>/datasets.json` | 数据目录 JSON 的绝对路径 |
| `GEO_MAX_INPUT_BYTES` | `268435456` | 单个输入最大字节数(256 MiB) |
| `GEO_MAX_VECTOR_FEATURES` | `1000000` | 元数据扫描要素上限,超限报错 |
| `GEO_WORKSPACE_ROOT` | 空 | 设置后启用分析与成果工作区(绝对路径,须与数据目录互不包含) |
| `GEO_MAX_ANALYSIS_FEATURES` | `100000` | 单个分析输入要素上限 |
| `GEO_MAX_OUTPUT_FEATURES` | `100000` | 单个成果输出要素上限 |
| `GEO_MAX_ARTIFACT_BYTES` | `16777216` | 单个成果最大字节数(16 MiB) |
| `GEO_LOG_LEVEL` | `INFO` | 日志级别,写 stderr 不写协议 stdout |

不设置 `GEO_WORKSPACE_ROOT` 时仅提供两个元数据工具;设置后启用全部 9 个工具与成果 resource。

## 工作区与成果

启用工作区后,每个分析工具创建一个隔离任务,输出 GPKG 或 PNG 成果。流程为:

1. 调用分析工具(如 `geo_vector_buffer`),返回 `task_id` 与成果元数据(`artifact_id`、`uri`、`size_bytes`、`sha256`)。
2. 用 `geo_get_task_info` / `geo_get_artifact_info` 查询任务与成果状态。
3. 通过 `resources/read` 读取 `artifact://{artifact_id}` 获取真实文件字节,而非服务器文件路径。

矢量成果可在后续分析中作为 `artifact_id` 输入复用,形成链式分析。

## 客户端接入

采用 `mcpServers` 配置格式的客户端示例(按你的实际安装路径替换):

```json
{
  "mcpServers": {
    "geospatial": {
      "command": "<项目目录>/.venv/Scripts/geospatial-mcp.exe",
      "args": ["--transport", "stdio"],
      "env": {
        "GEO_DATA_ROOT": "<数据目录绝对路径>"
      }
    }
  }
}
```

工具发现与调用不依赖客户端当前工作目录。Hermes 等 Agent 的接入方式见 [docs/Hermes接入指南.md](docs/Hermes接入指南.md)。

## 验证

```powershell
# 不调用任何 LLM 的普通 stdio 客户端验证
.\.venv\Scripts\python.exe examples\stdio_client.py --data-root $env:GEO_DATA_ROOT

# 单元测试(生成合成数据,不访问模型)
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

也可使用 MCP Inspector 做交互式调试(仅调试用途,非运行依赖):

```powershell
npx --yes @modelcontextprotocol/inspector --cli -e "GEO_DATA_ROOT=$env:GEO_DATA_ROOT" .\.venv\Scripts\geospatial-mcp.exe --method tools/list
```

## 安全边界与限制

- 仅适用于**可信管理员维护、客户端不可修改**的数据目录;不提供多用户认证授权。
- 目录授权与符号链接检查不构成完整沙箱;不防御管理员并发替换文件的 TOCTOU,也不保证恶意原生格式解析的进程隔离。
- 大小限制是**文件大小**限制,不是解压或解析内存上限。
- 输入目录应设置为只读;应用不修改输入,但不能替代操作系统权限。
- Shapefile 需完整存放于受控目录;不支持远程 URL、GDAL VRT、压缩归档与不可信 pickle。
- 仅提供 stdio 传输;HTTP、上传、进程级硬超时与资源隔离尚未实现,见 [docs/架构与路线图.md](docs/架构与路线图.md)。

## 文档

- [docs/工具参考.md](docs/工具参考.md):全部工具与 resource 的输入输出契约。
- [docs/架构与路线图.md](docs/架构与路线图.md):分层架构、设计原则、安全模型与后续路线。
- [docs/工具迁移清单.md](docs/工具迁移清单.md):后续扩展规划——旧能力逐项筛选与分批迁移建议(仅规划,不代表已实现)。
- [docs/Hermes接入指南.md](docs/Hermes接入指南.md):复用本机 Hermes 的接入与成果导出。
- [dev-log/日志模板.md](dev-log/日志模板.md):开发阶段记录模板。
