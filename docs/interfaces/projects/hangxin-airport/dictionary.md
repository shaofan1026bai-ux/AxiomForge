# 航信机场：设备与测点

项目 ID：`hangxin-airport`。这里只登记本项目现场映射，共用字段含义引用系统字典。

| 现场设备名称 | 连接 ID | 系统对象 ID | 测点 ID/字段 | 含义 | 单位/换算 | 查询/控制用途 |
|---|---|---|---|---|---|---|
| 1#空调机组电表 | xiaomai-readonly | projectId=832，deviceId=12744 | wh171k_mspm03_totalpower | 总有功功率 | kW，不换算 | 功率趋势、统计及散点配对，只读 |
| 1#空调冷水机组 | xiaomai-readonly | projectId=832，deviceId=12788 | nfdh9q_mscrk01_c21 | 室外温度 | ℃，不换算 | 温度趋势、统计及散点配对，只读 |

现场 ID 不保证跨项目唯一。记录设备层级、质量标识、状态枚举差异和数据延迟；查询状态与控制指令的映射需要明确确认。

两条测点由用户指定，2026-11-06已实查归属及单位，并验证同次历史批量查询。两点来自不同设备，不以设备名称猜测一对一控制关系。完整字段及质量边界见[小麦测点](../../systems/xiaomai/query/points.md)与[历史数据](../../systems/xiaomai/query/history.md)。
