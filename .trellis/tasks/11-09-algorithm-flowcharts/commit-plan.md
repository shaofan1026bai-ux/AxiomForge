# 模块提交计划（已授权）

2026-11-09用户明确授权“更新的代码可以按模块提交到代码仓库了”。此授权扩展到当前已完成的功能模块，替代此前仅提交流程图、排除其他已完成功能的暂定范围。执行本地提交，不推送。

先提交共享客户端及独立功能模块，最后统一接入工作台导航与启动参数；源码、对应测试、接口文档、规范和验收资料按归属一起提交。

## 1. feat: 新增小麦只读认证与历史数据客户端

- `src/axiomforge/xiaomai_client.py`
- `pyproject.toml`
- `uv.lock`
- `docs/interfaces/systems/xiaomai/README.md`
- `docs/interfaces/systems/xiaomai/auth.md`
- `docs/interfaces/systems/xiaomai/query/projects.md`
- `docs/interfaces/systems/xiaomai/query/points.md`
- `docs/interfaces/systems/xiaomai/query/history.md`

## 2. feat: 新增航信航班查询与空档分析模块

- `src/axiomforge/flights.py`
- `src/axiomforge/flight_source.py`
- `src/axiomforge/ui/flights.py`
- `tests/test_flights.py`
- `tests/test_flight_source.py`
- `.trellis/spec/backend/flights.md`
- `docs/interfaces/systems/mangshi-flights`
- `.trellis/tasks/11-04-hangxin-flight-topic`

## 3. feat: 新增航信功率与温度分析模块

- `src/axiomforge/power_temperature.py`
- `src/axiomforge/xiaomai_source.py`
- `src/axiomforge/ui/power_temperature.py`
- `src/axiomforge/ui/power_temperature_charts.py`
- `tests/test_power_temperature.py`
- `tests/test_xiaomai_source.py`
- `tests/test_power_temperature_view.py`
- `.trellis/spec/backend/power-temperature.md`
- `.trellis/spec/frontend/power-temperature.md`
- `docs/architecture/previews/power-temperature-20261106`
- `.trellis/tasks/11-06-hangxin-power-temperature`

## 4. feat: 新增航信AHU回风温度分析模块

- `src/axiomforge/ahu_temperature.py`
- `src/axiomforge/ahu_temperature_source.py`
- `src/axiomforge/ui/ahu_temperature.py`
- `src/axiomforge/ui/ahu_temperature.css`
- `src/axiomforge/ui/ahu_temperature_charts.py`
- `tests/test_ahu_temperature.py`
- `tests/test_ahu_temperature_source.py`
- `tests/test_ahu_temperature_view.py`
- `.trellis/spec/backend/ahu-temperature.md`
- `.trellis/spec/frontend/ahu-temperature.md`
- `docs/interfaces/systems/xiaomai/query/ahu-temperature.md`
- `docs/architecture/previews/ahu-temperature-20261109`
- `.trellis/tasks/11-09-hangxin-ahu-return-temperature`

## 5. feat: 新增航信每日用电占比分析模块

- `src/axiomforge/energy_share.py`
- `src/axiomforge/energy_share_source.py`
- `src/axiomforge/ui/energy_share.py`
- `src/axiomforge/ui/energy_share.css`
- `src/axiomforge/ui/energy_share_charts.py`
- `tests/test_energy_share.py`
- `tests/test_energy_share_source.py`
- `tests/test_energy_share_view.py`
- `.trellis/spec/backend/energy-share.md`
- `.trellis/spec/frontend/energy-share.md`
- `docs/interfaces/systems/xiaomai/query/energy-share.md`
- `docs/architecture/previews/energy-share-20261109`
- `.trellis/tasks/11-09-hangxin-daily-energy-share`

## 6. feat: 新增按项目管理的算法流程图模块

- `src/axiomforge/algorithm_flowcharts.py`
- `src/axiomforge/flowchart_store.py`
- `src/axiomforge/resources/hangxin-ahu.mmd`
- `src/axiomforge/ui/algorithm_flowcharts.py`
- `src/axiomforge/ui/algorithm_flowcharts.css`
- `src/axiomforge/ui/flowchart_canvas.py`
- `src/axiomforge/ui/flowchart_canvas.js`
- `tests/test_algorithm_flowcharts.py`
- `tests/test_flowchart_store.py`
- `tests/test_algorithm_flowcharts_view.py`
- `tests/test_flowchart_canvas.py`
- `.trellis/spec/backend/algorithm-flowcharts.md`
- `.trellis/spec/frontend/algorithm-flowcharts.md`
- `docs/architecture/algorithm-flowcharts.md`
- `docs/architecture/previews/algorithm-flowcharts-20261109`
- `.trellis/tasks/11-09-algorithm-flowcharts`

## 7. feat: 集成项目工作台导航与专题启动配置

- `src/axiomforge/main.py`
- `src/axiomforge/ui/workbench.py`
- `src/axiomforge/ui/styles.css`
- `README.md`
- `docs/architecture/workbench.md`
- `docs/architecture/workbench-ui.md`
- `docs/interfaces/projects/hangxin-airport/README.md`
- `docs/interfaces/projects/hangxin-airport/connections.md`
- `docs/interfaces/projects/hangxin-airport/dictionary.md`
- `docs/interfaces/systems/README.md`
- `.trellis/spec/backend/index.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/spec/frontend/nicegui-workbench.md`
- `.trellis/tasks/11-04-nicegui-workbench-design/design.md`
- `.trellis/tasks/11-04-nicegui-workbench-design/implement.md`
- `.trellis/tasks/11-04-nicegui-workbench-design/prd.md`

## 不纳入

`.DS_Store`、`.idea/`、`.agents/`、`.codex/`、`.gitattributes`、`outputs/`、`share-previews/`均保留原状，不混入功能提交。运行数据库、外部配置、账号、密码、Token不纳入。

## 验证

提交前全项目273项测试通过，ruff、格式及mypy检查通过。提交后再从HEAD导出的独立源码验证，防止遗漏未跟踪依赖。当前算法流程图任务在功能提交后归档并记录中文会话日志；其他任务保持现状。
