# 开源核验 Agent 调研

调研日期：2026-10-05。依据官方仓库、源码与文档；未安装运行或验证旅行场景效果。

**有可包装调用的核验器，但本次未找到能原样接入现有四类子图的方案。** FACT 最接近现成能力；ROUTE、CROWD、EXPERIENCE 仍需领域证据与判断规则。建议先读 FIRE 的循环，再看 DEFAME 的可调用核验器，最后看 Loki 的模块；封装试点优先 DEFAME。

| 项目与源码入口 | 已核实的工作流 | 接入判断与主要门槛 |
| --- | --- | --- |
| [FIRE](https://github.com/mbzuai-nlp/fire) · [verify_atomic_claim.py](https://github.com/mbzuai-nlp/fire/blob/main/eval/fire/verify_atomic_claim.py) | 判断证据是否足够→补搜→再判断；可提前结束 | **借流程优先**。模型、Serper、SBERT/PyTorch 依赖；部分代码强制 CUDA。末尾强制真假二选一，应改为允许证据不足；根目录未见许可证文件，暂不建议复制代码。 |
| [DEFAME](https://github.com/multimodal-ai-lab/DEFAME) · [FactChecker](https://github.com/multimodal-ai-lab/DEFAME/blob/main/defame/fact_checker.py) | [规划工具→取证→整理证据→裁决；信息不足继续循环](https://github.com/multimodal-ai-lab/DEFAME/blob/main/defame/procedure/variants/summary_based/dynamic.py)；支持图文 | **可包装，工具栈较重**。Apache-2.0（第三方目录另计）；同步 `verify_claim()` 返回报告与元数据，也提供 HTTP 服务。可注入工具、限制轮数；需模型及相应检索 API。 |
| [Loki / OpenFactVerification](https://github.com/Libr-AI/OpenFactVerification) · [FactCheck](https://github.com/Libr-AI/OpenFactVerification/blob/main/factcheck/__init__.py) | 提取主张→筛选可核查项、生成问题→检索→逐条验证；返回证据文本、URL、关系与解释 | **可包装，需修整**。MIT；真实入口是同步 `check_text()`，需模型与检索 API 配置（默认 Serper）。已有 claims 可复用检索/验证模块；[QueryGenerator](https://github.com/Libr-AI/OpenFactVerification/blob/main/factcheck/core/QueryGenerator.py) 用 `eval` 解析模型输出，接入前应替换。 |
| [SAFE](https://github.com/google-deepmind/long-form-factuality) · [check_atomic_fact](https://github.com/google-deepmind/long-form-factuality/blob/main/eval/safe/rate_atomic_fact.py) | 单条原子事实→多轮搜索→Supported / Not Supported | **适合最小基线**。Apache-2.0；需模型、Serper。[搜索实现](https://github.com/google-deepmind/long-form-factuality/blob/main/eval/safe/query_serper.py)仅保留摘要，需补回 URL；未获支持不能直接解释为假。 |
| [OpenFactCheck v1](https://github.com/openfactcheck-research/openfactcheck/tree/v1) · [源码目录](https://github.com/openfactcheck-research/openfactcheck/tree/v1/src/openfactcheck) | 主张处理→检索→验证；统一封装多种核验流程并评估 | **可作对照框架**。[v1 GPL-3.0](https://github.com/openfactcheck-research/openfactcheck/blob/v1/LICENSE) / [main AGPL-3.0](https://github.com/openfactcheck-research/openfactcheck/blob/main/LICENSE)；模型与搜索依赖随 solver 配置。[v1 文档](https://github.com/openfactcheck-research/openfactcheck/blob/v1/README.md)提供 `ResponseEvaluator.evaluate()`；main 的 v2 开发中，应固定版本。 |

另查了 [FactAgent](https://github.com/HySonLab/FactAgent)：其 LangGraph supervisor 调度拆解、查询、取证、裁决值得参考，但 [源码](https://github.com/HySonLab/FactAgent/blob/main/src/main_agent.py)实际入口是 `process_claim()`、返回执行步骤，与 README 的 `verify_claim()` 示例不符，暂不列入优先接入名单。

以下是**针对本项目的设计建议**，不是上述项目已经实现的旅行能力：

| CLAIM | 建议工作流 | 应借鉴的部分 |
| --- | --- | --- |
| FACT | 确认 POI 与适用日期→优先查官方开放、预约、收费信息→检查条件与冲突→必要时补搜→支持/矛盾/证据不足 | FIRE 的补搜与早停；DEFAME 的工具和证据报告。 |
| ROUTE | 明确起终点、交通方式、出行时间→地图路径计算→核对入口、绕行与通行限制→比较距离和时长区间 | 借工具调度；地图数据与计算应作为证据，不能只靠网页文字或模型估算。 |
| CROWD | 对齐地点、日期、星期、时段、节假日→收集相同时段的近期记录→区分实时与历史→给范围和缺口 | 借多来源检索；无匹配时段数据就保留未知，不能由一般热度推出当前排队时长。 |
| EXPERIENCE | 拆出安静、舒适、出片等维度→匹配人群、季节、时段→汇总独立体验与反例→说明一致性及适用条件 | 借证据组织；不把多数评论当事实真值，也不强制真假标签。 |

接入建议：保留现有主图，各子图自行选择 claims；新增适配层，把外部输出转换为 `ClaimFinding + Evidence(source, content, url)`，保留原 `claim_id` 并填写 `selected_claim_ids`。检索优先通过 `VerificationCapabilities.evidence_sources` 注入，已有原子主张不再重复抽取。循环设置检索预算并服从子图 180 秒期限；同步核验器宜隔离运行，避免阻塞异步主图。结论先写入 finding，**`SubgraphResult.status` 只表示执行状态**。最小试点先做 FACT，验证证据可追溯和过期信息处理，再扩展其余三类。
