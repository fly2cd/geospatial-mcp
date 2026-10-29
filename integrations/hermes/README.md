# 本机 Hermes 接入（C1）

这里是项目侧适配代码，不属于 `geospatial-mcp` 的运行时服务包，也不需要把 GIS 库装进 Hermes。

完整指南：[本机 Hermes 接入](../../docs/Hermes接入指南.md)。

在 **`geospatial-mcp` 项目根目录**运行，不依赖 GABench 的根目录启动器：

```powershell
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py test
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py smoke
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py chat
```

`test/smoke` 不调用模型；`chat` 使用现有模型并产生相应调用费用。GABench 根目录原 `hermes-geo.cmd` 仍可使用。本机 Windows PowerShell 策略会拦截 `.ps1`，无需为了这些入口更改系统策略。

测试（不会使用模型）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s integrations/hermes/tests -v
```

本机 C1 已验证 Hermes Agent 0.21.1，模型 alibaba-cn / qwen3.8-max。CLI 工具集参数是 `geospatial`，不是内部注册名 `mcp-geospatial`。使用独立项目 HERMES_HOME，不修改原 Hermes 全局配置。

`smoke --report` 只接受项目运行目录 `reports/` 或本项目 `dev-log/stage-c1-*.json`，不再向父项目日志目录写入。C2 尚未实施。
