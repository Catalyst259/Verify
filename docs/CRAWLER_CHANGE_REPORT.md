# 小红书实时爬虫接入：完整改动与推送交接

整理日期：2026-10-06，Asia/Shanghai。本报告汇总当前功能分支相对于上游 main 的全部实现改动，供代码审查、部署及后续 agent 接手使用。

## 1. 仓库、基线与提交

| 项目 | 当前值 |
| --- | --- |
| 原仓库 / origin | `https://github.com/Catalyst259/Verify.git` |
| 功能分支 | `feat/xiaohongshu-crawler` |
| 本轮重新 fetch 确认的 main | `55c22d0e39689fb88215ce62f118195302336098` |
| 实现代码版本 | `abe7433dd138f7c4b8377246d5f352f3409d6af8` |
| 写本报告前的差异 | 领先 main 4 个提交、落后 0 个；工作区干净；46 个文件发生变化 |
| 推送状态 | 等待用户申请原仓库写权限及完成本地验收；本轮只准备提交，没有执行 push |

实现提交依次为：

| 提交 | 内容 |
| --- | --- |
| `12bbd5250fe2eef2f2b786a2db6111b0ec5e22d1` | 原生实时小红书来源、默认十条、共享预算、排除输入及测试 |
| `534c47b227716bff2684014b981e34df4db9d0e1` | 空材料引导、DeepSeek JSON 兼容、Search 账本结束与有限纠错 |
| `fa908842c2461581e37ecf2751c5b6448ede1794` | 搜索前初始化首页、导航错误诊断、执行状态说明 |
| `abe7433dd138f7c4b8377246d5f352f3409d6af8` | 可读结论、理由、引用来源、证据编号、折叠原文与结果定位 |

本报告及入口链接单独保存为文档提交。准备完成后的最终 HEAD 和推送结果记录在仓库外 `../handoff/online/push-preparation.json`；不要把上表的实现版本误当成后续文档提交的 SHA。

## 2. 回退与原件保留

早期的 GUI / Excel / 离线 JSONL 接入已经撤出当前分支。旧提交 `8c2107eece456425b5761606f16b238f7abcfca6` 及完整旧仓库保留在本机工作区的 `../handoff/rollback/20261006-161019/Verify/`，不在这条功能分支的提交链中。

原爬虫目录保持原样；原源码 12 项哈希及归档配置、14 条采集记录、数据库的哈希核对均已留档。登录资料使用专用副本，另有本机完整备份。历史配置、数据、虚拟环境和日志没有因回退被删除。

新的后端运行链路不启动 GUI、不生成 Excel、不读取预采集 JSONL，也不使用旧转换 CLI。公开源码出处见 [CRAWLER_SOURCE_HASHES.json](CRAWLER_SOURCE_HASHES.json)；私有回退材料只存本机，不能整体作为公开交接包上传。

## 3. 原生来源与取证链路

`XiaohongshuSource.search(query) → list[Evidence]` 兼容仓库已有 `EvidenceSource` 接口。`create_app` 默认构造一个来源实例，并注册到 `VerificationCapabilities.evidence_sources["xiaohongshu"]`；显式注入 capabilities 时由调用方提供来源，应用不擅自替换它。

实际执行顺序：

1. 用户提交文字、图片或链接，提取器生成原始主张；地点只限定范围。
2. Fact Plan 自行选择可核验内容并生成计划，主图没有按 Claim.type 硬过滤。
3. Search 的每次 `search_web(query)` 用相同关键词并行搜索网页和小红书。两来源分别记录查询次数及候选数量。
4. 网页搜索返回候选，仍须 `read_page` 读取正文。小红书自动进入每篇详情、读取可见正文，默认最多十篇。
5. 两类正文都通过同一个 `SearchSession.execute` 登记，生成运行内证据 ID、计数、去重和输入排除。
6. Validate 只引用实际登记的材料，返回判定、理由、条件和剩余缺口；前端直接展示这些结果。

来源复用原爬虫的公开 DOM 搜索、正文提取和访问限制识别，改为异步 Playwright。没有使用私有 API、隐藏应用状态、验证码绕过或代理轮换。

### 数据契约

| 字段 | 行为 |
| --- | --- |
| `source_type` | 小红书固定为 `WEB`，不冒充官方来源 |
| `source` | 保留实际作者 / 来源标识 |
| `content` | 标题及笔记可见正文，保留原文；候选摘要不替代正文 |
| `url` | 公开输出使用净化链接；详情签名参数只留在来源本次调用内存 |
| `evidence_id` | 由代码生成，运行内唯一；模型不能生成或改写 |
| `retrieved_at` | 实际正文读取时间，必须带时区；登记时保留该时间 |
| `published_at` | 仅保留可靠日期 / 时间；不能可靠解析时为 `null`，不推算相对日期 |

