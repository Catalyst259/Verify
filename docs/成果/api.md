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

`link` 只接受小红书图文笔记的 HTTP(S) URL 数组，包括 `xhslink.com` 分享短链。网页输入框支持从分享文案提取 URL，API 不接受整段分享文案。服务器复用已登录会话读取标题、正文和配图；来源均为 `LINK`，`source_ref` 保留输入 URL。原笔记的规范化 ID 会从独立取证中排除。视频、音频和评论不属于当前读取范围。

链接读取失败沿用 `{"detail":"说明"}` 响应，说明包含链接条目序号：非法输入或不支持的材料为 422，笔记失效或读取失败为 502，需登录或受到平台访问限制为 503，读取超时为 504。非法请求 DTO 沿用 FastAPI 的 422 字段错误格式。多链接中任一条或任一配图读取失败，本次不进入模型提取，不返回遗漏材料的成功结果。

提取时，`IMAGE.source_ref` 必须为本次上传的精确 `file_code`，`LINK.source_ref` 必须为本次提交的原始 URL；只有提交了非空文字才允许 `TEXT`，其 `source_ref` 为 null。模型收到本次材料标识清单，首次来源不合法时最多进行一次来源纠正。纠正只修改错误来源的类型和标识，保留主张、合法来源及 `source_text`，与首次提取共用原时间预算，不重新读取链接或启动 Agent。

纠正后仍出现未提交来源时返回 HTTP 502：`{"detail":"模型返回的材料来源与本次提交不一致，来源校验未通过；请重试"}`。其他 Agent 提取失败仍为 502，模型配置缺失为 503，提取超时为 504。成功响应结构保持不变。程序检查来源是否属于本次材料；多张图片之间的语义归属仍由视觉模型判断。

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
