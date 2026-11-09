# AxiomForge
A personal AI-powered workbench for building rules, training models, and orchestrating algorithms.

## Run Locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run axiomforge
```

Open http://127.0.0.1:8080 in your browser. To use another port:

```bash
uv run axiomforge --port 8081
```

The service listens only on loopback. Stop it with Ctrl+C. 首次打开显示项目概览，点击项目卡片进入项目；航信默认进入真实分析，华师大和大润发默认进入项目概览。之后自动恢复该浏览器上次选择的项目，也可从顶部选择器或项目概览切换。项目选择和主题偏好保存在浏览器本地存储中；演示筛选、参数及运行记录属于当前客户端，刷新后重置。登录使用有芒市机场项目权限的小麦平台账号和密码。

## 登录与访问

打开首页会先进入登录页，使用小麦平台账号和密码验证，并核验芒市机场（项目832）权限。没有独立默认管理员密码，也不会把服务端数据源密码自动填入页面。认证期间数据源不可达时登录会失败，请恢复网络后重试。

登录会话仅保存于当前服务进程，浏览器只持有HttpOnly随机会话Cookie，固定8小时失效；退出或重启服务后需要重新登录。密码和小麦Token不写入浏览器存储或会话文件。工作台右上角显示当前账号并提供“退出”。未登录时页面、数据事件和导出受服务端保护。

登录账户用于工作台准入；现场数据查询继续使用原有服务端小麦／航班外部只读配置，两个启动参数仍需保留。不提供注册、修改密码或角色管理。

## Current Scope

The first NiceGUI style prototype implements the selected Research Canvas layout, light/dark/system themes, three project contexts, module navigation and an interactive synthetic power/temperature analysis. Date/device filters, parameter adjustments, analysis records, source inspection and CSV export work locally.

航信项目的“功率与温度分析”接入小麦真实测点，支持手动时间查询、摘要、双轴趋势、相关性散点、分析选区、数据明细和CSV导出。功率与温度专题仅在航信项目显示；华师大和大润发的数据分析暂为待接入状态。“今日航班”专栏支持 SurrealDB 只读查询、进出港双栏、搜索和分页、计划／动态时刻及长间隔时间带。模型训练、设备控制和定时任务尚未实现；算法修改仍通过 Codex 完成。

小麦数据源通过独立外部配置启用，配置不提交到仓库：

```bash
uv run axiomforge --xiaomai-env-file /path/to/xiaomai.env
```

配置文件包含 `XIAOMAI_ACCOUNT`、`XIAOMAI_PASSWORD` 和 `XIAOMAI_LOGIN_URL`；航信登录地址为 `https://smart-energy.yntravelsky.net.cn:36443/api/UserMicro/User/UserLogin`。也可通过 `AXIOMFORGE_XIAOMAI_ENV_FILE` 指定文件；显式进程环境优先。密码填写原始值，程序在服务端完成RSA加密，Token只在单次查询中使用。

选择“航信机场 → 功率与温度分析”，点击“查询刷新”。默认查询今天，截至提交时刻；支持昨天和自定义，最长连续7天，固定上游2分钟数据，分段查询后统一配对。源为“1#空调机组电表—总有功功率”和“1#空调冷水机组—室外温度”。不自动轮询；快照属于当前浏览器，刷新页面需重新查询。CSV导出完整成功查询范围，不随图表选区删减；缺失值不填0，相关性不代表因果或节能空间。详见[接口说明](docs/interfaces/systems/xiaomai/README.md)。

### AHU 回风温度专题

航信项目的“AHU 回风温度”使用同一小麦只读配置，比较 AHU1～5 的温度范围、P05～P95、样本平均及温区采样占比，并提供逐日统计、设备明细、数据质量和 CSV 导出。

默认近 7 个完整日、全天；可指定日期范围并开启每日固定时段。跨午夜按窗口开始日期归属，最后一天的窗口延伸至次日。例如选择 10 月 1～3 日、22:00～次日 06:00，最后窗口结束于 10 月 4 日 06:00。只接受已经结束的完整窗口，不自动截断未来部分。

