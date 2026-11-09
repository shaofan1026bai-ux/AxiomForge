# 当前用户授权项目

- 接口 ID：`xiaomai.query.owned-projects.v1`
- 状态：2026-11-06 航信环境已验证。
- 方法／路径：`POST /api/UserMicro/UserSubarea/GetUserOwnProject`。
- 鉴权：[用户登录](../auth.md)获取的Bearer Token。
- 副作用：只读查询。

## 请求

```json
{"companyId": 49, "userId": 12345}
```

`companyId`与`userId`都从本次成功登录响应读取；这里49为当前环境公司样本，12345为脱敏用户示例，均不以文档常量替代实际身份。

## 响应字段

`success=true`、数值`code=200`时，`data`为数组。航信样本含项目 `projectId=832`、`projectName=芒市机场`、`subareaId=985`；`treePath`、地址、图片等不是授权依据。

空列表是成功但无授权项目；目标项目不在列表时拒绝该专题取数。结构异常、项目ID无效和失败信封均作为查询失败，不默认授予本地所有项目。
