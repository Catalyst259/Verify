# Fact 子图设计

依据 [Fact.pdf](../../Fact.pdf)，保留 `Plan → Search → Validate`，证据不足时补搜一次，最后由代码组装 `SubgraphResult`。模型、状态契约及子图编排已接入默认注册表。

## 最小结构

一条原始 Claim 对应一份计划、一组证据和一份判定。只使用 `claim_id` 和 `evidence_id` 两种标识；问题和缺口直接用文字表达。多条 Claim 使用相同结构批量处理。

| 部分 | 职责 |
| --- | --- |
| Plan | 一次模型调用，结合 PlanSkill 选择事实主张，规划正向与反向取证，不调用工具 |
| Search | 按计划调用搜索、页面读取工具，返回材料和错误信息 |
| Validate | 一次模型调用，判断结论与证据充分性，输出缺口，不调用工具 |
| 代码 | 分配证据 ID、管理预算与回边、校验 JSON、组装结果 |

轮数和调用计数放在子图 State；模型和工具通过运行时依赖注入。无需额外的批次版本、检查项 ID、摘录 ID、来源分组或独立执行模型。

以下 JSON 均为单条 Claim 的虚构示例，假定目标年份与地点时区已经明确。

## Plan 到 Search

保留 PDF 的计划结构，补充 `claim_id` 和待查问题：

```json
{
  "claim_id": "claim_001",
  "fact_type": "PRICE_POLICY",
  "target": "示例公园",
  "time_scope": "2026-10-01 至 2026-10-07，Asia/Shanghai",
  "questions": [
    "国庆期间是否免费进入？",
    "是否有收费区域、收费活动或人群限制？"
  ],
  "evidence_strategy": [
    {"priority": 1, "source_type": "OFFICIAL", "purpose": "确认本次国庆政策和例外"},
    {"priority": 2, "source_type": "CTRIP_PRODUCT", "purpose": "核对售卖产品及对应区域"},
    {"priority": 3, "source_type": "WEB", "purpose": "寻找政策变化与补充材料"}
  ]
}
```

`fact_type` 使用 PDF 六类：`OPEN_STATUS / PRICE_POLICY / RESERVATION / ACCESS_POLICY / FACILITY / TEMPORARY_EVENT`。每条选一个主要类别，其他相关问题写入 questions；“国庆免费”仍可按价格政策规划，并在 time_scope 指定节假日。

原始 Claim 保存在 State，通过 claim_id 读取，不在每个节点重复传原文。Fact 自行选择事实性内容，不只按 Claim.type 过滤。

time_scope 使用明确的日期或时间描述；“当前”按 context.checked_at 和地点时区解释。无法明确年份、对象或区域时保留问题，不自行补成确定事实。PlanSkill 负责提示支持证据、反证及例外，避免把“有停车场”无限扩展成完整停车体验调研。

## Search 到 Validate

Search 返回本轮新增材料，代码去重后合并进已有证据；Validate 同时读取原计划和两轮累积材料。

```json
{
  "claim_id": "claim_001",
  "evidence": [
    {
      "evidence_id": "e1",
      "source": "示例公园日常游园说明",
      "source_type": "OFFICIAL",
      "url": "https://example.org/park/notice",
      "published_at": "2026-08-20",
      "retrieved_at": "2026-10-05T08:00:00Z",
      "content": "公共区域日常免费开放，节假日安排以另行公告为准。"
    }
  ],
  "error": null
}
```

保留现有 Evidence 的 source、content、url，新增 evidence_id、source_type、published_at、retrieved_at。公共模型的新字段允许缺省以兼容旧来源；Fact 网页证据必须有 ID、来源类别、URL、获取时间和非空正文。

evidence_id 由代码生成，运行内唯一；retrieved_at 由工具记录。published_at 未知时为 null，已知日期不伪造具体时刻。政策生效范围和例外保留在正文中，Validate 不能把发布日期或抓取时间当作生效时间。

