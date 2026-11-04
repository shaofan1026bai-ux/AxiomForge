# Modules and Screen Design

Status: first style prototype implemented. The user approved concept 3 (Research Canvas), its dark-theme refinement and style implementation. The prototype opens the selected synthetic topic; production landing behavior and first real-data topic remain deferred. All prototype data displays `演示数据`.

## Shared Shell

Desktop target 1440 × 1024, also review at 1280 × 800. Header: AxiomForge, project selector, environment and service state. Sidebar: shared modules, project topics, settings. Keep project identity visible on detail and execution pages.

Shared navigation: `项目概览`, `数据分析`, `算法与规则`, `模型实验`, `任务中心`, `接口与设备`. `项目专题` lists only current-project topics. Proposed sidebar 216–232 px, header 60 px, base typography 14–16 px. Use the selected Research Canvas hierarchy.

Settings concerns local paths/runtime and connection references. No chat-model configuration is required for external Codex conversations.

An optional platform overview can summarize the three projects without merging their raw data/control targets. Startup overview versus last-used project has not been selected.

## Modules

| Module | Content | Main interactions |
|---|---|---|
| Project overview | latest outputs, failures, active/upcoming work | open a result, experiment or run |
| Data analysis | source/time/device, quality, charts/table | filter, inspect, export |
| Algorithms and rules | versions, logic, parameters, source and trials | adjust, historical trial, inspect |
| Model experiments | target/factors, config, metrics and residuals | train registered workflows, compare |
| Task center | schedules, active runs, history | enable/disable, run now, cancel, inspect logs |
| Interfaces and devices | definitions, connections and device/point mappings | browse, later validate/test approved connectors |
| Project topic | one business question combining these resources | focused filters and combined outputs |

## Screens

### Project Overview

Current project/environment, one obvious continue-viewing entry, recent outputs and active/failed runs. Empty projects show missing data/interfaces/topics, not invented zero-valued operational KPIs.

### Analysis Topic

Illustrative topic: `功率与温度分析`, not a confirmed real requirement. Top: title, source/demo label, dates/device. Main: large chart with readable units and timezone plus a supporting scatter plot when relevant. Horizontal tabs separate `分析结果`, `参数配置`, `代码查看`, and `运行记录`; parameters do not consume a permanently visible side panel. A compact recent-run summary can remain below the analysis results.

`更新分析` recomputes an existing registered algorithm. Export includes filter/provenance metadata. Changed filters mark old results stale; errors never fabricate success. Distinguish missing data from zero consumption.

### Algorithm Detail

Name, project applicability and immutable version. Sections `逻辑说明`, `参数`, `代码`, `试跑记录`; main action `历史试跑`. Missing required fields disable execution with a reason. Display declared interfaces and proposed commands. No chat or AI-generation button.

Source is read-only in the first planned UI; changes happen through Codex. Typed parameter edits do not modify source. No arbitrary Python evaluation from browser input.

### Model Experiment

Choose dataset, registered workflow, target and factors. Show training/validation/test ranges before submission. Running jobs display status/logs without fabricated completion percentages. Results compare baseline metrics, actual/predicted curves and residuals. Flag incompatible targets, units or test datasets when comparing.

### Task Center

Separate `定时计划` and `执行记录`; surface active runs. Schedule rows show version, project/environment, trigger/timezone, next execution and enabled state. `暂停计划` differs from `停止当前运行`. Clearly show cancelling, interrupted and unknown control outcomes.

### Interfaces and Devices

Sections `连接`, `查询接口`, `控制接口`, `设备与测点`. Distinguish documentation readiness from connectivity. First shell browses documented entries; full management/import/testing follows in feature planning. Credentials stay masked and are excluded from export.

## Interaction States

