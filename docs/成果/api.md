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
