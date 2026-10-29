只使用 geospatial MCP 工具完成本项目合成数据测试。
先查询已登记数据并检查 points 的元数据；对现有 points 要素做 10 米缓冲，显式设置 analysis_crs 为 EPSG:32650。
对缓冲成果按数值字段 value 制图，标题使用 ASCII 文本 Hermes buffer demo。
通过 read_resource 实际下载缓冲 GPKG 和地图 PNG。
请用中文报告实际要素数量、CRS、警告、artifact_id、工具给出的 SHA256、真实客户端缓存路径，并明确区分工具报告的散列与客户端另行校验的散列。
不要生成新输入数据、调用其他工具集或修改配置，完成后停止。