HTTP 请求仍是 `target_place`、`text`、`link`、`image` 四字段，响应仍使用 `VerificationRun`。没有增加结果数据库、按 run_id 读取的 GET 接口或额外任务协议。

### 预算、并发与输入排除

| 项目 | 默认值 / 约束 |
| --- | --- |
| 每次小红书查询正文目标上限 | 10，配置允许整数 1–10 |
| 每来源每次查询候选上限 | 10；两来源分别计数，不合并后再截断 |
| 每条主张每轮工具调用 | 20；每次来源查询和每篇正文读取均计数，失败也计数 |
| 每条主张每轮查询 | 5；网页、小红书分别消耗一次查询额度 |
| 每条主张每轮新增证据 | 15，最多两轮累计 30 份 |
| 核验轮数 | 最多 2 轮，只有实际缺证据且仍有时间、能力时补搜 |
| 单次小红书来源超时 | 120 秒，包含等待锁，并受 Fact 剩余截止时间限制 |
| Fact / 主图外层子图超时 | 默认 300 秒；内部为 Validate、组装和清理预留时间 |
| 网页主张并发 | 最多 3 个主张；小红书专用会话通过异步锁串行访问 |

爬虫读取到只剩一次调用或一个证据槽位时停止，为网页正文保留余量。并行查询更新各自的计数槽，正常无结果、重复、预算用尽和失败都如实返回，不补造十条。

每个请求独立传递 `input_urls`，每条主张维护独立预算和证据账本。统一登记处按小红书笔记 ID 排除原输入，覆盖等价路径、默认端口和网页重定向后的最终链接，防止输入笔记自证。同 URL / 同正文去重；同 URL 的新正文可作为不同版本保存。

无可识别笔记 ID 的短链接，以及只粘贴文字的输入，不能仅靠 URL 身份可靠识别同一原帖，这一限制仍存在。十条是采集目标上限，不是可信度保证；可信度继续依据来源权威性、独立性、时效和交叉核对判断。

## 4. 后续故障修复

### 空材料与模型 JSON

- 前端要求文字、图片、链接至少提供一种，空材料在发请求前提示；地点输入旁说明其用途。
- 提取器不凭地点名称主动搜索或补造主张。有材料但没有明确说法时返回 `no_claims`；仅地点的 HTTP 调用仍保留 200 / `no_claims` 兼容行为。
- DeepSeek 字符串中的裸换行、回车和制表符先规范化，再执行原有严格 Schema 校验；有效 JSON 不改写。
- JSON 结构损坏、碎片、NaN / Infinity、截断、空内容或错误字段仍拒绝；不从说明文字中猜取结果。
- 配置读取与实际模型校验分离：无模型 Key 也能启动页面和手动登录；真正提取与判断仍要求有效模型配置。

### Search 结束与纠错

原生 Search Agent 的 `done` 只接受空对象 `{}`，由代码返回本轮实际工具账本。模型不再重新拼写所有材料，不能偷偷遗漏、替换或编造正文和证据 ID。可注入的 SearchResult 路径仍严格核对主张、证据和真实错误。

模型动作必须存在且有效；缺少 action 不执行工具。最多连续两次协议失败，允许一次纠正机会，Agent 最多 21 步；真实取证额度及截止时间没有放宽为无限重试。超时或协议失败保留已经登记的材料。

### 登录会话冷搜索与导航诊断

同一真实专用会话直接进入搜索曾稳定遇到约 25 秒 DOM 导航超时。恢复原爬虫的首页初始化后，先访问 HOME 并检查限制，再进入关键词搜索；主页推荐卡片不作为搜索候选或证据，不额外增加查询计数。

来源识别登录墙、验证码、限流、HTTP 错误、页面变化和超时。错笔记 ID、缺正文、未稳定详情或错误文档不登记；部分成功材料保留，网页取证继续。取消传播，当前页面及锁正确释放，服务关闭释放浏览器和驱动。

新增导航诊断仅输出固定页面类别、主机、异常类、耗时及经过格式 / 白名单校验的 Chromium 网络码，不输出签名 URL、查询参数、profile 路径或异常原文。通用网页现场诊断仍用于解释网站限制，私有运行日志不上传。

## 5. 前端结果展示

