# 验一下 · 提取与事实核验

已实现「上传图片 → 提交地点、描述、多个图片和链接 → browser-use 提取 claim」，并接入 LangGraph 主图。四类主张各有可执行子图：`fact` 规划、浏览器取证、判定及一次补搜；`route` 用地图实测比对时长与距离；`crowd` 在星期、节假日、时段、季节条件下核验拥挤度；`experience` 判断多个来源的体验是否一致。原生 HTML/JS 前端由 FastAPI 一并提供，无需 Node 构建或单独的前端服务。

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
    graph.py               # 初始化、提取、上下文、子图分发、汇合、冲突检测与结果组装
    state.py               # 主图和子图 State、并行结果合并
    models.py              # 四类判定模型、上下文、证据、子图结果与完整运行结果
    budget.py              # 运行级取证预算：跨子图共享的浏览器槽位与总截止时间
    capabilities.py        # 运行时注入地点解析、地图路线、模型调用、搜索与证据来源
    skeleton.py            # 四类共用的 Plan → Search → Validate 编排骨架
    subgraphs/__init__.py   # 四类子图的注册入口
    subgraphs/facts/       # 事实类：真假判定、浏览器取证
    subgraphs/route/       # 路线类：数值比对、地图实测
    subgraphs/crowd/       # 客流类：场景条件对齐、拥挤度判定
    subgraphs/experience/  # 体验类：来源一致性判定
  sources/
    nominatim.py           # 地点解析实现（POI → 坐标）
    valhalla.py            # 地图路线实现（坐标 → 时长/距离/可达圈）
    crowd_signal.py        # 客流信号接口，未接入数据供应商时明确报错
  storage/
    repository.py          # StorageRepository：SQLite 元数据与本地文件
    models.py              # 图片标识、MIME 与原始 bytes
    schema.sql             # Storage 自己的建表脚本
```

上传接口直接调用 Storage Repository；评估接口调用 VerificationService，由主图的提取节点调用 ClaimExtractionService。提取模块通过注入的 Repository 取图，再调用注入的 Agent 提取函数，并保留来源校验与顺序编号规则。Agent 接收地点、描述、链接和图片，不依赖 HTTP 请求 DTO。

## 启动

在仓库根目录执行。依赖由 uv 管理（`pyproject.toml` 声明，`uv.lock` 锁定），`requires-python >= 3.12`：

```bash
uv sync                                          # 建 .venv 并装依赖，含 dev 组
uv run playwright install chromium --no-shell    # 本地 Chromium
cp -n backend/config.example.toml backend/config.toml  # 保留已有配置
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 。`uv sync` 默认包含 dev 依赖组；只要运行时依赖用 `uv sync --no-dev`。
上面的 Playwright 命令安装本地 Chromium；Linux 如果缺少浏览器系统库，执行 `uv run playwright install-deps chromium`。已有 Chrome/Chromium 时可以跳过浏览器安装，在配置中指定 `browser_executable_path`。

在 `backend/config.toml` 填写 `api_key`，按供应商设置 `base_url` 和 `model`。支持提供图片输入及 JSON Schema 输出的 OpenAI 兼容模型，也兼容 `deepseek-flash`（使用 JSON mode 后本地校验结构，并修正 browser-use 0.13.10 自动关闭 DeepSeek 视觉的旧判断）。`config.example.toml` 中密钥留空；实际配置文件已被 Git 忽略。程序依次读取 `config.local.toml`、`config.toml`、`config.example.toml` 中首个存在的文件。未配置密钥时可以上传图片，评估返回明确的 503 提示。

后端启动时按 `backend/storage/schema.sql` 自动创建 `backend/data/files.sqlite3`，图片保存在 `backend/data/uploads/`。SQLite 是嵌入式数据库，无需单独启动数据库进程。当前为本地单会话骨架，`order` 按上传递增；传入模型的图片顺序始终以本次 `image` 数组为准。

