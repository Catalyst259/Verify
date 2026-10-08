Type: task
Status: open

# 能力槽位：MapRouting / PlaceResolver + 泛化模型与搜索注入

## Goal

把运行时能力容器扩展到能支撑三条新子图，并把 `FACT` 专用的注入点泛化为共用形状。

## Why

`VerificationCapabilities` 目前有 `place_resolver`、`evidence_sources`、`fact_llm`、`fact_search`。后两者名字与形状都是 `FACT` 专用，三个新子图没法用；`map_routing` 完全缺失，而 `ROUTE` 需要它。

## Decisions

**只扩展现有的 `VerificationCapabilities`，不新增注入机制、不新增 seam。**

新增/调整：

1. `map_routing`：抽象「坐标 → 时间/距离」。需要三个能力：
   - 单点路线（origin、destination、costing → 时长、距离）
   - 多点矩阵（sources、targets、costing → 时长、距离矩阵）
   - 步行可达圈（origin、costing、时间阈值 → 可达范围），用于判「步行 N 分钟」
   返回结构里必须带 `costing`（交通方式）与单位，不能只有裸数字。

2. `place_resolver` 已存在，本轮只补类型与契约，不接实现（实现见 ticket 06）。

3. `fact_llm` / `fact_search` 泛化为各子图共用形状。**保持 `FACT` 现有调用不改行为**；泛化后 `FACT` 的能力槽位应能指向同一实现。

`PlaceReference` 现在只有 `reference`。`ROUTE` 需要坐标，本轮扩展为可携带经纬度与来源标识；`resolved_place` 为 `None` 时不得视为已确认具体 POI（既有约定，保持）。

## Scope

- `backend/verification/capabilities.py`
- `backend/verification/models.py`（`PlaceReference`）
- `backend/main.py` 的装配点

## Acceptance

- `VerificationCapabilities` 能同时容纳地图能力与既有来源，且不破坏 `FACT` 现有注入。
- 三个新子图可以通过 `runtime.context` 取到地图能力。
- 现有测试全绿。
- 不引入新的顶层注入机制；`map_routing` 只是既有容器里的一个字段。

## Notes

- 本轮**不接真实供应商**（ticket 06 / 07）。
- 接口形状按「坐标进、时间/距离出」设计，与具体供应商无关，上线可整体替换实现。
