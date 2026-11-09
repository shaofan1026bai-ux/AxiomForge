# 设备激活测点

- 接口 ID：`xiaomai.query.device-points.v1`
- 状态：2026-11-06 航信环境已验证。
- 方法／路径：`POST /api/GatewayMicro/Device/GetDevicePoint`。
- 鉴权：[用户登录](../auth.md)获取的Bearer Token。
- 副作用：只读查询。

## 请求

```json
{"deviceIdList": [12744, 12788], "isActivate": true}
```

设备ID必须来自当前授权项目。样例只适用于[航信现场映射](../../../projects/hangxin-airport/dictionary.md)。

## 响应

`success=true`、数值`code=200`，`data`为测点数组，主要字段：

| 字段 | 含义 |
|---|---|
| projectId、deviceId、deviceName | 归属项目、设备ID与名称 |
| pointId、identifierName、pointName | 完整测点ID、标识与名称 |
| unit、decimalPlace | 单位与展示精度 |
| value、updateTime | 查询返回的点值和更新时间，不能默认保证新鲜 |
| isWrite | 厂商字段的可写声明；不代表本工作台提供写入能力 |
| dataType、pointEnumItemList | 类型及状态枚举，数字含义以厂商字典为准 |

适配器应按完整 `pointId`校验项目、设备与单位，不依赖返回顺序或只搜索“温度”。上游返回的最新值与所查历史区间最后值属于不同口径，页面摘要必须明确自己的统计范围。

设备目录补充：`POST /api/GatewayMicro/Device/GetDevicePage`已实查，`data`含`pagingInfo`和`list`。按项目过滤使用`projectIdList`；单值`projectId`在实查返回中未成为有效筛选条件。现场曾返回198台设备、2911个激活测点，这只是当次目录样本，不写死为程序预期数量。
