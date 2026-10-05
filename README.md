# 验一下 · 提取与事实核验

已实现「上传图片 → 提交地点、描述、多个图片和链接 → browser-use 提取 claim」，并接入 LangGraph 主图。Fact 子图执行规划、浏览器取证、判定及一次补搜；路线、客流和体验子图仍返回未实现状态。原生 HTML/JS 前端由 FastAPI 一并提供，无需 Node 构建或单独的前端服务。

## 代码分层

```text
backend/
  main.py                  # 组装依赖、初始化存储、挂载前端
  api/
    routes.py              # 文件上传与核验 HTTP 接口
    dto.py                 # HTTP 请求模型及字段校验
    error_handlers.py      # 异常到 HTTP 响应的映射及错误日志
  common/
    errors.py              # 跨模块共享的业务异常，不依赖 FastAPI
  extraction/
    agent.py               # browser-use、多模态消息、模型协议与提示词
    service.py             # 读取图片、调用 Agent、校验来源并整理 claim
    models.py              # Claim、材料来源与提取结果
  verification/
    service.py             # 运行主图，返回完整核验运行结果
    graph.py               # 初始化、提取、上下文、子图分发、汇合与结果组装
    state.py               # 主图和子图 State、并行结果合并
    models.py              # 上下文、证据、子图发现与完整运行结果
    capabilities.py        # 运行时注入地点解析、Fact 模型和搜索 Agent
    subgraphs/__init__.py   # Fact 与其他类别占位子图的注册入口
    subgraphs/facts/
      graph.py             # 两轮编排、输出校验、失败收束和结果组装
      llm.py               # 复用 load_config 的单次 OpenAI 兼容调用
      search.py            # browser-use Agent、工具预算和网页正文记录
  storage/
    repository.py          # StorageRepository：SQLite 元数据与本地文件
    models.py              # 图片标识、MIME 与原始 bytes
    schema.sql             # Storage 自己的建表脚本
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
- `POST /api/verifications`：提交下方 JSON，同步等待并返回完整 `VerificationRun`，包含 `run_id`、`context`、`claims`、`subgraph_results` 和 `status`。地点位于 `context.target_place`；无 claim 时 `claims` 为空数组，`status` 为 `no_claims`。每张图不超过 10 MB，支持 PNG/JPEG/WebP/GIF。响应结构见 [API 文档](docs/成果/api.md)。

```json
{
  "target_place": "上海迪士尼乐园",
  "text": "工作日上午不用排队",
  "link": ["https://example.com/post-a", "https://example.com/post-b"],
  "image": ["file_上传接口返回的标识"]
}
```

后端重新读取图片原始 bytes，按顺序将 `file_code` 标签与多模态图片内容送入模型；根目录 `prompt.md` 原文追加到 Agent 系统提示词。使用 browser-use 的结构化输出并检查来源标识，统一顺序编号。禁用搜索工具，按提示词只读取提交链接及其必要页面内容。登录墙或不可访问页面仍可能需要用户补充截图。

Fact 返回结构化判定、证据和缺口；报告、推荐、鉴权和任务队列尚未实现。前端展示完整运行 JSON 和执行状态；流程完成不代表主张为真。

## 主图扩展

主图使用 LangGraph 1.1.2。`VerificationService.run(VerificationInput(...))` 是完整核验的调用入口，解包主图的 `GraphOutput.result` 并返回 `VerificationRun`；HTTP 接口直接返回这一结果。`ClaimExtractionService.extract(...)` 仅负责提取，返回 `ClaimExtractionResult`。

默认注册可执行的 `fact`，以及 `route`、`crowd`、`experience` 三个占位子图。因此即使 Fact 正常完成，默认主图仍返回 `partial`。`create_app` 或 `VerificationService` 的 `subgraphs` 参数可传入 `{名称: 编译后的子图}` 注册表，替换默认配置。每个子图接收完整 `claims` 和共享 `context`，自行选择主张并返回 `SubgraphResult`；主图不按 Claim 类型过滤。

地点解析、模型和搜索通过 `VerificationCapabilities` 注入 LangGraph runtime context，不写入 State。Fact 的 Plan、Validate 每轮各执行一次 OpenAI 兼容调用，复用 `load_config()` 读取模型配置；三个节点加载各自提示词，仅 Plan 额外加载 `fact-plan` skill。`fact_llm` 和 `fact_search` 可替换为测试依赖；`fact_search=None` 表示当前没有取证能力。

Search 使用 browser-use Agent 和本地 Chromium，默认从 DuckDuckGo HTML 搜索候选，再读取页面可见正文。每条 Claim 最多两轮，每轮最多 5 次搜索/读取调用（失败也计数）、5 次查询、每次 5 个候选及 5 份新增证据；同时最多运行 3 个 Claim 的浏览器。证据 ID、抓取时间和预算由代码维护，Agent 输出须与实际工具记录一致。搜索页面被拦截或结构变化时返回取证错误；当前不支持登录、交互式翻页或图片正文提取。通用 `evidence_sources["web_search"]` 仍是供其他子图扩展的占位接口，Fact 不依赖它。

正常缺证据的 `UNVERIFIED` 属于已完成判定；技术失败单独记录，补搜失败保留旧判定和已取得材料。内部截止时间早于主图硬超时，Search 为 Validate 预留时间，浏览器在结束或取消时清理。其他子图不受 Fact 失败影响。

Fact 的诊断以 JSON 字符串追加到 `subgraph_results.fact.notes`，同时写入带 `fact_diagnostic` 前缀的后端日志；这些记录不作为证据，也不进入 Plan/Validate 的模型输入：

- `page_failure`：包含 `claim_id`、`round_number`、`tool_call`、操作、请求 URL、最终 URL、HTTP 状态、标题（最多 300 字符）、页面片段（最多 1000 字符）、加载状态、挑战页信号和异常。`navigation_ms`、`dom_read_ms`、`snapshot_ms`、`elapsed_ms` 分别记录导航、DOM 读取、现场采集和整次工具调用耗时。
- HTTP 状态取自浏览器主文档的 Navigation Timing，不额外请求网页；浏览器未提供时为 `null`，不能按 200 处理。导航失败且无法确认新文档已加载时，页面字段保留 `null`，避免记录上一页的状态。现场采集失败会保留 `snapshot_error`，不覆盖原始异常。
- `category` 根据可观察信号分类：`rate_limited`（429）、`challenge`（验证表单或人机验证文本）、`access_denied`（403，不单独认定反爬）、`http_error`（其他 HTTP 错误）、`load_incomplete`（未识别到搜索结果且文档仍在加载）、`parse_error`（文档加载完成但未匹配结果/空结果标记，可能布局变化）。导航及 DOM 异常另记 `load_error`/`dom_error`、`*_timeout` 或 `*_cancelled`；信号不足为 `unknown`。正常无结果不报错。已识别的 HTTP 错误页和挑战页不作为正文证据保存。
- `stage_timing`：记录 `plan`、`search`、`validate`、每条主张的 `search_claim`、每次 `search_llm` 和 `browser_cleanup` 耗时与执行结果。`search_claim.queue_ms` 为并发槽位等待时间，`remaining_ms` 为开始执行时的剩余时间；耗时单位均为毫秒，嵌套阶段不可直接相加。

流程与模块说明见 [主图设计](docs/成果/主图设计.md)。实现参考 [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) 和 [子图通信](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)。

## 检查

```bash
pip install -r backend/requirements-dev.txt
python -m pytest -q
# 可选：运行真实浏览器链路测试，模型 HTTP 响应使用本地测试替身。
VERIFY_BROWSER_TESTS=1 python -m pytest tests/test_browser.py tests/test_fact_browser.py -q
```

接口测试使用临时数据库，覆盖上传、落盘、重启恢复、图片顺序、四字段提交、错误状态，以及完整运行结果中的发现、证据和子图失败信息。浏览器测试覆盖拖拽两张图片、无链接及多个链接、页面实际读取、系统提示词与多模态消息传递、完整运行结果和状态展示；如使用非默认 Chromium 路径，可设置 `VERIFY_CHROMIUM`。测试中的替身输出仅用于验证链路，不代表真实模型提取效果；正式运行始终调用配置的模型。

主图测试使用真实 LangGraph 和脚本化子图，覆盖完整 Claim 列表分发、同一主张的多项发现、新子图注册、分支数据隔离、并行汇合、异常/超时隔离、空结果短路和地点/证据依赖传递。占位子图不会生成核验结论。

Fact 测试覆盖步骤指令注入、两轮状态累积、范围和引用校验、预算限制、无结果与失败收束。浏览器集成测试使用本地候选页、公告正文和 OpenAI 兼容 HTTP 替身，验证实际搜索/读取动作及普通模型、DeepSeek 两种消息协议；不代表真实模型判定质量或公网搜索可用性评测。

第一阶段曾使用真实 DeepSeek Flash，对临时的两张文字图片、两条测试网页及文字描述完成提取，返回 5 条 claim，覆盖四种类型。重构后的回归测试通过 API 与 VerificationService 验证持久化、材料顺序、结果整理和无效引用，并保留三组真实浏览器链路测试；删除旧字段迁移与重复异常枚举测试。测试不代表对任意网页或材料的提取质量评测。

接入参考：[browser-use Agent 参数](https://docs.browser-use.com/open-source/customize/agent/all-parameters)、[结构化输出](https://docs.browser-use.com/open-source/customize/agent/output-format)、[DeepSeek 图片输入](https://api-docs.deepseek.com/zh-cn/guides/vision/)、[DeepSeek JSON mode](https://api-docs.deepseek.com/guides/json_mode/)。
