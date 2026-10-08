Type: task
Status: open

# 证据源在单次运行内按查询记忆化

## Goal

同一次核验内，相同查询的材料只抓一次。

## Why

主图向所有子图广播完整 claim 列表，各子图自行选择。「周末人少」「适合日落」会同时被 `CROWD` 和 `EXPERIENCE` 捞到，两者都要去小红书搜同一批关键词。`xiaohongshu` 是串行访问 + 4 秒 pacing，默认最多 10 条笔记——重复抓取直接把整次核验的时间成本翻倍甚至三倍。

## Decisions

- 记忆化**限定在单次运行内**，不跨运行持久化（证据有时效性，`retrieved_at` 会变）。
- 缓存键基于查询文本与来源标识；同一来源同一查询命中即返回已抓材料。
- 缓存对 `EvidenceSource` 透明，子图无感知。放在来源实现之外的一层包装里，不要改每个来源。

## Scope

- 证据源包装层
- `backend/verification/capabilities.py` 的装配点

## Acceptance

- 同一次运行内，两个子图对同一来源发起相同查询时，底层来源只被调用一次，两者都拿到材料。
- 不同运行之间不共享缓存。
- 缓存返回的材料带正确的 `retrieved_at`，不因为复用而丢失时间戳语义。
- 现有测试全绿。

## Notes

- 不要做成通用缓存框架，一个 dict 够用。
- 空结果（查询无命中）也应被记忆，否则「无结果」会被反复重查。

## Resolution: 不做（wontfix）

实现过程中发现本 ticket 的前提不成立，按「这东西需要存在吗」的第一性判断撤销。

### 事实

`EvidenceSource` Protocol 声明的是：

```python
async def search(self, query: str) -> list[Evidence]: ...
```

但仓库里唯一真正走证据源、且代价最高的 `XiaohongshuSource`，签名是：

```python
async def search(self, query: str, *, execute: Execute | None = None, ...) -> None
```

它通过 `execute` 回调把证据写进取证会话，**没有返回值可缓存**，也不接受 Protocol 形状的调用。因此：

- 包装 `search(query) -> list[Evidence]` 根本包不住小红书；
- `web_search` 是会 raise 的占位；
- 当前没有任何来源真正走 `search(query) -> list[Evidence]` 这条路。

### 结论

记忆化在这层没有目标。要做就得改为「把缓存的证据重放进新会话并重发 evidence_id」，那是另一套设计，复杂度远超收益。当前代价最高的是小红书串行 + pacing，若后续实测证明重复抓取确实是瓶颈，应直接在 `XiaohongshuSource` 内部按 query 缓存笔记，而不是在能力层包装。

### 已验证

尝试过实现（`MemoizedSource` 包装 + `service.run()` per-run 装配），导致 5 个既有测试失败：既破坏了来源对象身份语义，也无法适配小红书签名。改动已完整回退，套件恢复 224 passed / 36 skipped 基线。
