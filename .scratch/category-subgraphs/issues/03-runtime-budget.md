Type: task
Status: open

# 全局浏览器并发预算 + 运行级总超时

## Goal

四条子图并行时，浏览器取证并发不随子图数量线性增长，且尾部有总超时兜住。

## Why

`facts/graph.py` 的 `search` 节点用 `asyncio.Semaphore(3)`，一次调用开一个 browser-use 浏览器。四条子图由主图 `Send` 并行广播，叠起来会是十几个 Chromium 进程。`xiaohongshu` 还必须串行访问。Windows 上会耗尽资源，且一条慢子图会把其他子图饿死到各自超时。

## Decisions

- 浏览器取证槽位设**全局上限**，四条子图共享，不随子图数量增长。
- 另设**运行级总超时**，早于各子图自身的硬超时，兜住尾部。
- 超时的子图必须记为「执行失败」，**不得伪装成「证据不足」**——这两个状态对用户含义不同。
- 单个子图内部的现有预算（两轮、每轮 20 次工具调用、5 次查询、15 份新增证据）保持不变。

## Scope

- 并发槽位与超时的持有位置（运行时能力容器或等价位置）
- `backend/verification/subgraphs/facts/graph.py` 的 `search` 节点改为使用全局槽位
- `backend/verification/graph.py` 的子图执行超时

## Acceptance

- N 条子图并行时，浏览器并发不超过配置上限，与子图数量无关。
- 一条子图阻塞不导致其他子图全部超时。
- 超时子图在 `SubgraphResult.status` 中体现为 `failed` 且 `error` 说明原因，不是 `completed` + 证据不足。
- 现有测试全绿。

## Notes

- 上限值取保守值；若后续实测需要调整，改配置常量即可。
- 不要为「公平性」引入复杂的调度器，一个全局信号量够用。
