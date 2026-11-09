# 航信机场

- 项目 ID：`hangxin-airport`
- 文档状态：航班查询、小麦目标测点及历史只读查询已验证，其余待整理
- 已验证范围：芒市LUM航班只读查询，Asia/Shanghai，来源《航班查询参考包 v1.0.0》，核对日期2026-11-04。

## 接入系统

| 系统 ID/版本 | 系统文档链接 | 连接 ID | 使用范围（查询/控制） | 平台项目/租户 ID |
|---|---|---|---|---|
| mangshi-flights/v1 | [芒市航班](../../systems/mangshi-flights/README.md) | flight-readonly | 查询 | 本场 LUM |
| xiaomai/v1 | [小麦设备与测点](../../systems/xiaomai/README.md) | xiaomai-readonly | 查询 | projectId=832，芒市机场 |

## 现场资料

- [连接与环境](connections.md)
- [设备与测点](dictionary.md)
- [规则与参数](rules.md)

共用接口定义引用 [系统目录](../../systems/README.md)。尚未确认这些项目是否使用相同接口协议，不预设系统类型或能力。
