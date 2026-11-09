# Journal - shaofan (Part 1)

> AI development session journal
> Started: 2026-11-04

---



## Session 1: NiceGUI Research Canvas style prototype

**Date**: 2026-11-04
**Task**: NiceGUI Research Canvas style prototype
**Branch**: `main`

### Summary

Implemented approved concept 3 with three project contexts, light/dark/system themes, synthetic charts, parameter forms, source inspection and local demo run records. Live integrations remain deferred.

### Git Commits

(No commits - planning session)

### Testing

- [OK] Ruff lint/format and mypy passed; pytest 5 passed; in-app browser navigation, project isolation, parameters, theme reload and responsive checks passed. Download verification was declined by browser permission gate; no bypass attempted.

### Status

[OK] **Completed**

### Next Steps

- Review local style prototype and choose the first real-data/interface workflow.


## Session 2: 按模块提交航信专题与算法流程图

**Date**: 2026-11-09
**Task**: 按模块提交航信专题与算法流程图
**Branch**: `main`

### Summary

用户授权后按小麦客户端、航班、功率温度、AHU回风温度、每日用电占比、算法流程图及工作台集成完成七个模块提交；归档流程图任务。

### Main Changes

- 全部功能代码、对应测试、规范及接口文档按模块提交；本地工具配置、缓存、外部凭据与临时产物未纳入。
- 仅归档当前算法流程图任务，保留其余任务原状态；未推送远程。

### Git Commits

| Hash | Message |
|------|---------|
| `3357e18` | (see git log) |
| `53ec401` | (see git log) |
| `ac5133e` | (see git log) |
| `231e245` | (see git log) |
| `b8e999c` | (see git log) |
| `275b8ee` | (see git log) |
| `0905578` | (see git log) |

### Testing

- [OK] Ruff、42文件格式检查、26源文件Mypy全部通过。
- [OK] 全项目273项测试通过；从HEAD独立导出源码再次273项通过。

### Status

[OK] **Completed**

### Next Steps

- 可按需要审阅七个功能提交后再推送远程。


## Session 3: 小麦登录与80端口HTTPS部署

**Date**: 2026-11-09
**Task**: 小麦登录与80端口HTTPS部署
**Branch**: `main`

### Summary

新增小麦平台身份验证、8小时服务端会话与HTTP/Socket保护；318测试和公网真实登录/退出/WS通过；部署123.60.61.161仍使用80端口可信HTTPS，保留白名单、原业务、数据与双源配置。

### Main Changes

- 登录原生表单、CSRF、会话撤销与跨会话传输校验
- d2ad455独立release上线，旧版本及SQLite备份保留
- HTTP/TLS同80分流与可信IP证书自动续期

### Git Commits

| Hash | Message |
|------|---------|
| `d2ad455` | (see git log) |
| `949127b` | (see git log) |

### Testing

- [OK] 318 passed；ruff/format/mypy28源及diff通过
- [OK] 公网HTTPS80登录/受保护页面/WS ack/退出及旧Cookie失效通过
- [OK] 浏览器服务器登录页、原业务200和PROXY非白名单403通过

### Status

[OK] **Completed**
