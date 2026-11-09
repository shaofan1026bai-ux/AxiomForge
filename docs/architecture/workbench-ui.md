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

本轮“算法与规则”增加可编辑的Mermaid图示模块：目录按项目隔离，航信首条AHU运行策略包含用户原始9节点9边；图示版本与可执行算法版本独立。布局为目录、画布和节点详情，提供源码预览、版本保存、历史派生及完整SVG导出。这里编辑的是说明图，不执行Python、现场控制或运行参数发布。操作与边界见[算法流程图说明](algorithm-flowcharts.md)。

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

## 航信今日航班专栏（2026-11-04）

用户选定进出港双栏，并追加长间隔时间带。实现入口仅在航信项目显示；共享壳层和主题不变。上方为手动日期查询与搜索、计划航班／进港／出港／本场已有实际时刻摘要，中部合并全量进出港绘制时间带与长间隔分页表，下方为双栏分页列表。列表搜索与方向／状态筛选不影响空档计算。

支持计划与动态（实际→预计→计划）时刻；默认间隔阈值30分钟可调。相邻计划日参与空档分割，跨日标注“次日”，首末端无数据不外推空档。保障时间未配置，当前不输出可停机时段或设备指令。真实查询通过服务器外部配置启用，不在前端暴露凭据。

浏览器已检查真实查询、搜索不影响间隔、30→60分钟阈值、动态切换、分页、项目入口隔离、1440×1024及1280×800亮暗主题。快照只保存在当前浏览器对应的服务端内存，页面刷新后需重新查询。字段和已知限制以[航班数据口径](../interfaces/systems/mangshi-flights/query/flights.md)为准。

### 国内／国际区域细分（2026-11-05）

用户确认国内与国际分别对应进出港区域。航班间隔新增“全场合并／国内进港／国内出港／国际进港／国际出港”范围按钮，显示当日各范围数量。全场时间带分四轨，区域视图只显示对应轨道与列表；搜索不改变区域间隔，切换区域时清除不兼容的方向过滤。航班行显示国内／国际／待确认标签。分类采用当前源库fcategory的样本映射，正式字典待确认；未知属性保守阻断同方向区域间隔，不默认国内，不生成设备策略。

本轮19项测试通过，真实数据与五种区域组件构建已核对；浏览器控制连接超时，本轮新增布局尚未完成浏览器视觉验收。

## 航信功率与温度真实专题（2026-11-06）

航信项目的“功率与温度分析”及“数据分析”入口使用真实小麦View。用户于2026-11-06追加明确：华师大和大润发隐藏整个航信专题分组，默认进入项目概览，共享“数据分析”显示待接入状态；不展示功率与温度演示专题。页面沿用Research Canvas及亮／暗／系统主题，使用“小麦只读”环境标签，不再展示演示算法参数和模拟运行记录。

顶部为今天／昨天／自定义时间、查询与完整范围CSV导出；默认今天，截至提交时刻，最长7天、固定上游2分钟。结果页提供平均功率、最大功率、温度范围和有效配对比例四项摘要，趋势与散点并排或窄屏上下排列。“缩放查看”不改分析范围，“选择分析时段”联动摘要与散点；点击散点定位趋势中的时间，重置恢复完整统计。

“数据明细”提供分页及选区查看；“测点与口径”保留两台来源设备、测点ID、单位和统计定义。缺失不填0，温度统计与功率缺失独立，r在样本不足或恒定时明确不可计算。CSV始终导出完整成功快照，包含来源、时区与数据状态。

初次进入不调用上游；查询失败保留旧快照，草稿待更新及失败时禁用导出。每客户端独立快照与请求代号，切项目／离页后迟到响应不得污染新画面。浏览器刷新后需重新查询，主题变化不触发取数。接口与时间口径见[小麦历史契约](../interfaces/systems/xiaomai/query/history.md)。本轮验收证据记录在专题任务的`research/`目录。
