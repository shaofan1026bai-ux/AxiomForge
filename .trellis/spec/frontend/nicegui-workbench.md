# NiceGUI Workbench Contract

## 1. Scope / Trigger

Applies to `src/axiomforge/main.py`, `ui/`, and `demo.py`. The current app is a local style prototype with synthetic data, not a device controller or scheduler. NiceGUI is the selected implementation framework; do not introduce a React scaffold.

航信“今日航班”专栏现已支持真实只读查询；修改该页同时阅读[航班契约](../backend/flights.md)。`FlightView`拥有每浏览器状态，I/O查询通过`run.io_bound`运行，主题切换只更新当前图表。列表筛选不得传入空档计算；失败保留旧快照，不将异常转换为空数组。航班页面使用“航班只读”环境标识，设备分析演示仍保留演示标签。

航信“功率与温度分析”使用小麦真实测点，修改前阅读[专题前端契约](power-temperature.md)与[只读数据契约](../backend/power-temperature.md)。华师大和大润发不展示功率与温度专题，默认进入项目概览，共享数据分析入口显示待接入状态。演示领域代码保留，但不作为这些项目的专题入口。真实专题使用独立View和快照，不复用`generate_samples`或演示`Parameters`。

航信新增“AHU回风温度”，路由`ahu-temperature`，采用独立`AhuTemperatureView`。修改前阅读[AHU前端合同](ahu-temperature.md)与[领域合同](../backend/ahu-temperature.md)。仅航信可进入，工作台切项目确认纳入其dirty/loading，断开／离页使请求失效；恢复查询偏好不自动取数。

## 2. Signatures

- Startup: `uv run axiomforge [--port PORT]`, default loopback `127.0.0.1:8080`.
- `generate_samples(project_id: str, device: str, start: date, end: date, params: Parameters) -> list[Sample]`.
- `export_csv(project_id: str, device: str, samples: list[Sample]) -> bytes`.
- `chart_options(samples, params, dark, *, scatter=False) -> dict[str, Any]` creates fresh options per client.
- `create_page()` creates one `Workbench` instance per browser client.

## 3. Contracts

- Project IDs come from `demo.PROJECTS`; device selection must belong to that project.
- `Parameters`: interval in minutes (15/30/60/120), temperature factor boolean, threshold 0–300 kW.
- Dates are inclusive calendar dates; samples run from start midnight through the end day at interval boundaries. Display/export timezone is Asia/Shanghai.
- Synthetic measurements are deterministic. CSV contains scope, units and `synthetic_demo` provenance, encoded UTF-8 with BOM.
- UI state and session records live in the client-owned Workbench. No global mutable current project or global chart theme.
- 浏览器本地 `axiomforge.theme` 保存 `light`、`dark` 或 `system`，系统模式监听系统主题变化；`axiomforge.project` 保存最近成功进入的项目 ID。AHU专题另以项目＋专题＋版本键保存最近合法提交的非敏感条件，具体见其合同；原始数据、凭据及运行记录不写入偏好。
- 无有效项目缓存时打开项目概览，不默认选中首个项目；概览卡片和顶部选择器共用项目切换逻辑，成功后航信进入真实专题，其他项目进入项目概览。有效缓存恢复对应项目及其默认入口，未知 ID 回退概览。未选择项目时，项目功能导航提示先选择项目。
- CSS owns semantic surface/text tokens; ECharts owns plot/axis/tooltip colors. Theme updates merge options and do not reset data zoom or recompute samples.
- Parameter drafts persist across tabs. Project switch confirms before discarding drafts. Changed filters mark results stale and disable CSV export until recomputed.
- Static code inspection uses a known repository function; never evaluate browser-submitted Python.
- NiceGUI 3.19 puts Quasar `!important` helpers in CSS layers. Use `color=None` on custom-colored navigation/tab buttons rather than fighting `text-primary` with unlayered CSS. Explicitly remove drawer default padding when the inner shell owns spacing.
- `ui.run_javascript` needs a top-level `return` when Python awaits a value. Guard a missing result when the browser disconnects during initialization.
- Rebuilding content deletes the original event slot. Send notifications before clearing that slot or use an explicitly live client/container context afterwards.

## 4. Validation and Error Matrix

| Input/event | Result |
|---|---|
| Foreign-project device | ValueError; UI warns, no result replacement |
| Reversed dates or range over 31 days | ValueError; UI warns |
| Empty/invalid date or parameter | UI warns and retains prior result |
| Unsupported interval/out-of-range threshold | ValueError |
| Project switch with unapplied changes | Confirmation; cancel restores previous project selection |
| Filter changes before update | Stale message and disabled export |
| Invalid persisted theme | Resolve to system mode |
| localStorage unavailable | Catch storage failure; current-session theme still works |
| 无缓存、未知或无效的项目 ID | 显示项目概览，不使用首个项目兜底 |
| 有效项目缓存 | 恢复对应项目，航信打开真实专题，其他项目打开项目概览 |
| 切换项目后取消确认 | 保持原项目、当前页、草稿和缓存 |
| 项目缓存不可读写 | 当前会话仍可从概览选择项目并进行分析 |

## 5. Good / Base / Bad Cases

- Base: load synthetic topic, adjust device/date, update, inspect and export.
- Good: switch theme while zoomed; preserve result and zoom, update plot labels/tooltips to readable colors.
- Bad: select a device from a different project, label old data with new filters, or portray a placeholder schedule as running.

## 6. Required Verification

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/axiomforge
uv run pytest
```

`tests/test_demo.py` verifies reproducibility/range counts, project-device isolation, invalid ranges and CSV provenance. Browser checks cover theme reload, project switching/cancel, parameters, record isolation, export, code/empty modules and 1440x1024 / 1280x800 layouts. Save visual evidence and report concrete gaps.

## 7. Wrong vs Correct

Wrong: assign `current_project` at module scope or mutate a global chart palette. One client's interaction then leaks to others.

Correct: create a Workbench per `@ui.page` request, capture all state there, and return fresh chart options using its resolved theme.

Wrong: export currently visible samples using newly edited device/date metadata.

Correct: block export while filters are stale; only export after validated recomputation.

航信“每日用电占比”采用独立`EnergyShareView`和路由`daily-energy-share`，见[页面合同](energy-share.md)及[领域合同](../backend/energy-share.md)。项目切换确认包含其dirty/loading，断开与离页使迟到结果失效，恢复偏好不自动请求。
