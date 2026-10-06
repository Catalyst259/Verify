# 后端实时小红书来源交接

## 分支与回退

origin 为 `https://github.com/Catalyst259/Verify.git`，分支为 `feat/xiaohongshu-crawler`，本次基线为 `55c22d0e39689fb88215ce62f118195302336098`。实施方案见 [CRAWLER_INTEGRATION_PLAN.md](CRAWLER_INTEGRATION_PLAN.md)。

旧离线接入提交 `8c2107eece456425b5761606f16b238f7abcfca6` 已从当前工作副本撤出，完整旧仓库留在工作区 `../handoff/rollback/20261006-161019/Verify/`。回退记录 `rollback.json` 核对本地配置、十四条笔记 JSONL 和数据库的原始 SHA256；`restored-runtime.json` 记录新副本恢复结果。原 `../新华社爬虫/` 未改写。

旧原始数据和登录资料仍是用户本地资产，不属于提交或共享交接包。新后端不读取旧笔记文件，也不启动爬虫 GUI 或生成 Excel。来源逻辑出处见 [CRAWLER_SOURCE_HASHES.json](CRAWLER_SOURCE_HASHES.json)。

## 配置与启动

使用 Python 3.12+，在仓库根目录安装后端依赖。Playwright 已属于生产依赖；本机使用系统 Edge，无需安装额外浏览器。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
# 已有本地配置不要覆盖；首次从 backend/config.example.toml 复制。
```

程序按 config.local.toml、config.toml、config.example.toml 的优先级读取 backend 下的首个存在文件。api_key、base_url 和 model 在需要模型核验时校验；启动页面和爬虫手动登录不要求模型密钥。

配置表 `[xiaohongshu]`：max_results 默认 10（整数 1–10），timeout_seconds 默认 120，pacing_seconds 默认 4。profile_path 默认后端数据目录内的 xiaohongshu-profile，配置相对路径按仓库根目录解析；executable_path 不配置时使用系统 Edge。本次恢复的专用副本在 `backend/data/xiaohongshu-profile/`，全部 backend/data 已忽略。

先关闭使用同一专用资料目录的后端，再单独登录：

```powershell
.\.venv\Scripts\python.exe -m backend.sources.xiaohongshu --login
```

在打开的浏览器中手动登录或完成站点验证，按命令提示结束，浏览器关闭后启动后端。登录资料文件存在不等于登录有效；资料占用不能静默切换到空会话。

```powershell
$env:PYTHONUTF8 = "1"
$env:ANONYMIZED_TELEMETRY = "false"
# 受限机器将 browser-use 配置缓存放在可写工作区。
$env:BROWSER_USE_CONFIG_DIR = "$PWD/../handoff/online/browser-use"
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000/。本版 Windows 使用单 worker，不使用 --reload。配置数量变化需重新构造后端来源，重启服务后生效。

## 调用契约与执行行为

XiaohongshuSource.search(query) 返回 list[Evidence]，可作为已有 EvidenceSource 使用。具体实现额外接受 execute、excluded_ids 和 deadline_at，以便 Fact 注入受控执行回调和本次输入限制。默认数量由来源构造配置控制。

每次 Fact search_web 自动并行搜索相同关键词的网页候选及实时小红书笔记；小红书搜索一次、每篇详情读取一次，均走 SearchSession.execute。网页候选仍由 read_page 读取，候选与摘要不是证据。action 输出展示实际登记的小红书证据及可恢复错误，最终引用仍以运行内账本为准。

来源返回 WEB 类型，标题和正文保持原文，作者进入来源标识，retrieved_at 为实际读取时间。发布时间只保留可靠 ISO 日期/时间，不能推算“昨天”。公开输出链接清除查询参数；搜索获得的带令牌详情 URL 仅留在本次来源调用私有内存。诊断不能泄露令牌、Cookie或密钥。

默认每轮 20 次工具调用、5 次查询、每次 10 个候选、15 份新增证据、两轮最多 30 份证据。查询和失败都计数，并发查询更新独立槽位。爬虫正文在只剩一个调用或一个证据槽位时停止，为网页正文保留余量；后续自动批次可返回部分材料。单次来源 120 秒包含锁等待，Fact/外层子图 300 秒，实际动作同时受剩余截止时间限制。