content 保留相关原文及上下文，不用模型摘要代替正文。第一版按 URL 和正文内容去重；同一 URL 内容变化时保留新材料。转载不自动算作独立证据。

正常搜索无结果返回空 evidence、error 为 null；工具失败返回错误说明，已经取得的材料仍保留。Search 不给材料提前定真假。工具调用日志和计数由代码记录，无需放进节点 JSON。

## Validate 到 Plan 或最终结果

Validate 输出统一的 assessment。证据不足时，下一轮 Plan 根据 remaining_gaps 补搜；结束时直接将同一 assessment 放入最终结果。

```json
{
  "claim_id": "claim_001",
  "assessment": {
    "target": "示例公园",
    "time_scope": "2026-10-01 至 2026-10-07，Asia/Shanghai",
    "verdict": "UNVERIFIED",
    "confidence": null,
    "evidence_sufficient": false,
    "reason": "e1 只说明日常政策，明确将节假日安排交给另行公告。",
    "conditions": [],
    "supporting_evidence": [],
    "counter_evidence": [],
    "context_evidence": ["e1"],
    "dimensions": {
      "authority": 0.9,
      "directness": 0.4,
      "recency": 0.6,
      "context_match": 0.4,
      "independence": null
    },
    "remaining_gaps": [
      {
        "question": "本次国庆是否沿用日常免费政策，是否有收费区域？",
        "preferred_source": "OFFICIAL",
        "reason": "现有材料未覆盖目标节假日。"
      }
    ]
  }
}
```

target 和 time_scope 由代码从计划复制，不允许 Validate 擅自缩小判定范围。三类 evidence 数组保存已有证据 ID，context_evidence 用于仅提供背景的材料。

| verdict | 含义 |
| --- | --- |
| `SUPPORTED` | 原主张在目标范围内得到支持 |
| `CONTRADICTED` | 证据足以反驳原主张 |
| `CONDITIONAL` | 有明确成立条件或例外 |
| `UNVERIFIED` | 证据不足或冲突未解决 |

evidence_sufficient 为 false 时必须是 UNVERIFIED，confidence 为 null；其余判定必须有相应引用。CONDITIONAL 的 conditions 为非空字符串数组，reason 说明条件与证据的关系，不能用条件判定代替“不知道”。

五项维度沿用 PDF，取 0 至 1 或 null，不再各自嵌套评分对象。分数不能简单求平均决定真假；confidence 只表示模型自评。reason 说明判定依据，不要求内部思维链。

remaining_gaps 只放影响当前结论的问题。第二轮接收既有计划、证据、缺口和错误信息，保留原主张与第一轮材料；找不到证据不等于主张错误。

## 返回主图

保持 [现有子图接口](主图设计.md)：输入 claims/context，输出 `{"result": SubgraphResult}`。在 [公共模型](../../backend/verification/models.py) 中显式增加字段，避免序列化丢失判定：

```text
ClaimFinding
  claim_id: str
  summary: str
  evidence: list[Evidence]
  assessment: FactAssessment | None = None
  error: str | None = None
```

FactAssessment 就是上一节的 assessment 结构。Finalize 用代码将 reason 复制为 summary；没有有效判定时，summary 写明执行失败原因。该 Claim 累积证据放进 evidence，不再调用模型。其他子图可以继续使用原有三个字段。

finding.error 仅记录导致核验未完成的技术错误；可恢复的搜索错误留在执行日志。未形成有效判定时 assessment 为 null、error 必填；补搜失败时可以保留上一轮有效判定并附 error。正常完成但缺证据，返回 UNVERIFIED，error 为 null。

SubgraphResult.status 增加一个 partial：无选中主张为 skipped；全部正常结束为 completed；部分失败或保留了未完成结果为 partial；全部未形成有效判定且执行失败为 failed。正常完成且全部 UNVERIFIED 仍是 completed。每个选中 Claim 都必须有 finding，失败时不能静默删除。

