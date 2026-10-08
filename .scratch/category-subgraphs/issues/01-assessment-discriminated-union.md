Type: task
Status: open

# 判定结构：Assessment 基类 + 判别联合

## Goal

让 `ClaimFinding` 能装下四类子图各自的判定结构，且 `model_validate` 从 dict 还原时不丢子类字段。

## Why

`CLAIMTYPE` 四类的判定语义不同（真假 / 数值比对 / 场景条件 / 体验一致性），但目前 `ClaimFinding.assessment` 硬编码为 `FactAssessment`。另外三个子图无处安放判定。

## Decisions（已定，不要重新设计）

`verification/graph.py` 与 `tests/test_frontend_results.py` 都真实走 `model_validate` 从 dict 还原这条路。原型实测结论：

```text
assessment: Assessment | None                序列化丢子类字段
assessment: SerializeAsAny[Assessment | None]  反序列化被 extra="forbid" 拒绝
assessment: Annotated[A | B | C, Field(discriminator="kind")]   双向都保住 ✓
```

**采用带 `kind` 字面量字段的判别联合。**

`Assessment` 基类承载四类共用字段：`target`、`time_scope`、`verdict`、`confidence`、`evidence_sufficient`、`reason`、`conditions`、`supporting_evidence`、`counter_evidence`、`context_evidence`。`verdict` 在基类是 `NonEmptyText`，子类收窄为各自的字面量。

`FactAssessment` 继承 `Assessment`，追加 `dimensions`、`remaining_gaps`，`verdict` 收窄为 `FactVerdict`。**既有字段名、校验规则（`consistent_verdict`）、JSON 结构一律不变。**

`kind` 字面量：`fact` / `route` / `crowd` / `experience`。

## Scope

- `backend/verification/models.py`
- 受影响的导入方与测试

## Acceptance

- `FactAssessment` 的 JSON 序列化输出与改动前完全一致（含 `dimensions`、`remaining_gaps`）。
- `SubgraphResult.model_validate(result.model_dump())` 往返不丢任何字段。
- 现有测试全绿，不需要修改断言。
- `tests/test_fact_state.py` 中的 `FactAssessment` 校验行为不变。

## Notes

- `NonEmptyText`、`FactScore` 等已有别名不要改名。
- `FactSourceType` 仍是 `Literal["OFFICIAL","CTRIP_PRODUCT","WEB"]`，本轮不动。
- 未完成的 grilling 项：四类 verdict 的最终措辞可能调整，但**结构**以此为准。
