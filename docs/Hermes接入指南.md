# 本机 Hermes 接入指南

本指南说明如何复用**已安装**的 Nous Research Hermes Agent,通过 stdio 连接本服务完成地理空间分析。接入代码位于 `integrations/hermes/`,**不向 MCP 服务包添加任何 Hermes 依赖**,服务本身保持 Agent 无关。

## 1. 工作方式

启动器复用本机已有的 Hermes 安装,设置**项目专用 HERMES_HOME**,再经 stdio 拉起独立 GIS 服务:

```text
项目 Python 入口 → 已安装的 Hermes → stdio MCP → geospatial-mcp
                        │                           │
                 原 Hermes Python             GIS 独立 .venv
```

- 不重新安装 Hermes,不向 Hermes 环境安装 GIS 依赖,不修改其原 `config.yaml`。
- 直接运行原 `hermes` 仍使用原配置,不会自动获得本项目工具;请使用本项目入口。
- 初始化仅从原配置筛选模型名、provider、base URL 生成最小配置,不复制原配置全文;对未知配置、路径越界或不一致设置明确拒绝而非覆盖。

## 2. 命令

以下命令在**项目根目录**执行:

```powershell
# 检查/幂等初始化项目配置,不调用模型
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py setup

# 用 Hermes 自带 MCP 测试命令验证连接,不调用模型
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py test

# 通过真实 Hermes 工具注册与调用机制跑完整流程,不调用模型
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py smoke

# 启动地理空间智能体,会使用现有模型并产生调用费用
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py chat
```

交互对话示例:

> 检查 points 数据,对它做 10 米缓冲,使用适合该数据的 EPSG:32650 分析坐标系;按 value 字段生成标题为 Hermes buffer demo 的地图,下载 GPKG 和 PNG,报告数量、CRS、警告、成果 ID 和下载路径。

其中的 EPSG:32650 **仅适用于项目合成演示数据**,不能直接套用到其他地区。

也可用 UTF-8 文件提交任务,避免终端管道造成中文乱码:

```powershell
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py chat --query-file integrations\hermes\demo-query.md --max-turns 12
```

`--max-turns` 取值 1–20,默认 12;它是模型轮数设置,不是进程硬超时或费用上限。

## 3. 文件与配置

相对项目根目录:

```text
integrations/hermes/
  project.py            安装发现、配置检查、环境与启动
  smoke_runtime.py      无模型的真实 Hermes 联调
  export_artifact.py    通过标准 MCP 下载带正确扩展名的成果
  instructions.md       地理空间 SOUL.md 初始模板
  demo-query.md         UTF-8 演示任务
  tests/test_project.py 接入层自动化测试
.cache/hermes-project/
  home/config.yaml      项目专用 Hermes 配置
  home/SOUL.md          地理空间助手约束
  workspace/tasks/      服务端任务记录与成果
  exports/              客户端校验后的 .gpkg / .png
  reports/              smoke 默认报告,每次使用新文件名
```

`.cache/` 已被 `.gitignore` 忽略;会话与工具输出可能包含用户数据,不要提交运行目录。

Hermes 使用 YAML **`mcp_servers`**(不是客户端 JSON 的 `mcpServers`),关键结构由启动器生成:

```yaml
mcp_servers:
  geospatial:
    command: <GIS环境的绝对python.exe路径>
    args: [-m, geospatial_mcp, --transport, stdio]
    env:
      GEO_DATA_ROOT: <登记数据目录的绝对路径>
      GEO_WORKSPACE_ROOT: <项目workspace的绝对路径>
      GEO_LOG_LEVEL: ERROR
      PYTHONUTF8: '1'
    connect_timeout: 30
    timeout: 60
    tools:
      include: [geo_*]
      resources: true
      prompts: false
```

**三个名称不要混用:**

- 配置名及 CLI `--toolsets` 参数:`geospatial`。
- Hermes 内部注册工具集:`mcp-geospatial`。
- Hermes 调用工具名示例:`mcp__geospatial__geo_vector_buffer`。

CLI 误用内部工具集名称会导致跳过 MCP 启动。服务自身提供 9 个工具;Hermes 另生成 `list_resources`、`read_resource`,故注册结果为 11 个工具。真实模型还可能使用 Hermes 的 `tool_describe`/`tool_call` 调度工具,不应把它们当作新增 GIS 算法。

需要长期保存联调报告时,显式指定本项目日志目录(保留 `stage-c1-` 前缀,每次使用新文件名):

```powershell
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py smoke --report dev-log\stage-c1-local-smoke-001.json
```

## 4. 下载与导出成果

`artifact://...` 是 MCP resource URI,不是浏览器下载地址。智能体经 `read_resource` 获取内容后,Hermes 给出真实客户端缓存路径;本机可能把 GPKG 缓存为 `.bin`。需要正确扩展名时使用导出器:

```powershell
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py export --artifact-id <实际返回的ID>
```

导出器通过标准 `geo_get_artifact_info` + `resources/read` 获取文件,不绕过 MCP 复制服务端路径;校验 ID、URI、格式/MIME、大小与 SHA256,只保存到项目 `exports/`;相同内容可复用,已有不同内容拒绝覆盖。客户端单成果上限 64 MiB,服务默认限制更小(以服务配置为准);该上限不等于传输/解析的进程内存隔离。

## 5. 使用自己的数据

1. 由管理员准备只读、可信的数据目录,按 [README](../README.md) 的格式建立 `datasets.json`;路径须相对该目录,禁止远程 URL 与目录穿越。
2. 新副本可运行 `setup --data-root <绝对数据目录>`;之后的启动保留该选择。
3. 切换数据先结束项目会话,修改项目 `home/config.yaml` 中 `mcp_servers.geospatial.env.GEO_DATA_ROOT`,再运行 `setup` 检查;不要修改原 Hermes 全局配置。
4. `smoke` 针对合成数据 `points`/`mask` 与固定结果数量,不是任意生产数据的通用测试;新数据先用 `test` 检查连接再调用元数据工具。

新机器先按 [README](../README.md) 创建独立环境;首次使用演示数据时执行:

```powershell
.\.venv\Scripts\python.exe examples\create_analysis_demo.py --output examples\analysis-data
.\.venv\Scripts\python.exe -X utf8 integrations\hermes\project.py setup
```

项目移动到新路径时应重建独立环境并重新初始化项目专用配置,不要照搬含绝对路径与会话状态的 `.venv`/`.cache`。自动发现逻辑面向本机 Windows 安装布局;其他布局需适配或提供 `--hermes-home`。

## 6. 凭证与数据边界

- `setup`/`test`/`smoke`/`export` 不需要模型 API Key,不触发 LLM 请求。
- `chat` 从进程环境或原 Hermes `.env` 按所选 provider 白名单读取凭证,仅注入子进程环境;不把密钥写入配置、命令参数或日志,也不复制原 `.env`/`auth.json`。
- 模型对话会把提示、工具 schema 与返回的数据摘要发送到所配置的模型端点;涉及敏感地理数据前先确认可发送范围。
- 专用 HERMES_HOME 与工具集限制**不是操作系统沙箱**,也不是多用户授权机制;不要用于不可信目录或公开服务。
