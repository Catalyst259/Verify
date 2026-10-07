Blocked by: 02
Type: task
Status: open

# NominatimPlaceResolver 实现

## Goal

把 `TARGET_PLACE` 文字解析为具体 POI 坐标，接入 `place_resolver` 能力槽位。

## Why

`ROUTE` 要算路线就必须有起终点坐标；`CROWD`/`EXPERIENCE` 需要地点时区来对齐证据时间。目前 `resolved_place` 恒为 `None`，等于没有地点概念。

## Decisions

演示阶段用 `nominatim.openstreetmap.org`（`/search` 端点，`format=jsonv2`，`accept-language=zh-CN`）。

实测已确认：`上海迪士尼乐园`、`武康大楼`、`上海海昌海洋公园` 三条中文 POI 均可解析到正确坐标，返回 `type=theme_park` / `yes` 等分类。

硬约束（来自服务方使用条款，必须遵守）：

- **每秒最多 1 次请求**，脚本限单连接
- **必须带可识别应用的 `User-Agent`**（库的默认 UA 不够；冒充其他应用会被封）
- 尽量带有效 `Referer`
- 不要把服务 URL 硬编码进应用（写成可配置）
- 大流量站点禁止使用

实现要求：

- 限速：进程内 1 req/s 闸门
- 进程内缓存：同一查询不重复请求
- 解析失败或无结果时返回 `None`，**不得返回猜测坐标**
- 配置项走既有配置读取方式（`backend/config.example.toml` 补默认值），不要另起一套

## Scope

- 新来源实现
- 装配点接入 `place_resolver`
- 配置

## Acceptance

- `VerificationContext.resolved_place` 在解析成功时携带坐标与来源标识，失败时为 `None`。
- 连续两次相同查询只发一次网络请求。
- 请求频率不超过 1 req/s。
- User-Agent 可识别为本应用。
- 现有测试全绿；新增测试用替身 HTTP，不打真实网络。

## Notes

- 本轮只做单点解析，多候选择优（多个同名 POI）**不在范围**。
- 生产应换成高德/百度等供应商；实现必须隔离在能力槽位之后。
