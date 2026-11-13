# 航信 AHU 二氧化碳与启停契约

## 范围

适用于 `ahu_co2.py`、`ahu_co2_source.py`、`ahu_co2_report.py` 与航信项目专用视图。共用 [AHU 回风合同](ahu-temperature.md) 与 [航班合同](flights.md)。

## 接口

- `load_ahu_co2(device_id, day, *, cancelled=...) -> DaySnapshot`。
- `load_ahu_co2_range(start_day, end_day, *, cancelled=..., progress=...) -> RangeSnapshot`。按日起止包含，批查五台，进度在完成一天的设备查询后更新。
- `report_for(snapshot, threshold) -> Report` 提供五台汇总及事件记录。单日快照保存点击事件后所用的图表与航班数据。
- `points_for(device) -> tuple[PointSpec, ...]` 返回同一设备六点；`events_for(samples, threshold) -> tuple[Event, ...]` 使用同台设备按时间排序的采样。
- `research_windows(flight_snapshot, device_id) -> tuple[FlightWindow, ...]` 返回合并后的半开参考窗。

## 数据契约

复用小麦认证、项目归属、历史身份校验和分钟序列解码。设备映射与窗口规则见[接口说明](../../../docs/interfaces/systems/xiaomai/query/ahu-co2.md)。不同时间字段可各自缺失。开机时 `Event.time` 是首次确认运行的采样时刻，`previous_stopped` 是此前最后确认停机时刻，`sample` 是此前有效 CO₂ 的实际采样。高值停机段以峰值采样作代表，保留 `start/end/count`。

## 边界和失败

输入限 AHU1～5、今天或更早的北京时间日期；范围最多 31 天，当天截到最近完整分钟。任一小麦调用失败时抛脱敏 `AhuCo2SourceError`，禁止用空序列代替失败。航班源失败时发布真实 AHU 历史并保留 `flight_error`。查询取消不发布快照。

## 场景

典型查询一次对照五台近七日的浓度分布，点选事件核对首个运行反馈、最后停机时间和浓度。对照阈值可本地修改。按设备连续判定跨午夜事件，按代表时刻关联当日图表和参考窗。没有有效配对时保留事件及缺测提示，分布样本数只统计有有效浓度的开机。

## 验证

覆盖未知网格、冲突状态、同刻证据、前置浓度回溯、点位串台拒绝、航班失败降级、延误锚点与设备研究范围。真实样本只记录聚合值和少量事件证据。

## 证据边界

`v56` 是 CO₂ 控制器设定值，`v20` 与同刻有效频率用来确认实际运行。真实启机触发规则需要控制逻辑或现场日志佐证。
