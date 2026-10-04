# 验一下 · 提取能力与主图框架

已实现「上传图片 → 提交地点、描述、多个图片和链接 → browser-use 提取 claim」，并接入 LangGraph 主图：提取后将完整 Claim 列表交给类别子图，再汇总执行结果。四类子图目前为明确返回未实现状态的占位图。原生 HTML/JS 前端由 FastAPI 一并提供，无需 Node 构建或单独的前端服务。

## 代码分层

```text
backend/
  main.py                  # 组装依赖、初始化存储、挂载前端
  api.py                   # 两个 HTTP 接口及请求校验
  extraction/
    service.py             # 读取图片、调用 Agent、校验来源并整理 claim
    models.py              # Claim、材料来源与提取结果
  verification/
    service.py             # 运行主图，并适配现有提取响应
    graph.py               # 初始化、提取、上下文、子图分发、汇合与结果组装
    state.py               # 主图和子图 State、并行结果合并
    models.py              # 上下文、证据、子图发现与完整运行结果
    capabilities.py        # 可注入的地点解析和证据来源，预留 Web Search
    subgraphs/__init__.py  # 四类占位子图和注册入口
  storage/
    repository.py          # StorageRepository：SQLite 元数据与本地文件
    models.py              # 图片标识、MIME 与原始 bytes
    schema.sql             # Storage 自己的建表脚本
  agent.py                 # browser-use、多模态消息、模型协议与提示词
```

上传接口直接调用 Storage Repository；评估接口调用 VerificationService，由主图的提取节点调用 ClaimExtractionService。提取模块通过注入的 Repository 取图，再调用注入的 Agent 提取函数，并保留来源校验与顺序编号规则。Agent 接收地点、描述、链接和图片，不依赖 HTTP 请求 DTO。

## 启动

在仓库根目录执行，使用 Python 3.12+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
pip install playwright
python -m playwright install chromium --no-shell
cp -n backend/config.example.toml backend/config.toml  # 保留已有配置
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 。Windows 激活命令为 `.venv\Scripts\Activate.ps1`。
上面的 Playwright 命令安装本地 Chromium；Linux 如果缺少浏览器系统库，执行 `python -m playwright install-deps chromium`。已有 Chrome/Chromium 时可以跳过浏览器安装，在配置中指定 `browser_executable_path`。

在 `backend/config.toml` 填写 `api_key`，按供应商设置 `base_url` 和 `model`。支持提供图片输入及 JSON Schema 输出的 OpenAI 兼容模型，也兼容 `deepseek-flash`（使用 JSON mode 后本地校验结构，并修正 browser-use 0.13.10 自动关闭 DeepSeek 视觉的旧判断）。`config.example.toml` 中密钥留空；实际配置文件已被 Git 忽略。程序依次读取 `config.local.toml`、`config.toml`、`config.example.toml` 中首个存在的文件。未配置密钥时可以上传图片，评估返回明确的 503 提示。

后端启动时按 `backend/storage/schema.sql` 自动创建 `backend/data/files.sqlite3`，图片保存在 `backend/data/uploads/`。SQLite 是嵌入式数据库，无需单独启动数据库进程。当前为本地单会话骨架，`order` 按上传递增；传入模型的图片顺序始终以本次 `image` 数组为准。

## 两个接口

- `POST /api/files`：multipart 字段 `file`。返回 `{ "file_code": "file_…", "file_id": "file_…", "mime_type": "image/png" }`。两个标识同值，保持已有接口兼容。
- `POST /api/verifications`：提交下方 JSON，同步等待并直接返回 `prompt.md` 规定的 `{ "target_place": "…", "claims": [...] }`。每张图不超过 10 MB，支持 PNG/JPEG/WebP/GIF；无 claim 时返回空数组。

```json
{
  "target_place": "上海迪士尼乐园",
  "text": "工作日上午不用排队",
  "link": ["https://example.com/post-a", "https://example.com/post-b"],
  "image": ["file_上传接口返回的标识"]
}
```

后端重新读取图片原始 bytes，按顺序将 `file_code` 标签与多模态图片内容送入模型；根目录 `prompt.md` 原文追加到 Agent 系统提示词。使用 browser-use 的结构化输出并检查来源标识，统一顺序编号。禁用搜索工具，按提示词只读取提交链接及其必要页面内容。登录墙或不可访问页面仍可能需要用户补充截图。

没有实现真实性核验、证据检索、报告、推荐、鉴权或任务队列。前端仅展示返回的 claim JSON。

## 主图扩展

主图使用 LangGraph 1.1.2。`VerificationService.run(VerificationInput(...))` 返回内部 `VerificationRun`，包含完整提取结果及按子图名称组织的结果；HTTP 接口继续返回原有提取 JSON。

默认注册 `fact`、`route`、`crowd`、`experience` 四个占位子图。`create_app` 或 `VerificationService` 的 `subgraphs` 参数可传入 `{名称: 编译后的子图}` 注册表，替换默认配置；若要新增一个子图，可先取得 `default_subgraphs()`，再加入新图。每个子图接收完整 `claims` 和共享 `context`，自行选择主张、拆分内部任务，并返回 `SubgraphResult`。一条主张可以被多个子图选择，也可以对应多项发现；主图不按 Claim 类型过滤。

地点解析及证据来源通过 `VerificationCapabilities` 注入 LangGraph runtime context，不写入 State。默认的 `web_search` 来源是待接入的能力占位，调用时明确抛出 `NotImplementedError`；当前不调用搜索供应商。子图异常或超时单独记录，其他子图结果仍可汇总。

流程与模块说明见 [主图设计](docs/成果/主图设计.md)。实现参考 [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) 和 [子图通信](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)。

## 检查

```bash
pip install -r backend/requirements-dev.txt
python -m pytest -q
# 可选：运行真实浏览器链路测试，模型 HTTP 响应使用本地测试替身。
VERIFY_BROWSER_TESTS=1 python -m pytest tests/test_browser.py -q
```

接口测试使用临时数据库，覆盖上传、落盘、重启恢复、图片顺序、四字段提交和错误状态。浏览器测试覆盖拖拽两张图片、无链接及多个链接、页面实际读取、系统提示词与多模态消息传递、claim 展示；如使用非默认 Chromium 路径，可设置 `VERIFY_CHROMIUM`。测试中的替身输出仅用于验证链路，不代表真实模型提取效果；正式运行始终调用配置的模型。

主图测试使用真实 LangGraph 和脚本化子图，覆盖完整 Claim 列表分发、同一主张的多项发现、新子图注册、分支数据隔离、并行汇合、异常/超时隔离、空结果短路和地点/证据依赖传递。占位子图不会生成核验结论。

第一阶段曾使用真实 DeepSeek Flash，对临时的两张文字图片、两条测试网页及文字描述完成提取，返回 5 条 claim，覆盖四种类型。重构后的回归测试通过 API 与 VerificationService 验证持久化、材料顺序、结果整理和无效引用，并保留三组真实浏览器链路测试；删除旧字段迁移与重复异常枚举测试。测试不代表对任意网页或材料的提取质量评测。

接入参考：[browser-use Agent 参数](https://docs.browser-use.com/open-source/customize/agent/all-parameters)、[结构化输出](https://docs.browser-use.com/open-source/customize/agent/output-format)、[DeepSeek 图片输入](https://api-docs.deepseek.com/zh-cn/guides/vision/)、[DeepSeek JSON mode](https://api-docs.deepseek.com/guides/json_mode/)。
