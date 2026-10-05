# 开源核验 Agent 调研

核查日期：2026-10-05。按近期代码提交、发布和多人贡献筛选，优先借鉴工作流；未实跑或验证中文旅行场景效果。

**优先看 Local Deep Research 的逐主张核验、GPT Researcher 的调查与复审循环；DeerFlow 2.0 用来参考工具调度。** 前两者更接近本项目需求，三者都有近期实质维护记录。

| 项目 | 活跃依据 | 最值得借鉴与阅读入口 |
| --- | --- | --- |
| [Local Deep Research](https://github.com/LearningCircuit/local-deep-research) · MIT | **2026-10-05** 仍有[去重与 URL 规范化代码修复](https://github.com/LearningCircuit/local-deep-research/commit/41a8bc9b278ca100fbd80af19a8070da90b62ba5)；[v1.10.6](https://github.com/LearningCircuit/local-deep-research/releases/tag/v1.10.6)列出 7 位贡献者 | **最贴近核验需求**。[Verify with Research](https://github.com/LearningCircuit/local-deep-research/blob/main/src/local_deep_research/web/static/js/pages/note-detail.js#L3145)：提取事实主张→合并问题启动取证→按同一份报告逐条给支持／矛盾／部分支持／未核实及来源。另看[检索策略](https://github.com/LearningCircuit/local-deep-research/blob/main/src/local_deep_research/advanced_search_system/strategies/langgraph_agent_strategy.py)，学习按发现选择工具、拆分子问题。 |
| [GPT Researcher](https://github.com/assafelovic/gpt-researcher) · Apache-2.0 | **2026-09-26** 发布 [v3.7.0](https://github.com/assafelovic/gpt-researcher/releases/tag/v3.7.0)，含检索插件、上下文过滤及多位社区作者的代码修复 | **最适合先试接入调查能力**。[编排](https://github.com/assafelovic/gpt-researcher/blob/main/multi_agents/agents/orchestrator.py)：规划→并行研究→写稿→事实复审→返修；[子任务循环](https://github.com/assafelovic/gpt-researcher/blob/main/multi_agents/agents/editor.py)把研究、审查、修订分开。 |
| [DeerFlow 2.0](https://github.com/bytedance/deer-flow) · MIT | **2026-10-05** 仍合入多位作者的[运行时和浏览器修复](https://github.com/bytedance/deer-flow/commits/main/) | **适合借工程组织方式**。[Deep Research 工作流](https://github.com/bytedance/deer-flow/blob/main/skills/public/deep-research/SKILL.md)：广搜→按维度深挖→找反例→查缺口；按任务加载工具与技能，子任务隔离上下文。当前 2.0 已是通用 Agent 运行框架，读旧版固定流程图容易对不上代码。 |

可以拿来用的程度：

- **LDR** 有 `pip install local-deep-research` 和 [`detailed_research(...)`](https://github.com/LearningCircuit/local-deep-research/blob/main/src/local_deep_research/api/research_functions.py)，可复用检索与带来源的研究结果；[Notes 核验服务](https://github.com/LearningCircuit/local-deep-research/blob/main/src/local_deep_research/research_library/notes/services/note_ai_service.py#L866)属于应用功能，默认抽取 5 条、最多 10 条事实主张，接入时需封装并适配本项目的 180 秒预算。
- **GPT Researcher** 有异步 [`conduct_research()`](https://github.com/assafelovic/gpt-researcher/blob/main/gpt_researcher/agent.py)，支持指定来源、域名和 MCP；可只取研究上下文与来源，省掉长报告生成。当前要求 Python 3.12+。
- **DeerFlow** 提供 [`DeerFlowClient`](https://github.com/bytedance/deer-flow#embedded-python-client)，可嵌入 Python；对已有主图而言，更值得借工具、上下文和任务管理设计。

一个影响设计的细节：GPT Researcher 的 [FactChecker](https://github.com/assafelovic/gpt-researcher/blob/main/multi_agents/agents/fact_checker.py)只复审已有草稿，本身没有检索调用；其编排达到修订上限会放行。建议借“独立复审”这一职责，改成发现证据缺口就补搜，预算耗尽则保留未知或待复核。

本项目建议采用：**选择主张→拆核验问题→调用领域工具→保存原文与出处→检查时效、反证和缺口→补搜或返回结论**。四类共用证据管理，分别设计判断规则：

| CLAIM | 需要补上的领域逻辑 |
| --- | --- |
| FACT | 官方信息优先；核对 POI、日期和适用条件；允许支持、矛盾、部分支持、证据不足。 |
| ROUTE | 明确起终点、交通方式和出行时间；用地图计算并核对入口与通行限制。 |
| CROWD | 对齐日期、时段、节假日；区分实时记录与历史规律，缺数据时保留未知。 |
| EXPERIENCE | 按安静、舒适等维度组织独立体验与反例，说明适用人群和条件，不强制真假。 |

若先做一次接入试验，建议在 FACT 子图内封装 GPT Researcher 的取证能力，借 LDR 的逐条核验结构；保留现有 `claim_id`，映射为 `ClaimFinding + Evidence`，`status` 继续只表示执行状态。无需替换现有主图。

[Open Deep Research](https://github.com/langchain-ai/open_deep_research)已于 **2026-08-21** 归档，本轮不列为活跃社区推荐。
