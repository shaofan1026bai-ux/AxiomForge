# 小麦用户登录

- 接口 ID：`xiaomai.auth.login.v1`
- 状态：2026-11-06 航信环境已验证。
- 方法／路径：`POST /api/UserMicro/User/UserLogin`。
- Content-Type：`application/json`。

## 请求与加密

```json
{
  "account": "<ACCOUNT>",
  "pwd": "<RSA_CIPHERTEXT_BASE64>",
  "loginClient": 0,
  "loginWay": 0
}
```

`pwd`由原始密码经公开RSA公钥、PKCS#1 v1.5填充加密后Base64编码。参考代码使用JSEncrypt；本地Python适配应保持协议相容。公钥来自用户提供的参考包，可用公开配置覆盖以适应轮换；专题数据源账号密码从服务端外部配置读取；工作台登录入口另接受用户本次表单输入，在服务端按同一协议验证账号及项目832权限，输入密码不写入配置或持久状态。两种用途不互相替代，不把已有示例密文当成可长期复用的凭据。

## 成功响应

业务信封为 `success=true`、数值 `code=200`，`data`含正整数 `userId`、`companyId`。响应头 `Access-Token`提供用户Token；HTTP头名称不区分大小写。后续请求使用：

```http
Authorization: Bearer <TOKEN>
```

Token获取后还要查询授权项目，不能仅凭登录成功认定可访问任意设备。原始响应还可能含个人资料，本项目不保存或显示整份登录响应。

## 生命周期与错误

当前专题每次手动查询重新登录，Token仅用于该次服务端调用，不持久化、不进入前端或CSV。过期与续期协议尚待确认，不猜测有效期，不复制参考项目的应用Cookie方案。

密码错误、账号受限、网络／HTTP失败、无Token或响应结构异常均阻止后续数据查询；错误正文及完整底层异常不直接转发到UI。禁用跨源重定向，避免凭据被带到其他地址。

工作台准入使用独立8小时随机会话，不复用小麦Token作为浏览器Cookie；退出或服务重启后失效。HTTP、Socket及导出由工作台会话保护，详见[登录准入合同](../../../../.trellis/spec/backend/authentication.md)。
