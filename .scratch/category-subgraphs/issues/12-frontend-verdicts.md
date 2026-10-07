Blocked by: 08, 09, 10
Type: task
Status: open

# 前端展示四类子图的判定词表

## Goal

前端能正确展示四类主张各自的判定语义、证据与冲突标记。

## Why

判定词表从「真假四档」变成四套不同语义。如果前端写死 `SUPPORTED/CONTRADICTED/...`，`ROUTE` 的「数值冲突」和 `EXPERIENCE` 的「存在明显分化」会显示成空白或错位，等于白做后端。

## Decisions

- 保持「结论 → 理由 → 证据」三级结构，用户先看最影响决策的问题，再按需展开来源、发布时间、场景条件、原始证据。
- 四类各自的词表都要能正确显示，不做强行统一的标签。
- `ROUTE` 的主张时长 / 实测时长 / 距离 / 交通方式要能直接对比展示。
- 冲突标记（ticket 11）要能显示「哪两条结论打架」。
- 证据不足要明确表达为「证据不足」，不得暗示「通过」。
- 仅扩展判定词表的展示，**不改交互与布局**（Out of Scope）。

## Scope

- `frontend/`

## Acceptance

- 四类判定词表各自正确显示，无空白、无错位、无误映射。
- `ROUTE` 的数值对比可读。
- 冲突标记可见。
- 证据不足显示明确。
- 现有前端测试全绿（`tests/test_frontend_*.py`）。

## Notes

- 前端测试走 `VerificationRun.model_validate(...)` 注入，不打真实后端（`tests/test_frontend_results.py` 是先例）。
- 不引入前端构建链，保持原生 HTML/JS 由 FastAPI 直接挂载。
