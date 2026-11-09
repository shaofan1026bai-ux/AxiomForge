# Design Index

Status: the selected concept and refined themes were approved. The user authorized style implementation; the first local prototype is implemented. Live integrations remain future scope.

- [Architecture and boundaries](../../../docs/architecture/workbench.md)
- [Modules and screens](../../../docs/architecture/workbench-ui.md)
- [Requirements](prd.md)
- [Implementation plan](implement.md)

Architecture owns data/execution contracts; screen design owns navigation and page composition. PRD owns user scope.

Existing interface documentation remains in place. NiceGUI supersedes the earlier tentative React recommendation. Separate presentation and domain Python packages preserve boundaries without a separate frontend service.

Concept images are synthetic visual explorations. Actual browser screenshots and scope limits are linked from the screen design. No database, live connector, training or scheduler has been created.

## 项目入口与偏好恢复（2026-11-04）

首次请求以未选择项目的概览状态构建界面。连接浏览器后恢复主题和 `axiomforge.project`，校验项目 ID 后经 `enter_project` 进入默认设备的分析结果页；无效或不可读取的缓存保留概览。卡片入口与顶部选择器复用同一切换逻辑，先确认未应用修改，再切换项目、重置分析状态并写入缓存。取消切换只恢复选择器，不改变页面、草稿或缓存。浏览器存储失败不影响会话内选择；项目偏好跨页面刷新和服务重启保留，分析参数、筛选和运行记录不持久化。