- Data panels support loading, empty, failed, stale and ready states.
- Project switching resets incompatible forms and prompts before discarding edits.
- Browser tabs have separate project context; navigation never retargets a running job.
- Reconnection reloads persisted run state.
- Labels/icons supplement status colors; preserve visible focus and field labels.
- Paginate/virtualize long tables; document chart aggregation/downsampling and preserve raw-data access.

## Visual Exploration

Three alternative single-screen concepts explore the same modules and synthetic analysis topic:

- Clear Analysis Desk: light sidebar, dominant chart, narrow parameter inspector.
- Operations Notebook: dark sidebar, light result workspace, parameters in a drawer.
- Research Canvas: cool-neutral shell, analysis/detail tabs and compact run timeline.

The third displayed image, Research Canvas, was selected. The other two concepts are not implementation targets. Concepts are not operating screenshots; application code remains pending converged planning review.

## Selected Theme Design

The user requires both light and dark backgrounds. Keep layout, module order, content and behavior identical between themes; change the full semantic palette rather than only the page background.

Implemented header control: `亮色 / 暗色 / 系统`. The preference persists locally per browser independently of project selection. In system mode, react to OS changes; explicit choices override them. An early theme bootstrap reduces initial surface flashing while the client connects.

| Semantic token | Light | Dark |
|---|---|---|
| Page canvas | #F8FAFC | #111827 |
| Sidebar/header | #F1F5F9 | #0F172A |
| Plot/table surface | #FFFFFF | #182234 |
| Primary text | #0F172A | #E5E7EB |
| Secondary text | #475569 | #A8B4C6 |
| Dividers/borders | #CBD5E1 | #334155 |
| Accent/link/focus | #4F46E5 | #818CF8 |

Tokens are proposed implementation values, not a pixel-perfect claim about generated images. Actual text/background and button-label pairs must be contrast-checked when built. Primary filled-button text needs its own contrast-safe token; do not blindly use white on the dark-theme accent.

Theme all dropdowns, dialogs, menus, form fields, disabled/hover/focus states, tables, scroll containers, empty/error panels and code viewers. Update chart backgrounds, axis labels, legends, tooltips and gridlines, including project topics. Preserve series identity and use dash/marker differences in addition to color. Do not recreate or rerun analysis when changing theme; retain filters, scroll/zoom state, parameters and job progress.

NiceGUI theme controls should be integrated with explicit semantic UI/Plotly tokens; a default dark-mode toggle alone is not sufficient. Do not change process-global chart templates based on a browser preference. Resolve theme per browser and update only that browser's displayed plots. Static historical PNG artifacts keep their original appearance; newly rendered charts follow the active theme. Export-theme selection can be deferred.

### Theme Acceptance

- Light and dark remain readable at both target viewport sizes, including menus and source code.
- Changing theme preserves project, filters, selected tabs, chart range and active runs.
- Preference survives reload and project switching; separate browsers do not overwrite one another's theme.
- System mode, if included, responds to the OS without overriding an explicit choice.
- Status remains understandable without color; no white plot rectangles or unreadable tooltip text remain in dark mode.

Selected light reference: [Research Canvas concept](assets/research-canvas-light.png). Dark reference: [Research Canvas dark refinement](assets/research-canvas-dark.png). Both are synthetic design concepts generated with the built-in ImageGen tool.

## Implemented Prototype and Verification

- [Actual light interface](previews/light-reviewed.jpg), [actual dark interface](previews/dark-reviewed.jpg).
- Shared navigation, all three project selectors, theme preferences, synthetic analysis/filtering, parameter form, real source inspection and per-client demo records are implemented.
- Model, task and interface modules are navigable pending-integration states. Project overview currently presents a site catalogue; operational summaries await real integrations.
- Filters/parameters/records are session-only and reset on reload; only theme preference persists. No SQLite, live connection, worker supervisor, training or scheduler is claimed.
- CSV generation and provenance are unit-tested. Browser download verification was declined by the browser permission gate and was not retried or bypassed.
- See [design QA](../../design-qa.md) for comparisons, checks and residual limitations.
