Blocked by: 02
Type: task
Status: open

# ValhallaRouting 实现

## Goal

实现 `map_routing` 能力槽位的真实实现，供 `ROUTE` 子图取实测时长与距离。

## Why

`ROUTE` 的判定本质是数值比对：主张「地铁步行 5 分钟」vs 实测时长。没有可信的路线数据，这个子图就只能靠猜。

## Decisions

演示阶段用 `valhalla1.openstreetmap.de`（FOSSGIS 托管的 Valhalla 服务）。三个端点：

- `/route`：单点路线，`costing` 支持 `pedestrian` / `auto` / `bicycle`
- `/sources_to_targets`：多点矩阵，一次问多组起终点
- `/isochrone`：等时圈，返回步行 N 分钟可达范围

实测已确认三个端点均可用，返回时长（秒）与距离（公里）。

硬约束（来自服务方使用条款，必须遵守）：

- **路由每秒最多 1 次请求**
- **脚本限单连接**
- 必须带可识别应用的 `User-Agent` 与 `Referer`
- **不要把服务 URL 硬编码进应用**（写成可配置）
- 条款禁止商用产品把该服务作为重要组成部分

**判定「步行 N 分钟」必须用等时圈或路线时长，不得用直线距离折算。** 直线距离会把「步行 5 分钟」误判为成立。

实现要求：

- 限速：进程内 1 req/s 闸门
- 进程内缓存
- 请求失败返回错误信息，不得返回伪造数值
- 配置项走既有配置读取方式

## Scope

- 新来源实现（`map_routing` 能力槽位）
- 装配点
- 配置

## Acceptance

- 单点路线返回时长与距离，带交通方式与单位。
- 多点矩阵一次返回多组结果。
- 等时圈能表达「步行 5 分钟可达范围」。
- 请求频率不超过 1 req/s。
- 现有测试全绿；新增测试用替身 HTTP，不打真实网络。

## Notes

- 生产必须换供应商（高德/百度/Google）。实现隔离在能力槽位之后，替换时不动子图。
- 演示阶段合规性已在 spec 的 Further Notes 记录。
