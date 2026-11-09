# 提交计划（等待确认）

本任务只读功能已实现并完成最终浏览器复核，55项测试及静态检查通过，证据见`research/qa.md`。本文是最终提交确认的材料，不表示已执行git暂存、提交或推送。

## 建议工作提交

`feat: 新增航信功率与温度真实数据分析专题`

本次新增文件：

- `src/axiomforge/power_temperature.py`
- `src/axiomforge/xiaomai_source.py`
- `src/axiomforge/ui/power_temperature.py`
- `src/axiomforge/ui/power_temperature_charts.py`
- `tests/test_power_temperature.py`
- `tests/test_xiaomai_source.py`
- `tests/test_power_temperature_view.py`
- `.trellis/spec/backend/power-temperature.md`
- `.trellis/spec/frontend/power-temperature.md`
- `docs/interfaces/systems/xiaomai/`下的系统、鉴权与查询文档
- `docs/architecture/previews/power-temperature-20261106/`验收截图
- 本任务目录的规划与验收材料

与既有工作共用的文件，仅暂存本专题改动：

- `src/axiomforge/main.py`、`src/axiomforge/ui/workbench.py`、`src/axiomforge/ui/styles.css`
- `pyproject.toml`、`uv.lock`
- `README.md`、`docs/architecture/workbench.md`、`docs/architecture/workbench-ui.md`
- `docs/interfaces/systems/README.md`
- `docs/interfaces/projects/hangxin-airport/README.md`、`connections.md`、`dictionary.md`
- `.trellis/spec/backend/index.md`、`.trellis/spec/frontend/index.md`、`.trellis/spec/frontend/nicegui-workbench.md`

提交时必须逐块核对历史改动与新改动；若当前真实专题依赖尚未提交的历史航班／项目切换代码，需先明确依赖分组，不默默把旧工作混入本提交。实现开始前的集成文件快照保存在本机临时目录，可用于区分修改来源，不提交此临时目录。

## 当前不纳入提交的既有改动

- 航班领域、来源、UI及其测试：`flights.py`、`flight_source.py`、`ui/flights.py`、`test_flights.py`、`test_flight_source.py`
- 航班规范与接口文档：`.trellis/spec/backend/flights.md`、`docs/interfaces/systems/mangshi-flights/`
- 既有任务：`.trellis/tasks/11-04-hangxin-flight-topic/`和`.trellis/tasks/11-04-nicegui-workbench-design/`的历史改动
- `.agents/`、`.codex/`、`.gitattributes`、`.idea/`、`.DS_Store`
- `outputs/`、`share-previews/`等历史产物

仓库外登录配置、账号、密码、Token和下载测试CSV不纳入任何提交。当前没有暂存或提交上述内容。

## 后续记账

按项目工作流，用户确认提交计划后才执行工作提交，再归档当前任务并记录日志；记账提交也须使用符合AGENTS.md的中文说明，不沿用不相符的自动默认信息。没有提交确认时保留当前工作区和任务，不执行自动归档提交，也不推送远程。

## 本轮授权更新（2026-11-09）

用户已授权将更新代码按模块提交；改用算法流程图任务commit-plan.md中的整体分组。功率专题源码、测试和验收材料独立一组，共享小麦客户端先提交，共享工作台集成最后提交。
