# 实施计划

- [x] SessionStore、小麦身份验证、登录/退出原生页面、CSRF/频率控制和HTTP/WS中间件。
- [x] NiceGUI client绑定和Socket事件校验、撤销清理；main/workbench集成。
- [x] 测试无登录、伪造/过期/注销cookie、跨会话client、登录失败/无权限、CSRF、下载和Socket重用。
- [x] 全量ruff、format、mypy、pytest及diff检查。
- [x] 浏览器登录/失败/导航/退出/刷新/主题布局验收；保留外部配置后重启。
- [x] 更新代码规范、README和验收记录；不提交无关路径。

- [x] 以本任务提交创建独立服务器release，保留数据/配置，切换服务并回归HTTP/Socket登录保护。
- [x] 可信IP证书与HTTPS入口、原白名单和其他业务路由、续期timer检查。

用户选择保留80端口：已完成HTTP/TLS同端口分流，无需云侧443；公网真实登录、Socket与退出、原业务路由和证书续期检查通过。
