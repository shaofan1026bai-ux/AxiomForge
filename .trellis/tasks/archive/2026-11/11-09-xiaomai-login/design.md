# 登录访问边界设计

原生服务端HTML登录表单与现有NiceGUI工作台共享应用，POST /login执行后台线程小麦认证。复用XiaomaiClient和Settings，固定HTTPS域名、公钥与项目832授权校验，不对密码作strip。表单有CSRF随机cookie/hidden token及同源检查；用户输入长度与并发/失败频率受控，错误脱敏。

服务端内存SessionStore保存随机不透明token的会话，8小时固定过期；cookie仅携带随机token，HttpOnly、SameSite=Strict、HTTPS下Secure。认证成功旋转会话，失败不创建；退出POST服务端撤销并清cookie。重启内存清空。无明文密码或上游token持久化。

ASGI中间件保护所有业务HTTP和Socket，只有登录页与精确静态资源路径公开；不能整体豁免/_nicegui。WebSocket入/出包均检查当前会话。NiceGUI客户端ID绑定到服务端会话；事件/握手检查发起socket及目标client归属，防复制client_id跨会话。撤销会话清理已注册客户端，取消等待中的View查询，阻止迟到发布。网页缓存no-store。

main安装鉴权并注册GET/POST登录/退出，根页面绑定请求会话，顶部账号/退出按钮用原生POST表单携带CSRF。不会将密码填入NiceGUI元素状态。既有数据源继续服务端配置，不把登录凭据写到数据源或浏览器。

回滚移除main鉴权安装及workbench绑定/退出UI，删除独立auth模块和测试。无数据库迁移。

## 既有服务器更新

沿用/opt/axiomforge/releases/<commit>及current软链接、axiomforge.service、127.0.0.1:18081。原配置保留于/etc/axiomforge，SQLite保留/var/lib/axiomforge。先构建新release再原子切换，失败回滚软链接；旧release保留。Nginx原IP站点仅对既有来源放行；通过IP可信证书HTTPS保护登录，保留/charts/、/plc-points/。证书与更新工具隔离既有站点，续期自动化及真实TLS验证另记录。

用户要求保留80后，采用Nginx既有stream ssl_preread分流HTTP与TLS到loopback18083/18443；使用PROXY protocol+realip保持白名单。工作台HTTP跳https://IP:80，其他HTTP业务和ACME保留。不使用443，证书可信校验、续期、匿名拦截及真实Socket链路均通过。