后端拥有单个异步来源实例及专用浏览器会话，锁串行化访问；查询参数和来源 URL 映射只在当前调用保存。每请求独立传递 input_urls，登记前按小红书 ID 排除本次输入，包括经网页跳转返回的同一笔记。没有笔记 ID 的短链接或只粘贴文字时，无法凭来源链接识别同一帖子。

正文缺失或最终 ID 不一致时不登记。登录、验证码、HTTP 错误（包括 401/403/429）或超时时记录错误，已登记的部分材料保留，网页取证继续。取消必须传播并清理当前导航，服务退出释放浏览器及驱动。

## 验证与接手

测试命令与结果见 [CRAWLER_VERIFICATION.json](CRAWLER_VERIFICATION.json)。使用新的 pytest 临时目录保存每次运行证据，不覆盖旧离线版本的日志：

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q --basetemp "$PWD/../handoff/online/pytest-backend-new"
$env:VERIFY_BROWSER_TESTS = "1"
$env:VERIFY_CHROMIUM = "C:\Program Files\Google\Chrome\Application\chrome.exe"
.\.venv\Scripts\python.exe -m pytest tests/test_browser.py tests/test_fact_browser.py tests/test_xiaohongshu_source_browser.py -q --basetemp "$PWD/../handoff/online/pytest-browser-new"
```

合成测试和本地模型协议替身检验接口与执行链路；真实站点测试另外记录账号会话是否有效、实际数量、错误和正文契约，不代表真实模型判定质量或所有关键词穷尽。十条为目标上限，重复笔记、输入排除、站点限制、空正文、剩余预算和时间都可能使实际数量更少。

2026-10-06 本地必需验收已通过：后端 158 项通过，11 项 opt-in 浏览器测试另行全部通过。完整浏览器日志含十条真实 DOM 正文、错误 HTTP 正文拒绝及取消清理。一次真实小红书模块查询“上海公园”读取十条 WEB 正文，59.482 秒，净化链接和带时区的读取时间检查通过；发布时间均保持未知，未调用真实模型，也未把正文写入交接回执。

新后端已用单 worker、无 reload 在本地启动，页面及 OpenAPI 返回 200，无效核验请求返回 422，四字段 HTTP 契约核对通过。进程号和日志位置以 `../handoff/online/backend-startup.json` 为准；本机辅助启动脚本为同目录 `start_backend.ps1`。随后已按用户要求在忽略的 config.local.toml 配置 DeepSeek Flash，并单独验证模型连接；模型参数每次调用重新读取，来源参数仍需重启生效。密钥不提交。独立来源采集不要求模型密钥。

所有工作需留档；最终本地提交、启动进程、测试日志与推送结果记录在仓库外 handoff/online 和 CRAWLER_ONLINE_PUSH_RECEIPT.md。后续 agent 先重新检查 Git、上游及进程，再继续工作。推送必须晚于本地验收，权限补齐前不改为 Fork 或推送旧离线提交。

后续空材料输入和真实模型 Search 中断排查见 [EMPTY_INPUT_AND_MODEL_JSON.md](EMPTY_INPUT_AND_MODEL_JSON.md)。它记录同日追加修复与验证；上面的158项和11项是首次爬虫集成的历史验收，后续完整测试数量以追加记录为准。默认 route、crowd、experience 仍未实现，不能把其 partial 状态归因于爬虫没有运行。

后续搜索导航修复见 [CRAWLER_NAVIGATION_FIX.md](CRAWLER_NAVIGATION_FIX.md)：来源已恢复原爬虫的主页初始化和限制检查，再进入关键词搜索；冷搜索超时与同会话预热后十条正文分别实测留档。导航异常只导出有限脱敏元数据，主页卡片不作为证据、预算不变。前端分别解释子图执行状态和证据是否充分；本轮197项后端及相关浏览器检查通过，真实模型API结果以本机追加回执为准。