## 两个接口

- `POST /api/files`：multipart 字段 `file`。返回 `{ "file_code": "file_…", "file_id": "file_…", "mime_type": "image/png" }`。两个标识同值，保持已有接口兼容。
- `POST /api/verifications`：提交下方 JSON，同步等待并返回完整 `VerificationRun`，包含 `run_id`、`context`、`claims`、`subgraph_results` 和 `status`。地点位于 `context.target_place`；无 claim 时 `claims` 为空数组，`status` 为 `no_claims`。每张图不超过 10 MB，支持 PNG/JPEG/WebP/GIF。响应结构见 [API 文档](docs/成果/api.md)。

```json
{
  "target_place": "上海迪士尼乐园",
  "text": "工作日上午不用排队",
  "link": ["https://www.xiaohongshu.com/explore/0123456789abcdef01234567", "http://xhslink.com/o/example"],
  "image": ["file_上传接口返回的标识"]
}
```

后端重新读取上传图片原始 bytes，按顺序将 `file_code` 标签与多模态图片送入模型。链接仅支持小红书图文笔记：复用专用登录会话读取标题、正文和全部轮播配图，再将材料交给同一提取模型；提取 Agent 不再自行访问网页。根目录 `prompt.md` 追加到系统提示词，并检查来源标识、统一顺序编号。笔记配图来源为 `LINK`，对应提交的原始链接，原笔记不能成为后续独立佐证。

页面可直接粘贴完整分享文案，前端提取其中的 URL；API 的 `link` 仍是纯 URL 数组。支持 `xiaohongshu.com`/`www.xiaohongshu.com` 的 `/explore/{24位ID}`、`/discovery/item/{ID}`、`/search_result/{ID}`，以及 `xhslink.com` 分享短链。以上示例仅演示格式，需要替换成可访问的真实笔记。用户主页、搜索列表、其他网站和视频笔记不支持。链接失败会显示条目序号和原因，不会静默跳过配图；详见 [小红书链接图文核验](docs/XIAOHONGSHU_LINK_MATERIALS.md)。

地点仅限定核验范围。前端要求文字、链接、图片至少提供一种，空材料会在发请求前提示；有材料但没有明确说法时仍可返回 `no_claims`。HTTP API 保留仅地点请求返回 `no_claims` 的兼容行为，不会凭地点名称主动补造主张。

DeepSeek JSON mode 返回的字符串裸换行等控制字符会规范化后再做原有 Schema 校验；截断响应、无效 JSON 结构和错误字段仍拒绝。故障原因与验收见 [空材料与模型响应交接](docs/EMPTY_INPUT_AND_MODEL_JSON.md)。

四类判定语义互不相同，不能用同一套真假词表表达：

| 类别 | 判定语义 | 词表 |
| --- | --- | --- |
| `fact` | 真假 | `SUPPORTED` / `CONTRADICTED` / `CONDITIONAL` / `UNVERIFIED` |
| `route` | 数值比对 | `MATCHED` / `MISMATCHED` / `CONDITION_MISMATCH` / `UNVERIFIED` |
| `crowd` | 场景条件 | `SUPPORTED` / `NOT_SUPPORTED` / `SCENARIO_ONLY` / `UNVERIFIED` |
| `experience` | 体验一致性 | `CONSISTENT` / `DIVERGENT` / `SCENARIO_DEPENDENT` / `UNVERIFIED` |

`route` 的判定同时携带主张时长、实测时长、交通方式、距离和容差，便于直接对比。四类共用 `UNVERIFIED` 表达证据不足；证据不足时不得给出确定结论，这是代码强制的约束，不依赖提示词。

同一条主张可能被多个子图选中并给出多项发现。结论互斥时，运行结果的 `conflicts` 字段列出矛盾双方与各自依据；系统只陈述矛盾，不替用户裁决哪条对。报告、推荐、鉴权和任务队列尚未实现。前端按「结论 → 理由 → 证据」三级展示完整运行结果和执行状态；流程完成不代表主张为真。