浏览器记住最近一次合法提交的查询条件；相对预设重新打开时按北京时间滚动，自定义日期保持不变。恢复条件不自动取数，点击“查询统计”后才访问数据源。数据快照仅保留在当前客户端对应的服务端内存，浏览器刷新后需要重新查询。

采用 1 分钟查询网格，开机需同刻运行反馈为 1 且频率大于 0、不超过 60 Hz；未知运行状态不归为停机。缺测不补值，主要范围和温区比例描述有效采样，不代表精确持续时间。该口径独立于功率专题的 2 分钟查询。

最多支持 62 个窗口开始日期，单次查询总截止为 15 分钟，可点击“停止等待”。39 日、五台 15 点的真实查询实测约 84 秒；实际耗时取决于数据源和网络。停止或任一分片失败不发布部分总体统计。接口与统计规则见[AHU口径说明](docs/interfaces/systems/xiaomai/query/ahu-temperature.md)。

### 每日用电占比专题

航信项目增加“每日用电占比”，沿用同一小麦只读配置。日期按北京时间自然日计算，起止日期均包含，默认近7个完整日，最多62日；手动查询，记住最近合法提交的日期条件，恢复不自动取数。

固定统计美的空调1～5、三菱外机、冷水主机总表及AHU总表共八项（展开为13块物理电表）。占比的分母为当天八项合计，不代表机场总用电。累计读数按日界差计算，精确午夜优先，仅允许两侧包围且总间隔不超过5分钟的短时对齐。

缺测日仍显示其他项已知电量，但所有占比留空；真实0保留为0。区间汇总分别展示“区间累计差”和“可计算日合计”，不会将跨日缺口均分。图例仅高亮，不改变分母。支持每日电量／占比、选中日明细、边界质量与完整范围CSV导出。详见[每日用电统计口径](docs/interfaces/systems/xiaomai/query/energy-share.md)。

### 航班数据源

航班连接仍需单独启用，凭据不复制进仓库：

```bash
uv run axiomforge --flight-env-file /path/to/project.env
```

选择“航信机场 → 今日航班”，点击“查询刷新”。连接字段沿用参考包的 `SURREALDB_*`，也可通过 `AXIOMFORGE_FLIGHT_ENV_FILE` 指定文件。航班快照仅保存在当前浏览器对应的服务端内存，刷新后重新查询；无自动轮询。间隔阈值默认30分钟，可调整；空档仍须扣除保障时间才能用于后续策略评估，当前不执行启停。详见[航班数据口径](docs/interfaces/systems/mangshi-flights/query/flights.md)。

两个专题同时使用时，启动命令必须一起加载两个外部配置；只传小麦配置不会自动配置航班连接：

```bash
uv run axiomforge \
  --flight-env-file /path/to/flight.env \
  --xiaomai-env-file /path/to/xiaomai.env
```

建议将固定启动参数保存在本机启动文件中，重启继续使用同一入口。不要把含真实凭据的配置文件提交到仓库。

### 算法流程图

进入当前项目的“算法与规则”，可新增图示并粘贴Mermaid流程图文本，更新预览后保存为新的图示版本，支持历史查看、节点说明、画布缩放／平移／全屏和完整SVG导出。航信自带“AHU运行策略”样例；华师大和大润发初始为空目录。

已保存版本默认存于本机`.nicegui/algorithm-flowcharts.sqlite3`，该目录不会提交到Git；可通过`AXIOMFORGE_FLOWCHART_DB`指定其他文件。源码语法错误时保留上次有效预览，未保存草稿不跨刷新或崩溃恢复。图示版本不等同于可执行算法版本，不发布参数或设备控制。详见[模块说明](docs/architecture/algorithm-flowcharts.md)。

## Development Checks

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/axiomforge
uv run pytest
```

## Design References

- [Architecture and execution boundaries](docs/architecture/workbench.md)
- [Modules and screen design](docs/architecture/workbench-ui.md)
- [Interface documentation guide](docs/interfaces/README.md)
- [Project documentation](docs/interfaces/projects/README.md)
- [Planning task and acceptance criteria](.trellis/tasks/11-04-nicegui-workbench-design/prd.md)
