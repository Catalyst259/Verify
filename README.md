# 验一下 · 第一阶段

只实现「上传图片 → 提交地点、描述、多个图片和链接 → browser-use 提取 claim」。原生 HTML/JS 前端由 FastAPI 一并提供，无需 Node 构建或单独的前端服务。

## 代码分层

```text
backend/
  main.py                  # 组装依赖、初始化存储、挂载前端
  api.py                   # 两个 HTTP 接口及请求校验
  verification/
    service.py             # VerificationService：读取图片、调用 Agent、整理 claim
    models.py              # Claim、来源与提取结果
  storage/
    repository.py          # StorageRepository：SQLite 元数据与本地文件
    models.py              # 图片标识、MIME 与原始 bytes
    schema.sql             # Storage 自己的建表脚本
  agent.py                 # browser-use、多模态消息、模型协议与提示词
```

上传接口直接调用 Storage Repository；评估接口只调用 VerificationService。Service 通过注入的 Repository 取图，再调用注入的 Agent 提取函数；Agent 接收地点、描述、链接和图片，不依赖 HTTP 请求 DTO。当前直接使用具体 Repository，没有通用基类、额外 StorageService 或 Repository 转发层。

## 启动

在仓库根目录执行，使用 Python 3.12+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
pip install playwright
python -m playwright install chromium --no-shell
cp -n backend/config.example.toml backend/config.toml  # 保留已有配置
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 。Windows 激活命令为 `.venv\Scripts\Activate.ps1`。
上面的 Playwright 命令安装本地 Chromium；Linux 如果缺少浏览器系统库，执行 `python -m playwright install-deps chromium`。已有 Chrome/Chromium 时可以跳过浏览器安装，在配置中指定 `browser_executable_path`。

在 `backend/config.toml` 填写 `api_key`，按供应商设置 `base_url` 和 `model`。支持提供图片输入及 JSON Schema 输出的 OpenAI 兼容模型，也兼容 `deepseek-flash`（使用 JSON mode 后本地校验结构，并修正 browser-use 0.13.10 自动关闭 DeepSeek 视觉的旧判断）。`config.example.toml` 中密钥留空；实际配置文件已被 Git 忽略。程序依次读取 `config.local.toml`、`config.toml`、`config.example.toml` 中首个存在的文件。未配置密钥时可以上传图片，评估返回明确的 503 提示。

后端启动时按 `backend/storage/schema.sql` 自动创建 `backend/data/files.sqlite3`，图片保存在 `backend/data/uploads/`。SQLite 是嵌入式数据库，无需单独启动数据库进程。当前为本地单会话骨架，`order` 按上传递增；传入模型的图片顺序始终以本次 `image` 数组为准。

## 两个接口

- `POST /api/files`：multipart 字段 `file`。返回 `{ "file_code": "file_…", "file_id": "file_…", "mime_type": "image/png" }`。两个标识同值，保持已有接口兼容。
- `POST /api/verifications`：提交下方 JSON，同步等待并直接返回 `prompt.md` 规定的 `{ "target_place": "…", "claims": [...] }`。每张图不超过 10 MB，支持 PNG/JPEG/WebP/GIF；无 claim 时返回空数组。

```json
{
  "target_place": "上海迪士尼乐园",
  "text": "工作日上午不用排队",
  "link": ["https://example.com/post-a", "https://example.com/post-b"],
  "image": ["file_上传接口返回的标识"]
}
```

后端重新读取图片原始 bytes，按顺序将 `file_code` 标签与多模态图片内容送入模型；根目录 `prompt.md` 原文追加到 Agent 系统提示词。使用 browser-use 的结构化输出并检查来源标识，统一顺序编号。禁用搜索工具，按提示词只读取提交链接及其必要页面内容。登录墙或不可访问页面仍可能需要用户补充截图。

没有实现真实性核验、证据检索、报告、推荐、鉴权或任务队列。前端仅展示返回的 claim JSON。

## 检查

```bash
pip install -r backend/requirements-dev.txt
python -m pytest -q
# 可选：运行真实浏览器链路测试，模型 HTTP 响应使用本地测试替身。
VERIFY_BROWSER_TESTS=1 python -m pytest tests/test_browser.py -q
```

接口测试使用临时数据库，覆盖上传、落盘、重启恢复、图片顺序、四字段提交和错误状态。浏览器测试覆盖拖拽两张图片、无链接及多个链接、页面实际读取、系统提示词与多模态消息传递、claim 展示；如使用非默认 Chromium 路径，可设置 `VERIFY_CHROMIUM`。测试中的替身输出仅用于验证链路，不代表真实模型提取效果；正式运行始终调用配置的模型。

第一阶段曾使用真实 DeepSeek Flash，对临时的两张文字图片、两条测试网页及文字描述完成提取，返回 5 条 claim，覆盖四种类型。重构后的回归测试通过 API 与 VerificationService 验证持久化、材料顺序、结果整理和无效引用，并保留三组真实浏览器链路测试；删除旧字段迁移与重复异常枚举测试。测试不代表对任意网页或材料的提取质量评测。

接入参考：[browser-use Agent 参数](https://docs.browser-use.com/open-source/customize/agent/all-parameters)、[结构化输出](https://docs.browser-use.com/open-source/customize/agent/output-format)、[DeepSeek 图片输入](https://api-docs.deepseek.com/zh-cn/guides/vision/)、[DeepSeek JSON mode](https://api-docs.deepseek.com/guides/json_mode/)。