## 实时小红书来源

后端通过已有 `EvidenceSource` 接口注册 `XiaohongshuSource`。每次 Fact `search_web(query)` 使用相同关键词并行执行网页搜索和小红书实时搜索，默认最多读取十条笔记原正文，直接登记 `WEB` 证据。网页候选继续由 `read_page` 读取；不需要 GUI、Excel或提前采集 JSONL。

在 `backend/config.toml` 的 `[xiaohongshu]` 表设置 `max_results = 10`（整数 1–10）、`timeout_seconds = 120`、`pacing_seconds = 4`。默认使用系统 Edge 和专用 `backend/data/xiaohongshu-profile/`，可通过 `executable_path`、`profile_path` 指定路径。数量设置重启后端后生效。

首次登录或登录失效时，先关闭使用该资料目录的后端，再运行：

```powershell
.\.venv\Scripts\python.exe -m backend.sources.xiaohongshu --login
```

在打开的浏览器中手动登录，按命令提示结束后启动后端。正常核验时浏览器隐藏；验证码、登录失效、限流或超时会明确记录，已取得材料保留，网页核验继续。Windows 使用单 worker、无 `--reload`。登录命令和启动页面不需要模型 Key，完整核验仍须配置模型。

来源模块也可直接调用；以下实例使用同一后端配置，结束时释放浏览器：

```python
from pathlib import Path
from backend.extraction.agent import read_config
from backend.sources.xiaohongshu import XiaohongshuSource

async def collect(query):
    root = Path.cwd()
    source = XiaohongshuSource.from_config(read_config(), root=root, data_directory=root / "backend/data")
    try:
        return await source.search(query)
    finally:
        await source.aclose()
```

正式 Fact 调用额外注入预算回调与输入排除，每次查询和正文读取分别计数，输入笔记不能成为自身的外部证据。十条是目标上限，实际数量受结果、预算、访问限制和时间影响；数量不直接提高可信度评分。

所有累计改动、完整文件清单、最新验收与推送步骤见 [完整改动报告](docs/CRAWLER_CHANGE_REPORT.md)。启动、回退和测试结果见 [实时爬虫交接](docs/CRAWLER_HANDOFF.md)。[实施方案](docs/CRAWLER_INTEGRATION_PLAN.md) 明确要求本地验收通过后才能推送。

## 主图扩展

主图使用 LangGraph 1.1.2。`VerificationService.run(VerificationInput(...))` 是完整核验的调用入口，解包主图的 `GraphOutput.result` 并返回 `VerificationRun`；HTTP 接口直接返回这一结果。`ClaimExtractionService.extract(...)` 仅负责提取，返回 `ClaimExtractionResult`。

默认注册四类可执行子图 `fact`、`route`、`crowd`、`experience`，各自主动选择主张并返回 `SubgraphResult`。`create_app` 或 `VerificationService` 的 `subgraphs` 参数可传入 `{名称: 编译后的子图}` 注册表，替换默认配置。每个子图接收完整 `claims` 和共享 `context`，主图不按 Claim 类型过滤——因此「地铁步行 5 分钟」可能同时被 `fact` 和 `route` 选中，两者的矛盾由 `conflicts` 显式标出。

地点解析、地图路线、模型和搜索通过 `VerificationCapabilities` 注入 LangGraph runtime context，不写入 State。各类子图的 Plan、Validate 每轮各执行一次 OpenAI 兼容调用，复用 `load_config()` 读取模型配置；三个节点加载各自提示词，仅 Fact 的 Plan 额外加载 `fact-plan` skill。`llm` 和 `search` 可替换为测试依赖；`search=None` 表示本次运行没有该类别的取证能力，子图据此记为缺证据而非失败。`map_routing` 或 `place_resolver` 为 `None` 时，`route` 同样只能给「证据不足」，不得猜测坐标或折算时长。

