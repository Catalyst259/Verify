# 空材料与模型响应故障交接

## 空主张与部分完成的区别

当前产品从用户提交的文字、图片、链接中提取说法，地点名称只限定范围。仅地点时不能生成待核验主张；提取 prompt 明确禁止为补充主张主动搜索地点。空列表进入主图汇总，不调用 Fact 或小红书。HTTP API 保留仅地点请求的200/no_claims兼容行为；前端增加材料三选一检查、地点提示和 no_claims 引导，避免消耗模型调用后才发现没有输入材料。

默认注册 fact、route、crowd、experience。后三个子图目前仍是 not_implemented，因此即使 Fact 完成，默认整体状态仍为 partial。网页挑战页、模型协议错误和子图缺功能分别记录，不能通过改状态掩盖未完成项。新增爬虫模块不会让尚未实现的三个子图自动可用。

## DeepSeek JSON 兼容修复

实际模型响应在 thinking 字符串内含裸换行，直接 model_validate_json 会拒绝并使 Search Agent 在单次失败后停止。已经从小红书登记的证据仍保留，Fact 可在这些证据上给出判定，但 Search 的错误令该 finding 为 partial。

backend/common/model_json.py 用标准库只规范化字符串内的控制字符：严格解析成功的合法 JSON 原样返回；仅遇裸控制字符时使用 json.loads(strict=False) 解码，再用 json.dumps 转义。NaN/Infinity、缺括号、无效转义、说明夹带、多个JSON文档仍拒绝；不抽取片段、不补造字段或结构。结果继续交给原来的 Pydantic Schema 与证据账本校验。

提取 Agent、Fact Search Agent 两处 DeepSeek JSON mode 适配调用此 helper；Fact Plan/Validate 保留已有完整 Markdown 围栏兼容，再调用 helper。两处 Agent 也拒绝 finish_reason 非 stop 或空正文，避免执行截断但恰好语法完整的输出。没有延长截止时间、修改预算、绕过验证码或降低引用校验。

模型密钥只从已忽略的本地配置读取。本机配置和诊断原文位于仓库外私有交接目录，不随本次提交上传。

## Search 结束结果由代码生成

JSON 修复后的真实复测进入了第二轮，但模型在结构化结束结果里重新回写历史材料，并改造证据字段，触发既有“未读取或被改写的证据”校验。这种结果继续拒绝；不能为了让流程结束而放松校验。

原生 Search 的结束动作改为 `done:{}`：空参数模型禁止额外字段，由 SearchSession 直接生成本轮 SearchResult。Tools 不再注册 SearchResult 输出模型，Agent 不再传 output_model_schema，Search 提示词不再附最终结果 Schema，避免要求模型复制整篇正文。SDK 的 done 动作名称保留，最后一步和预算耗尽仍能结束。

正文、证据ID、来源、读取时间、实际错误继续来自执行账本；第二轮重复材料不会回显成新增证据，历史材料仍留在 State。结束动作不消耗取证预算。外部注入 runner 的 `result(raw)` 校验和最终引用校验不变；done 的 success 只表示工具流程结束，不表示证据充分或主张为真。

后续真实复测又发现完整 JSON 仅包含 thinking、缺少必需 action，原有 Schema 正确拒绝该响应。Search 的提示现在明确要求非空 action 数组，结束使用 `action:[{"done":{}}]`。SDK 的连续失败上限设为2，即一次失败后有一次后续步骤纠正机会；连续两次失败仍停止并保留材料，不给错误响应补造动作。失败步骤也计入总步骤上限，原有最多21步骤和Fact截止时间继续限制运行，模型纠错不增加查询、读取或证据额度。

## 验收与接手

2026-10-06 追加修复后的完整后端测试：185 passed、18 skipped。其中18项 opt-in 浏览器测试有9项原浏览器/Fact链路在此次重新运行，5项新增空材料前端场景重新运行；最新6项 Fact 浏览器链路全部通过，未变更的4项小红书 DOM 浏览器场景保留上次通过记录。

- model-json 单元检查裸LF/CR/tab/NUL、已有转义保持原义、合法JSON原样返回、错误Schema及结构失败。
- 8项实际嵌套 Agent 适配检查截断、过滤、空响应拒绝及浏览器清理。
- 7项真实系统 Chrome + 本地供应商协议替身链路检查两个实际 DeepSeek 适配支持裸控制字符，普通 GPT 路径和坏最终输出保留行为仍正常。与上述8项合跑的日志为 json-browser-tests.log（15 passed）。
- 5项真实系统 Chrome 前端场景检查仅地点、空白材料不发POST，文字/链接/已上传图片单独存在均可提交；没有真实模型或公网调用。
- 7项 native done 单元场景检查本轮完整账本、真实错误、第二轮去重及历史保留、四种模型额外字段拒绝、Search 提示分支不附最终结果 Schema。最新6项真实 Chrome Fact 场景检查原生空参数结束、裸控制字符、一次缺action后纠正、连续两次缺action仍partial并保留正文。后两种场景只有2次取证工具调用、1次查询、1份正文，坏响应不会执行或重放取证动作。

最终完整日志在本机 `../handoff/online/protocol-retry-backend-final.log`、`missing-action-browser-tests.log`、`json-browser-tests.log`、`test_frontend_input-1.log`。旧失败日志没有删除：首次JSON回归6项失败仅因既有测试固定预期 ValidationError，而前置JSON校验现在对坏语法抛 ValueError；更新测试允许这两种失败类别后完整回归通过，无效内容仍不会调用搜索。native done 版完整回归第一次出现 Windows CPython dis/inspect 原生 access violation，未得到完整结果；源码和检查未变，换新临时目录重跑185项通过，最新纠错版185项也通过。该负面结果保存在 diagnosis-native-final.log，尚未定位解释器异常。先前浏览器测试记录 Windows 管道析构警告，最新6项运行无警告。

真实模型与爬虫 API 验证另存本机 `../handoff/online/`；单条用户结果、具体地点和采集正文不会放入公开文档。运行中服务已先通过独立控制台 Ctrl+C 正常 shutdown，确认旧浏览器结束，再用单worker、无reload加载修复。最新进程以 backend-startup.json 为准。

最新一次真实 DeepSeek + 小红书 API 运行完成两轮，HTTP200，Fact completed、finding.error 为空，147.027秒；JSON与证据回写中断未再发生。但此次小红书网页导航多次失败或超时，DuckDuckGo 返回人机挑战，实际新增正文为0，判定为 UNVERIFIED。这个结果只确认流程和错误保留，不是成功采集十条或主张质量验收。此前几次真实运行各取得十条正文的历史记录保留，不能替代本次失败记录；整体 partial 仍包含三个未实现子图。

上述零正文记录是导航修复前的历史结果。后续同会话冷搜索超时已重现，恢复主页初始化后生产来源实际读取十条，导航修复与新的状态展示见 [CRAWLER_NAVIGATION_FIX.md](CRAWLER_NAVIGATION_FIX.md)。当前运行与API复测以本机最新启动和验收回执为准，不覆盖历史失败证据。

后续 agent 先重新检查 Git、最新上游、配置覆盖优先级和进程，阅读本机 EMPTY_INPUT_AND_PARTIAL_HANDOFF.md 了解具体实际结果。测试供应商协议兼容不代表判定质量；网页搜索仍可能出现人机验证，三个未实现子图仍需各自完成开发与验收。