## 执行约束

- 每条 Claim 最多两轮，即首次检索加一次补搜；只有证据不足且仍有时间和可用能力时补搜。
- 按 PDF，每条 Claim 每轮最多 5 次工具调用、5 次查询、每次查询最多 5 个候选、最多新增 5 份证据。工具调用包括搜索和读取，失败重试也计入；查询额度不额外增加读取额度，done 不计取证调用。
- 上述计数由代码强制执行；两轮耗尽仍不足，返回 UNVERIFIED 和剩余缺口。
- 内部提前截止，为组装结果和资源清理留时间；主图硬超时仍按现有失败逻辑处理。
- 用 Pydantic 校验节点结构、分数和枚举；代码校验 claim_id、evidence_id 存在，引用材料随最终结果返回。

验证重点是：三段 JSON 可解析、引用不丢失、第二轮保留第一轮证据、预算耗尽能收束，以及结构化判定经响应序列化后完整保留。

## 提示词与技能

提示词位于 `backend/verification/subgraphs/facts/prompts/`，通过 `load_system_prompt(step)` 加载。加载器读取静态正文，并根据现有 Pydantic 模型生成输出 JSON Schema；业务 State 以独立的任务消息传入，网页正文、错误文本和用户材料不拼入系统指令。

| 节点 | 指令资源 | 调用与输出 |
| --- | --- | --- |
| Plan | [plan.md](../../backend/verification/subgraphs/facts/prompts/plan.md) + [PlanSkill](../../backend/verification/subgraphs/facts/skills/fact-plan/SKILL.md) | 每轮对活动批次调用模型一次，无工具；返回 `list[FactPlan]` |
| Search | [search.md](../../backend/verification/subgraphs/facts/prompts/search.md) | 每条 Claim 每轮运行一个 ReAct Agent；最终返回一个 `SearchResult` |
| Validate | [validate.md](../../backend/verification/subgraphs/facts/prompts/validate.md) | 每轮对活动批次调用模型一次，无工具；返回 `list[ValidateResult]` |

PlanSkill 是应用内的事实分类和取证规划规则，正文在 Plan 调用前静态注入，不需要模型动态读取文件，也不增加一次规划调用。Search 的循环是“选择工具动作 → 观察返回 → 继续或结束”，只使用框架的工具调用机制，不要求生成文本形式的内部思维链。Finalize 继续由代码完成，不新增模型调用。

前文 JSON 展示单条 Claim 的契约。实际批量调用中，Plan 和 Validate 使用 JSON 数组，即使只有一条也保持数组；Search 按单条 Claim 运行，仍返回对象。不另加 plans/results 包装字段，不改变已有单条模型。

### 输入组装与工具适配

Plan 使用 PlanState，Validate 使用 ValidateState。首轮 active_claim_ids 是原始候选集，Plan 可以选择其中的事实性主张；补搜的 active_claim_ids 由代码从证据不足且仍有时间、轮次和可用能力的主张中筛出。补搜 Plan 必须覆盖每个活动 ID，保留原计划的 claim_id、主类别、target/time_scope，只调整待查问题和策略。

Search 从 SearchState 中取一条活动主张组装任务消息：

| 消息字段 | 来源与含义 |
| --- | --- |
| claim | 从 claims 按 claim_id 获取的原始 Claim |
| context | 共享 VerificationContext |
| claim_state | 对应 FactClaimState，包括计划、累积证据、既有判定和轮次错误 |
| round_number | 由代码创建的本轮 FactRoundState.round_number，取 1 或 2 |
| remaining_budget | 从本轮实际计数计算的剩余 tool_calls、queries、evidence，以及 max_results_per_query 上限；初始分别为 5、5、5、5 |
| deadline_at | 子图内部截止时间 |