Search 使用 browser-use Agent 和本地 Chromium，通过 DuckDuckGo HTML 搜索网页候选，并自动并行调用 `evidence_sources["xiaohongshu"]` 实时读取笔记。每条 Claim 最多两轮，每轮最多 20 次搜索/读取调用（失败也计数）、5 次查询、每次最多 10 个候选及 15 份新增证据。所有类别共享 2 个 Claim 网页浏览器槽位，小红书专用会话串行访问。证据 ID、抓取时间和预算由代码维护，Agent 输出须与实际工具记录一致。一次核验共享 240 秒运行预算，图片提取和地点解析消耗的时间也计入；子图单独调用时默认上限为 300 秒，经主图调用时只能使用运行预算的剩余量。小红书单次调用默认 120 秒且受剩余截止时间限制。通用 `evidence_sources["web_search"]` 仍为占位；实际网页搜索保留原实现，小红书通过已有来源注册表接入。网页读取不支持交互式登录或图片正文，小红书会话通过单独登录命令维护。

正常缺证据的 `UNVERIFIED` 属于已完成判定；技术失败单独记录，补搜失败保留旧判定和已取得材料。内部截止时间依据共享预算计算，早于主图硬超时；Search 的排队和执行一起计时，为 Validate 预留时间。排队或取证超时会保留已完成主张和已取得材料，并明确标注未完成项。浏览器在结束或取消时清理，其他子图不受 Fact 失败影响。图片超时的原因与回归说明见 [图片核验超时修复](docs/IMAGE_SEARCH_TIMEOUT.md)。

Fact 的诊断以 JSON 字符串追加到 `subgraph_results.fact.notes`，同时写入带 `fact_diagnostic` 前缀的后端日志；这些记录不作为证据，也不进入 Plan/Validate 的模型输入：

- `page_failure`：包含 `claim_id`、`round_number`、`tool_call`、操作、请求 URL、最终 URL、HTTP 状态、标题（最多 300 字符）、页面片段（最多 1000 字符）、加载状态、挑战页信号和异常。`navigation_ms`、`dom_read_ms`、`snapshot_ms`、`elapsed_ms` 分别记录导航、DOM 读取、现场采集和整次工具调用耗时。
- HTTP 状态取自浏览器主文档的 Navigation Timing，不额外请求网页；浏览器未提供时为 `null`，不能按 200 处理。导航失败且无法确认新文档已加载时，页面字段保留 `null`，避免记录上一页的状态。现场采集失败会保留 `snapshot_error`，不覆盖原始异常。
- `category` 根据可观察信号分类：`rate_limited`（429）、`challenge`（验证表单或人机验证文本）、`access_denied`（403，不单独认定反爬）、`http_error`（其他 HTTP 错误）、`load_incomplete`（未识别到搜索结果且文档仍在加载）、`parse_error`（文档加载完成但未匹配结果/空结果标记，可能布局变化）。导航及 DOM 异常另记 `load_error`/`dom_error`、`*_timeout` 或 `*_cancelled`；信号不足为 `unknown`。正常无结果不报错。已识别的 HTTP 错误页和挑战页不作为正文证据保存。
- `stage_timing`：记录 `plan`、`search`、`validate`、每条主张的 `search_claim`、每次 `search_llm` 和 `browser_cleanup` 耗时与执行结果。`search_claim.elapsed_ms` 包含排队和执行，`queue_ms` 为并发槽位等待时间，`remaining_ms` 为取得槽位时的剩余时间；若排队即超时，则保留开始排队时的剩余量。耗时单位均为毫秒，嵌套阶段不可直接相加。

流程与模块说明见 [主图设计](docs/成果/主图设计.md)。实现参考 [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) 和 [子图通信](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)。

## 检查

