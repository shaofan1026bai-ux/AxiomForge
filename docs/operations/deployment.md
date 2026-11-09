# AxiomForge 部署与登录入口

## 当前版本与访问

2026-11-09应用版本`d2ad455`，公网入口为`https://123.60.61.161:80/`；仍只用80端口。原`http://123.60.61.161/`的工作台请求自动308跳转到同端口HTTPS。小麦平台密码只通过可信TLS连接提交，未登录无法访问业务页面、Socket或导出。

来源IP白名单保留在服务器Nginx配置；没有扩大工作台匿名公网访问。`/charts/`、`/plc-points/`及默认HTTP站点保持原响应。不要取消来源限制，也不要把登录退回公网明文HTTP。

## 应用发布与数据

- 专用服务：`axiomforge.service`，启用开机自启，运行用户`axiomforge`。
- 代码：`/opt/axiomforge/releases/<commit>`，`/opt/axiomforge/current`软链接指向生效版本；本次保留上一版本483046d。
- 内部应用仅监听`127.0.0.1:18081`。候选验收临时18082已停。
- 双源配置保留`/etc/axiomforge/{flight,xiaomai}.env`，不放入源码或浏览器。
- 流程图SQLite保留`/var/lib/axiomforge/algorithm-flowcharts.sqlite3`。升级前通过SQLite backup API备份至`/var/backups/axiomforge/`，不复制正在写入的数据库文件。
- 新建独立release并通过导入、候选服务、匿名保护和真实平台认证后才原子切换current。应用检查失败时自动指回旧目录并重启。
- 本次锁文件无依赖变化，服务器网络慢；新venv复用同一Python3.12旧版本已锁定site-packages，再用`uv sync --frozen --no-dev --offline`重新安装新release可编辑包，已确认模块从新release导入。未复用旧console入口或修改旧release。

## 同端口HTTP／HTTPS

Nginx既有stream模块以`ssl_preread`判断80端口连接协议：普通HTTP转loopback18083，TLS转loopback18443。公开监听仍为80；443监听和临时新增的443防火墙规则已移除。

前端stream使用PROXY protocol传递原IP。两个后端仅监听loopback，并设置可信loopback及`real_ip_header proxy_protocol`恢复真实来源，否则会把所有访问误认为本机并破坏白名单。默认站点、原业务、ACME验证仍经过HTTP后端；只有工作台HTTP入口转同端口HTTPS。

证书为Let's Encrypt可信IP证书，SAN包含部署IP，未使用自签名或关闭证书验证。Certbot5.9隔离于`/opt/axiomforge/tls-venv`及`/etc/axiomforge/tls`，不替换系统旧Certbot，也不影响其他证书。`axiomforge-tls-renew.timer`每12小时检查，成功续期后先`nginx -t`再reload。IP证书有效期较短，不能禁用此timer。

部署细节与完整回滚备份在服务器：`/opt/axiomforge/deploy/port80-mux-20261109.md`及`port80-mux-20261109T103153Z/`。网络配置回滚应整体恢复nginx.conf、default和mcp_service，先测试再reload；不能只删stream而留下proxy_protocol后端。应用和网络回滚独立，回退应用时仍保留HTTPS保护。

## 验证证据

- 全量318测试、ruff/check+format、mypy28源文件、diff通过；wheel包含登录模板。
- 公网HTTPS80真实CA验证、首页303、登录200、匿名下载和Socket401、no-store通过。
- 真实小麦登录303、工作台200、Secure+HttpOnly Cookie、合法绑定Socket握手及ack事件通过。
- 退出303、复制旧Cookie无效、首页重新拦截和匿名数据401通过。
- 原HTTP业务与默认站点仍200，ACME HTTP文件内容一致；模拟非白名单PROXY来源在两后端均403。
- 浏览器实际打开服务器/login，无证书警告。截图见`docs/architecture/previews/login-20261109/server-login-port80.jpg`。
