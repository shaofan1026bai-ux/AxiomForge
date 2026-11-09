# 芒市航班只读数据源

系统 ID：`mangshi-flights`。来源为用户提供的《航班查询参考包 v1.0.0》，连接 SurrealDB 的 `flight` 表。2026-11-04 在本场 `LUM` 完成只读实查；此验证仅覆盖当前源库，不代表其他机场或上游数据完整性。

- [航班查询与字段口径](query/flights.md)
- 实现：`src/axiomforge/flight_source.py`、`flights.py`。
- SDK：`surrealdb==2.0.0`；认证配置由服务端外部文件提供，不随文档发布。
- 当前无写入、设备控制、自动轮询或定时任务。