```bash
uv sync                    # dev 依赖组随默认同步装入
uv run python -m pytest -q
# 可选：运行真实浏览器链路测试，模型 HTTP 响应使用本地测试替身。
VERIFY_BROWSER_TESTS=1 uv run python -m pytest tests/test_browser.py tests/test_fact_browser.py tests/test_xiaohongshu_source_browser.py -q
```

接口测试使用临时数据库，覆盖上传、落盘、重启恢复、图片顺序、四字段提交、错误状态，以及完整运行结果中的发现、证据和子图失败信息。浏览器测试覆盖拖拽两张图片、无链接及多个链接、页面实际读取、系统提示词与多模态消息传递、完整运行结果和状态展示；如使用非默认 Chromium 路径，可设置 `VERIFY_CHROMIUM`。测试中的替身输出仅用于验证链路，不代表真实模型提取效果；正式运行始终调用配置的模型。

主图测试使用真实 LangGraph 和脚本化子图，覆盖完整 Claim 列表分发、同一主张的多项发现、新子图注册、分支数据隔离、并行汇合、异常/超时隔离、空结果短路、地点/证据依赖传递，以及同一主张结论互斥时的冲突标记。

Fact 测试覆盖步骤指令注入、两轮状态累积、范围和引用校验、预算限制、无结果与失败收束。浏览器集成测试使用本地候选页、公告正文和 OpenAI 兼容 HTTP 替身，验证实际搜索/读取动作及普通模型、DeepSeek 两种消息协议；不代表真实模型判定质量或公网搜索可用性评测。

第一阶段曾使用真实 DeepSeek Flash，对临时的两张文字图片、两条测试网页及文字描述完成提取，返回 5 条 claim，覆盖四种类型。重构后的回归测试通过 API 与 VerificationService 验证持久化、材料顺序、结果整理和无效引用，并保留三组真实浏览器链路测试；删除旧字段迁移与重复异常枚举测试。测试不代表对任意网页或材料的提取质量评测。

接入参考：[browser-use Agent 参数](https://docs.browser-use.com/open-source/customize/agent/all-parameters)、[结构化输出](https://docs.browser-use.com/open-source/customize/agent/output-format)、[DeepSeek 图片输入](https://api-docs.deepseek.com/zh-cn/guides/vision/)、[DeepSeek JSON mode](https://api-docs.deepseek.com/guides/json_mode/)。

## 地图能力与演示阶段限制

`route` 的判定依赖两个能力槽位，开发期由 OpenStreetMap 生态的公共服务提供：

| 能力 | 槽位 | 实现 | 用途 |
| --- | --- | --- | --- |
| 地点解析 | `place_resolver` | `sources/nominatim.py` | 把 `TARGET_PLACE` 文字解析为 POI 坐标 |
| 路线测量 | `map_routing` | `sources/valhalla.py` | 单点路线、多点矩阵、步行可达圈 |

两者都遵守服务方使用条款：路由每秒最多一次请求、脚本限单连接、必须发送可识别本应用的 User-Agent、服务地址可配置而不硬编码进应用。判定「步行 N 分钟」只用路线时长或等时圈，**不使用直线距离折算**——折算会把夸大的步行时长误判为成立。

**这两项是演示阶段的临时实现，不是长期选择。** 公共服务的条款不允许商用产品把它们作为重要组成部分。接口按「坐标进、时间/距离出」设计，与供应商无关，上线时替换实现不动子图。

`crowd` 的客流信号同样受限：演示阶段没有真实客流或交易数据，`sources/crowd_signal.py` 提供了从近期公开评价密度估计拥挤度的接口，但缺少公开评价数据供应商时会明确报错而不是编造一个拥挤度。当前 `crowd` 的判定依据是网页与小红书的近期材料加场景条件对齐，结论中标注了场景与证据的时间覆盖。

场景条件的对齐基准取进程当前时刻而非 `checked_at`，两者在一次运行内的差异可忽略；证据的时效上限为两年，早于该阈值的材料只能作背景。