- 状态文字区分完成、部分完成、失败、跳过、尚未实现和未提取到主张；`UNVERIFIED` 是未能确认，不把它误写成主张为假。
- 每条结果显示原始主张、四种真实判定、理由、核验范围、条件、缺口及错误；按 claim_id 匹配，判定理由不被当成主张标题。
- 使用产生该 finding 的真实子图状态，避免非 FACT 主张由 Fact 处理时拿错占位子图状态。
- 内部证据 ID 在理由中显示为对应材料编号，原 JSON 及引用列表不变。
- 根据实际引用区分支持、反证、背景和未引用。引用来源默认展开，正文及未引用材料按需展开；显示来源链接及真实时间。
- 完整 JSON 默认折叠；完成后自动滚动并聚焦结果区，第二次提交替换旧结果。
- 模型和网页内容使用 `textContent`，链接只允许 HTTP / HTTPS，新窗口使用 `noopener noreferrer`。

已打开的页面需要刷新加载新脚本。刷新会清空页面旧响应；它尚未持久化。本机接收到的用户原响应已留档，展示回放没有再次调用模型或爬虫。

## 6. 当前明确未实现的功能

默认注册表中只有 Fact 是实际核验子图；`route`、`crowd`、`experience` 仍是占位。因此 Fact 完成时，整体仍可能是 `partial`，原因由前端如实说明。

纯主观口味、好玩程度等可能被 Fact Plan 跳过。例：“西湖醋鱼很好吃”提取为 EXPERIENCE，Fact 没有选中，体验占位也不执行搜索，因此这次不会调用爬虫。这个路径已解释并按用户要求停止修改，没有在后续提交中实现体验核验，也没有把主观偏好强行当成客观事实。

通用 `evidence_sources["web_search"]` 仍是供应商占位；实际网页搜索使用既有 browser-use 链路。地点解析未接入时 `resolved_place=null`，不能表示已确认具体 POI。推荐、报告后端、鉴权及任务队列也不是此次实现范围。

## 7. 验收证据与边界

### 本轮推送准备：当前实现版本

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 完整离线后端测试 | 197 passed、36 opt-in skipped，10.79 秒 | `../handoff/online/prepush-backend-20261006-210624.log` |
| 全部本地浏览器用例 | 36 passed，92.32 秒；无失败 | `../handoff/online/prepush-browser-20261006-210740.log` |
| 依赖一致性 | `pip check` 通过 | 本轮推送准备回执 |
| 独立改动审查 | 确认 4 提交 / 46 实现文件，核对预算、语义、安全及历史边界 | 本轮推送准备回执 |

本地浏览器检查覆盖上传与多模态输入、GPT / DeepSeek 协议、Fact 真实工具调用、十篇小红书合成页面正文、错误 HTTP / 取消清理、空材料、执行状态与结果卡片。页面和模型协议使用本地替身，不访问真实登录账号、不等价于真实模型质量验收。

### 已留档的真实与历史检查

| 阶段 | 记录 | 能证明什么 |
| --- | --- | --- |
| 最终来源修复后 | 真实来源返回 10 条正文，59.53 秒 | 单个真实关键词和会话的正文 / 链接 / 读取时间契约，未调用模型 |
| 最终真实 API | DeepSeek Flash + 爬虫链路 HTTP 200；Fact completed；累计 13 条不同笔记链接的材料；119.535 秒 | 多次查询可累计超过单次十条，最终引用属于真实账本；模型当次返回 SUPPORTED，不是通用正确率保证 |
| 用户响应展示回放 | 已有 10 条材料，实际引用 5 条；无页面错误；原 JSON 完整 | 当前前端可读展示、编号、引用分类和定位；没有新增模型 / 爬虫调用 |
| 早期集成 | 158 项后端、11 项浏览器 | 初版历史快照，见 CRAWLER_VERIFICATION.json |
| 导航修复 | 197 项后端、29 项 opt-in 跳过 | fa90884 阶段历史结果，不代表现在新增的全部前端用例 |
| 可读结果修复 | 25 项前端浏览器通过，最后编号改动的 7 项再次通过 | abe7433 实现阶段的结果展示检查 |

不同证据 ID 和不同链接只证明记录不重复，不代表作者或信息源相互独立；真实来源独立性没有由上述契约审计证明。

旧零证据结果、模型 JSON 失败、导航超时、测试失败均保留。曾出现一次 Windows Python 原生 access violation，未定位；同代码重新运行通过，不能声称该系统异常已修复。DuckDuckGo 可能遇到人机验证，小红书可能失效 / 限流；来源失败不被伪装成证据或主张真假。

