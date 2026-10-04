# api
## 文件上传
POST /api/files
Content-Type: multipart/form-data
响应DTO:
```json
{
  "file_id": "string",
  "mime_type": "string"
}
```
## 核验任务创建
POST /api/verifications
Content-Type: application/json
请求DTO:
```json
{
  "target_place": "string",
  "text": "string",
  "link": ["string"],
  "image": ["string"]
}
```

同步执行后返回 `VerificationRun`，不再只返回提取结果。地点从顶层 `target_place` 移至 `context.target_place`，`claims` 仍在顶层。以下为空主张时的响应示例：

```json
{
  "run_id": "f87331f7c96e46c58d3b93967395a7fe",
  "context": {
    "target_place": "公园",
    "checked_at": "2026-10-05T00:00:00Z",
    "resolved_place": null
  },
  "claims": [],
  "subgraph_results": {},
  "status": "no_claims"
}
```

`subgraph_results` 按子图注册名称组织，每项包含 `graph_name`、`status`、`selected_claim_ids`、`findings`、`notes` 和 `error`。每条 `findings` 包含 `claim_id`、`summary` 和 `evidence`，证据包含 `source`、`content` 和可选的 `url`。

整体 `status` 描述执行情况：

- `no_claims`：没有提取到主张，未执行核验子图。
- `not_implemented`：核验功能尚未实现；默认四个子图目前均为占位实现。
- `completed`：核验流程完成，不代表主张为真。
- `partial`：子图状态混合，尚未全部完成或跳过。
- `failed`：所有已执行子图均失败，仍保留提取主张和子图错误。

流程返回运行结果时 HTTP 状态为 200，子图失败通过上述状态和结果表达；提取或共享上下文阶段抛出的异常仍使用 HTTP 错误响应。
