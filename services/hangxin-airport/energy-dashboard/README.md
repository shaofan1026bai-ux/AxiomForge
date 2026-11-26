# 芒市机场能耗看板后端与每日同步

此目录收录芒市机场能耗看板的 Node.js 后端，以及每天从小麦历史接口同步能耗和温度的 Python 作业。两者通过 SQLite 交换数据。后端只读看板数据库，每日同步负责写入。现场设备控制由同级的任务管理服务负责。

| 组件 | 入口 | 2026-11-26 线上来源 |
| --- | --- | --- |
| 看板 API 和静态页面托管 | `server/index.mjs` | `mangshi-airport-energy-dashboard:15c3ff0`，6 个 `server/*.mjs` 文件与看板源码仓库提交 `15c3ff0` 一致 |
| 小麦每日同步 | `scripts/sync_daily.py` | `mangshi-airport-energy-sync:df47ede-checkin3`，收录容器内运行版本及 3 个同镜像辅助脚本 |
| 同步镜像构建 | `Dockerfile.sync` | 与服务器部署目录文件和看板源码仓库提交 `df47ede` 一致 |

同步镜像里的 7 个 `axiomforge` 模块和本仓库导入时的 `d97a373` 逐文件一致。镜像构建从仓库现有 `src/axiomforge/` 取用这些模块。`import_region_temperature.py` 和 `sync_daily.py` 含看板 Git 提交之外的线上修订，以容器文件为准。`source.sha256` 记录这次导入的源文件和依赖版本，可在本目录用 `shasum -a 256 -c source.sha256` 校验。

## 后端

线上使用 Node.js 22.23.5，后端只依赖 Node.js 内置模块，包括 `node:sqlite`。默认监听 `127.0.0.1:4187`，可用 `BIND_HOST` 和 `PORT` 覆盖。接入 HTTPS 反向代理时，`APP_ORIGIN` 使用浏览器实际访问的 HTTPS 来源。`XIAOMAI_PUBLIC_KEY` 可选，用于配置小麦登录的 RSA 公钥。

`/api/auth/` 处理小麦登录和会话，`/api/energy` 和 `/api/region-temperature` 从 `.local/energy.sqlite` 读取数据。看板前端在独立项目构建。需要同时提供页面时，将那边生成的静态产物放入本目录的 `dist/`。

```bash
node --test server/*.test.mjs
node server/index.mjs
```

第一条命令使用模拟登录和临时 SQLite，适合无生产数据的本地验证。第二条命令启动本地后端。

## 每日同步

作业在北京时间每天 03:00 同步上一个完整自然日。进程会根据 `daily_sync_runs` 补齐已有记录之后的日期。同步失败时保留原数据，自动模式等待 15 分钟后重试。手动指定 `--date` 时只同步指定日期。

同步镜像使用 Python 3.12，依赖版本见 `Dockerfile.sync`。运行时用 `AXIOMFORGE_XIAOMAI_ENV_FILE` 指定外部凭据文件，字段沿用 `XIAOMAI_ACCOUNT`、`XIAOMAI_PASSWORD`、`XIAOMAI_LOGIN_URL`。线上数据库默认是 `/data/energy.sqlite`，可通过 `--database` 改用其他已初始化、可写的数据库。初始化能耗、区域温度、室外温度表的辅助脚本保留在 `scripts/`，历史文件应通过各脚本的参数指向外部存放位置。

在本目录只验证 Python 入口参数，可使用仓库锁定环境：

```bash
uv run --project ../../.. --locked python scripts/sync_daily.py --help
```

构建同步镜像时，`axiomforge` 上下文指向本仓库的模块目录，`wheels` 可指向单独准备的离线依赖目录。以下命令通过联网安装依赖来构建镜像。构建不会启动同步作业。

```bash
docker build -f Dockerfile.sync \
  --build-context axiomforge=../../../src/axiomforge \
  --build-context wheels=./scripts \
  -t mangshi-airport-energy-sync:local .
```