`CRAWLER_VERIFICATION.json` 中“模型 Key 为空”等字段是初版快照。本机后续已配置并验证 DeepSeek Flash，Key 只在忽略的本地配置中，不能把旧快照误读为当前配置。真实判断质量与用户正在进行的手动验收仍单独记录，不叠加历史测试次数制造总成绩。

## 8. 环境、启动和复验

Playwright 从开发依赖移到生产依赖；开发依赖继续引用生产依赖，不重复声明。使用 Python 3.12+，本机正常来源使用系统 Edge，合成浏览器测试使用系统 Chrome。

配置按 `backend/config.local.toml`、`config.toml`、`config.example.toml` 的优先级读取首个存在文件。本地配置与 Key 不提交。`[xiaohongshu]` 默认 `max_results=10`、`timeout_seconds=120`、`pacing_seconds=4`；来源参数变化需要重启，模型参数每次调用读取。

```powershell
# 在仓库根目录，已有配置不要覆盖。
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check

# 先停止同一专用 profile 的后端，再手动登录；结束登录后启动服务。
.\.venv\Scripts\python.exe -m backend.sources.xiaohongshu --login
$env:PYTHONUTF8 = '1'
$env:ANONYMIZED_TELEMETRY = 'false'
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Windows 运行单 worker，不用 reload，不并行打开同一 profile。页面为 `http://127.0.0.1:8000/`。本机最近启动于 2026-10-06 20:58:31+08；最新 PID 与日志在 `../handoff/online/backend-startup.json`，接手必须重新核对，不能复用历史 PID。

```powershell
$env:PYTHONUTF8 = '1'
Remove-Item Env:VERIFY_BROWSER_TESTS -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m pytest -q

$env:VERIFY_BROWSER_TESTS = '1'
$env:VERIFY_CHROMIUM = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
$env:VERIFY_TEST_BROWSER_PATH = $env:VERIFY_CHROMIUM
.\.venv\Scripts\python.exe -m pytest tests/test_browser.py tests/test_fact_browser.py tests/test_xiaohongshu_source_browser.py tests/test_frontend_input.py tests/test_frontend_status.py tests/test_frontend_results.py -q
```

## 9. 全部实现文件清单

下表完整覆盖 `55c22d0..abe7433` 的 46 个变更文件。本报告是新增汇总文档，README 额外增加其入口；功能代码没有在本次准备中修改。

