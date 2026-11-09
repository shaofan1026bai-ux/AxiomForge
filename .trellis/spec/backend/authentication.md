# 小麦登录与工作台准入合同

## 1. 范围／触发

修改`auth.py`、`auth_socket.py`、main或登录集成前阅读。用户明确要求以小麦平台账号验证访问工作台；不得新增硬编码默认密码或以浏览器localStorage当鉴权。

## 2. 签名

`Session(token, account, expires_at, csrf)`，`sessions.create(account)`、`get(token)`、`revoke(token)`和`on_revoke(token, callback)`。`session_from_scope(scope)`读取HttpOnly Cookie。`install_auth(app)`注册登录／退出及ASGI中间件；`bind_client(client, session)`与`install_socket_guard()`绑定并保护NiceGUI事件。

## 3. 契约

GET /login原生HTML表单；POST /login只在服务端线程调用固定小麦HTTPS登录及项目832授权校验，密码不strip、不持久化，小麦token不返回页面。登录凭据与服务端查询凭据分开，后者仍来自现有外部配置。

会话为随机不透明token，内存保存、固定8小时有效；HttpOnly/SameSiteStrict Cookie，HTTPS下Secure。登录后旋转，退出POST撤销，重启全失效。登录/退出需要CSRF校验及同源检查；输入、频率和并发有限，失败不创建会话。业务响应no-store。

中间件不整体豁免/_nicegui，Socket入出包检查当前会话。NiceGUI事件还校验socket会话与client归属，复制client_id不能跨会话访问。Engine.IO后续轮询与WebSocket升级还要校验`sid`所属初始Cookie，防止合法B会话复制A传输sid读取队列。撤销清理对应客户端与等待中的View；后台已发请求可完成但不能发布迟到数据。HTTP长轮询在发送响应头与正文前重新验证会话，注销后不发送已排队数据；流式响应可被截断。退出操作自身仍返回303并清除Cookie。

## 4. 验证矩阵

| 输入／动作 | 结果 |
|---|---|
| 未登录GET / | 重定向/login且不生成业务client |
| 错误密码／无项目832权限／网络失败 | 脱敏中文错误，不创建会话 |
| 伪造／过期／注销cookie | 拒绝页面、下载和Socket |
| 不同会话client_id或复制的传输sid | 拒绝握手、事件、轮询和升级 |
| 轮询等待期间注销 | 返回401空响应，不发送旧队列数据 |
| 缺失或不匹配CSRF／跨站POST | 拒绝认证／退出操作 |
| 退出时存在后台查询 | 旧View失效，不发布结果 |

## 5. 基本／良好／错误案例

基本：使用本人有权限的小麦账号登录，手动查询再退出。良好：A退出后其他已开A页面不能重用连接；B不受影响。错误：只在页面显示登录遮罩，仍让未认证Socket触发服务端查询。

## 6. 必须测试

HTTP未认证／成功／错误／权限／CSRF／过期／退出、Cookie属性与不泄露凭据，Socket无会话／跨会话／注销重用、传输sid跨Cookie／注销期间排队HTTP与流式正文、正常委托框架处理，航班迟到快照不发布；全量ruff/format/mypy/pytest。浏览器验证表单、中文错误、导航、退出、刷新和响应式。

## 7. 错误与正确

错误：`localStorage.authenticated=true`或公开所有`/_nicegui`路径。正确：服务端查随机会话，业务client绑定会话，HTTP和Socket都强制检查；静态资源仅明确路径开放。