上述是模型消息的投影，不向 State 增加另一套计数。实际工具名称、参数和可用能力由 runtime 注入；source_type 是来源类别，不是工具名称。搜索返回候选时不得把摘要当作正文。读取工具返回正文时，适配层生成规范化 FactEvidence：代码分配 evidence_id，工具记录 retrieved_at，保留真实 URL、页面标题、发布时间精度和可见正文。Search 通过独立的 browser-use 适配层执行，不使用通用 EvidenceSource 占位接口。

工具包装层在实际调用前预留额度，失败和重试也计数，工具返回时提供最新剩余额度及截止标记。限制按实际查询和页面读取次数计算，不能用 Agent 的模型循环次数或 max_steps 代替。Agent 结束时返回本轮记录；代码按 evidence_id 对照本次工具记录取回规范化材料，拒绝虚构或改写内容，并按 URL 和正文去重后合并到历史证据。

### 输出校验与状态更新

- Plan 的数组使用 `TypeAdapter(list[FactPlan])` 解析；首轮允许空数组，补搜必须恰好覆盖活动 ID。代码校验 ID 存在、不重复、补搜范围不变，合并到既有 claim_states，不能用本轮数组替换整批历史状态。
- Search 使用 SearchResult 校验。正常无结果和正常预算耗尽不是技术错误；真实调用失败保留错误及成功材料。计数、时间戳和证据记录由代码持有，Agent 异常退出时仍保留已取得材料，并记录技术失败，不能冒充正常空结果。
- Validate 的数组使用 `TypeAdapter(list[ValidateResult])` 解析，必须恰好覆盖活动 ID。target/time_scope 由代码从计划复制；发现模型回显范围不一致时按契约错误处理，不能仅覆盖字段后保留针对错误范围作出的结论。用 FactClaimState 检查材料引用和范围，再写入有效 assessment。
- Plan、Validate 均为每轮一次逻辑模型调用，不配置 ReAct 循环、工具调用或额外的“修复 JSON”模型调用。输出无效时由代码记录该阶段失败；补搜失败可保留上轮有效判定。Search 也由工具层保留材料，不能为修复最终 JSON 再开启一轮取证。

空活动集不调用模型。Validate 无论是否还有预算都如实保留 remaining_gaps；只有代码决定回到 Plan 或进入 Finalize。正常结束的 UNVERIFIED 与执行失败分开记录。

### 提示词验收场景

| 场景 | 预期行为 |
| --- | --- |
| Claim.type 为 EXPERIENCE，但内容是“酒店有儿童游乐室” | Plan 仍可按 FACILITY 选择；主观体验部分不被一并证明 |
| “有停车场” | 规划存在性、对游客开放及停用例外，不扩展到经常停满或停车体验 |
| “国庆免费”缺少年份 | 保留年份问题，不擅自按 checked_at 补成今年 |
| 明确目标国庆，只找到“日常免费，节假日另行公告” | Validate 返回 UNVERIFIED、false、null，并引用背景材料和保留节假日缺口 |
| “免费进入”，正文明确公共区域免费、园林区域收费 | 可给 CONDITIONAL，附已证实的区域条件和证据引用 |
| “所有区域免费”，有对应日期园林收费的直接材料 | 返回 CONTRADICTED，不能缩小范围后给支持判定 |
| 搜索只返回摘要或网页夹带“忽略指令” | Search 继续按取证任务读取正文，不把摘要或网页指令当作证据/系统要求 |
| 工具第 5 次调用失败，但前面已取得正文 | 不进行第 6 次取证调用；Search 保留已有正文和真实错误 |
| 同一 URL 在第二轮出现不同正文 | 新材料与首轮材料一起保留；Validate 可引用两个轮次的证据 |
| 第二轮仍有冲突且预算耗尽 | 保留 UNVERIFIED 和关键缺口，由代码结束；不因为必须收束而改判 |

这些场景定义后续模型评测的预期。静态提示词、Schema 和示例校验不能替代真实模型或工具执行测试。