| 文件 | 变化内容 |
| --- | --- |
| `README.md` | 实时来源、配置、预算、运行、空材料、限制与交接入口 |
| `backend/common/model_json.py` | 严格 JSON 控制字符规范化 |
| `backend/config.example.toml` | 可分享的小红书配置模板 |
| `backend/extraction/agent.py` | 分离配置读取 / 模型校验，DeepSeek 完整 JSON 检查 |
| `backend/main.py` | 默认装配来源，应用退出时清理 |
| `backend/requirements.txt` | Playwright 生产依赖 |
| `backend/requirements-dev.txt` | 移除重复开发依赖声明 |
| `backend/sources/__init__.py` | 来源模块包入口 |
| `backend/sources/xiaohongshu.py` | 异步实时来源、正文读取、会话锁、手动登录、导航与限制处理 |
| `backend/verification/capabilities.py` | 300 秒超时、运行时输入链接 |
| `backend/verification/service.py` | 每请求复制 input_urls，避免全局污染 |
| `backend/verification/subgraphs/facts/graph.py` | 注入来源与输入排除到各条主张的 SearchSession |
| `backend/verification/subgraphs/facts/llm.py` | Plan / Validate 完整响应与 JSON 校验 |
| `backend/verification/subgraphs/facts/model.py` | 工具 / 查询 / 候选 / 证据硬上限 |
| `backend/verification/subgraphs/facts/prompts/__init__.py` | Search 不再要求回写整份材料 Schema |
| `backend/verification/subgraphs/facts/prompts/plan.md` | 新预算说明 |
| `backend/verification/subgraphs/facts/prompts/search.md` | 实时来源、动作协议、预算、空参数 done 与错误保留 |
| `backend/verification/subgraphs/facts/search.py` | 来源并行、计数槽、读取时间、输入排除、真实账本 done、有限纠错 |
| `docs/CRAWLER_HANDOFF.md` | 运行、保留原件、回退、各轮修复交接 |
| `docs/CRAWLER_INTEGRATION_PLAN.md` | 实时接入与本地验收后推送方案 |
| `docs/CRAWLER_NAVIGATION_FIX.md` | 首页初始化根因、修复、安全诊断及后续验证 |
| `docs/CRAWLER_SOURCE_HASHES.json` | 原爬虫来源与哈希 |
| `docs/CRAWLER_VERIFICATION.json` | 初版集成机器验收快照 |
| `docs/EMPTY_INPUT_AND_MODEL_JSON.md` | 空材料、模型协议与 Search 修复记录 |
| `docs/READABLE_VERIFICATION_RESULTS.md` | 可读结果、安全渲染、定位和前端检查 |
| `docs/成果/Fact子图设计.md` | 新预算、来源接入、计数与失败收束 |
| `docs/成果/业务规则.md` | 空主张与禁止补造规则 |
| `docs/成果/主图设计.md` | 默认来源装配、运行时输入排除 |
| `frontend/app.js` | 材料检查、真实状态、结果卡片、引用编号、安全链接与定位 |
| `frontend/index.html` | 输入提示、结果容器、折叠 JSON、静态资源版本 |
| `frontend/style.css` | 结论与证据卡片样式 |
| `tests/test_browser.py` | DeepSeek 裸控制字符与折叠原响应读取回归 |
| `tests/test_deepseek_response.py` | 模型响应协议检查 |
| `tests/test_fact_browser.py` | 原生 done / 缺 action / 有限纠错的实际浏览器链路 |
| `tests/test_fact_state.py` | 新预算边界 |
| `tests/test_fact_workflow.py` | 完整 Fact 流程与新预算兼容 |
| `tests/test_frontend_input.py` | 空材料、文字 / 链接 / 图片入口 |
| `tests/test_frontend_results.py` | 判定、条件缺口、角色、安全、归属、编号、重复提交和焦点 |
| `tests/test_frontend_status.py` | 完成 / 失败 / 未实现 / 缺证据展示 |
| `tests/test_model_json.py` | 控制字符、坏结构、NaN / Infinity 等边界 |
| `tests/test_native_search_finish.py` | 真实账本结束协议 |
| `tests/test_xiaohongshu_api.py` | HTTP 接入、引用与请求隔离 |
| `tests/test_xiaohongshu_bootstrap.py` | 首页面初始化、限制 / 取消 / 安全异常字段 |
| `tests/test_xiaohongshu_search.py` | 共享预算、去重、输入排除、并行与部分失败 |
| `tests/test_xiaohongshu_source.py` | 来源配置、正文契约、锁、超时、清理 |
| `tests/test_xiaohongshu_source_browser.py` | 十篇真实本地 DOM、网页余量、HTTP 错误及取消 |

## 10. 推送准备与后续操作

当前原仓库写权限正在申请，用户手动本地验收仍在进行。此前普通 push 返回 403；本轮未用写操作探测权限，也没有 Fork、强制推送、改 main 或创建 PR。

待用户确认权限到位及本地验收通过后：

```powershell
git status --short
git remote -v
git branch -vv
git -c http.sslBackend=openssl fetch origin main
git rev-list --left-right --count origin/main...HEAD
git -c http.sslBackend=openssl ls-remote --heads origin feat/xiaohongshu-crawler

# 仅正常推送原仓库功能分支；不使用 --force。
git -c http.sslBackend=openssl push -u origin feat/xiaohongshu-crawler

# 远端分支 SHA 必须与本地 HEAD 相等，记录实际结果。
git rev-parse HEAD
git -c http.sslBackend=openssl ls-remote --heads origin feat/xiaohongshu-crawler
```

上游若更新或远端分支已有他人提交，先检查差异，不自动重置或覆盖。再次遇到 403 时保留本地提交、原错误与回执；当前报告不是推送成功证明。

只推 Git 中的公开代码、合成测试和文档。`backend/config.local.toml` / `config.toml`、profile、真实数据、数据库、截图、日志、虚拟环境和本机 handoff 都不进入提交。提交前核对所有实现提交的文件清单与凭据检查，历史归档不得误加入。

详细分阶段记录仍保留：[实时接入交接](CRAWLER_HANDOFF.md)、[空材料与 JSON](EMPTY_INPUT_AND_MODEL_JSON.md)、[导航修复](CRAWLER_NAVIGATION_FIX.md)、[结果展示](READABLE_VERIFICATION_RESULTS.md)。本机最新准备 SHA、检查结果和推送状态见 `../handoff/online/push-preparation.json` 与 `../handoff/CRAWLER_ONLINE_PUSH_RECEIPT.md`。
