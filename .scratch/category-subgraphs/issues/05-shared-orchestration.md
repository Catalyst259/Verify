Blocked by: 01
Type: task
Status: open

# 公共编排骨架抽取 + FACT 迁移

## Goal

把 `FACT` 的两轮 `Plan → Search → Validate` 编排抽成四类共用的骨架，并让 `FACT` 改用它，输出完全不变。

## Why

四类子图的编排形状完全一致（初始化 → 计划 ⇄ 取证 → 判定 → 组装），只有三个注入点不同：计划/判定提示词、取证工具集、判定词表模型。不抽就得复制四份 `facts/graph.py` + `search.py`，修一处 bug 要改四遍。

## Decisions

类别差异收敛到三个注入点：

1. 计划与判定提示词
2. 取证工具集
3. 判定词表模型

**三个注入点的实际边界（读过 `facts/` 之后的结论，不要重新推导）：**

- `facts/graph.py` 的节点编排（`initialize` / `plan` / `search` / `validate` / `finalize` + `measured_node` 计时 + 预算 + 回边）**是通用的**，除两处：`load_system_prompt(step)` 与 `runtime.context.fact_llm` / `fact_search`。这两处参数化即可。
- `facts/search.py` 的 `SearchSession` / `create_tools` / `run_search` **是 browser-use 专用的**，不是通用取证。`ROUTE` 不用浏览器，它调 `map_routing` + `place_resolver`。
  → 所以第二个注入点的正确抽象层级是「**为一条 claim 产出证据**」，不是「**跑一个 browser-use agent**」。骨架只定义前者；`FACT`/`EXPERIENCE`/`CROWD` 的实现内部用浏览器，`ROUTE` 的实现内部用地图调用。
- `facts/model.py`（`FactType` / `FactPlan` / `FactEvidence` / `SearchResult` / `ValidateResult`）与 `facts/state.py`（`FactClaimState` / `FactState` / `PlanState` / `ValidateState`）**是 FACT 专用的**，不进骨架。各子图自带状态模型，形状对齐即可。

骨架持有：节点编排、两轮回边判定、工具预算、失败收束、计时诊断、`SubgraphResult` 组装。

预算基线沿用 `FACT` 现有上限（两轮、每轮 20 次工具调用、5 次查询、15 份新增证据）作为四类共同基线。`ROUTE` 不需要第二轮补搜时，其 `validate` 直接返回无缺口即可提前收束，**不为对称而强行跑满**。

`FACT` 迁移后，其 `SubgraphResult` 的 JSON 结构、字段、校验行为**必须与迁移前逐字节等价**（忽略 `run_id`、时间戳等天然变动项）。

## Scope

- `backend/verification/subgraphs/` 下的公共骨架
- `backend/verification/subgraphs/facts/` 改用骨架

## Acceptance

- `FACT` 对同一输入产出的 `SubgraphResult` 与迁移前一致，已有测试不改断言即可通过。
- 骨架能被四类复用；`ROUTE`/`CROWD`/`EXPERIENCE` 只需提供三个注入点。
- 诊断（`stage_timing`、`page_failure` 等既有格式）保持不变。
- 现有测试全绿。

## Notes

- 不要趁机改进 `FACT` 的提示词或判定质量，验收线是「输出不变」。
- 诊断不进入模型输入，这一约束保持。
- 未做第二个真实子图前，骨架只抽**确定共用**的部分；`ROUTE` 若形态差异太大（不需要浏览器），在 ticket 10 处理，不要为它预先抽象。
