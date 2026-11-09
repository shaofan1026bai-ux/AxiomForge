"""Research Canvas shell and interactive, synthetic analysis preview."""

import inspect
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import events, ui

from axiomforge.auth import Session, sessions
from axiomforge.auth_socket import bind_client
from axiomforge.demo import (
    DEMO_END,
    DEMO_START,
    PROJECT_BY_ID,
    PROJECTS,
    Parameters,
    Sample,
    export_csv,
    generate_samples,
)
from axiomforge.ui.ahu_temperature import AhuTemperatureView
from axiomforge.ui.algorithm_flowcharts import AlgorithmFlowchartsView
from axiomforge.ui.charts import chart_options
from axiomforge.ui.energy_share import EnergyShareView
from axiomforge.ui.flights import FlightView
from axiomforge.ui.power_temperature import PowerTemperatureView

NAVIGATION = (
    ("overview", "项目概览", "home"),
    ("data", "数据分析", "query_stats"),
    ("algorithms", "算法与规则", "code"),
    ("models", "模型实验", "science"),
    ("tasks", "任务中心", "checklist"),
    ("interfaces", "接口与设备", "cable"),
)
THEMES = {"light": "亮色", "dark": "暗色", "system": "系统"}
TABS = ("分析结果", "参数配置", "代码查看", "运行记录")

THEME_BOOTSTRAP = """
<script>
(() => {
  let mode = 'system';
  try { mode = localStorage.getItem('axiomforge.theme') || 'system'; } catch (_) {}
  const media = matchMedia('(prefers-color-scheme: dark)');
  const dark = mode === 'dark' || (mode === 'system' && media.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();
</script>
"""


class Workbench:
    """All mutable view state belongs to one NiceGUI client."""

    def __init__(self, session: Session | None = None) -> None:
        self.auth_session = session
        self.drawer: ui.left_drawer
        self.project_id = ""
        self.device = ""
        self.result_device = self.device
        self.start = DEMO_START.isoformat()
        self.end = DEMO_END.isoformat()
        self.params = Parameters()
        self.draft_interval = self.params.interval
        self.draft_temperature = self.params.temperature
        self.draft_threshold = self.params.threshold
        self.section = "overview"
        self.active_tab = TABS[0]
        self.dark = False
        self.mode = "system"
        self.os_dark = False
        self.stale = False
        self.samples: list[Sample] = []
        self.records: dict[str, list[dict[str, Any]]] = {p.id: [] for p in PROJECTS}
        self.charts: list[tuple[ui.echart, bool]] = []
        self.nav_buttons: dict[str, ui.button] = {}
        self.dark_mode = ui.dark_mode(value=False)
        self.flight_view = FlightView()
        self.power_view = PowerTemperatureView()
        self.ahu_view = AhuTemperatureView()
        self.energy_view = EnergyShareView()
        self.flowchart_view = AlgorithmFlowchartsView()

    def build(self) -> None:
        ui.colors(primary="#4f46e5")
        ui.add_head_html(THEME_BOOTSTRAP)
        ui.add_css(Path(__file__).with_name("styles.css").read_text())
        with ui.header().classes("p-0"):
            with ui.row().classes("topbar items-center"):
                with ui.row().classes("brand"):
                    ui.icon("view_in_ar")
                    ui.label("AxiomForge").classes("brand-text")
                ui.button(icon="menu", color=None, on_click=lambda: self.drawer.toggle()).props(
                    'flat round dense aria-label="展开导航"'
                ).classes("lt-md")
                self.project_select = (
                    ui.select(
                        {p.id: p.name for p in PROJECTS},
                        value=None,
                        on_change=self.select_project,
                    )
                    .props('outlined dense aria-label="选择项目" placeholder="请选择项目"')
                    .classes("project-select")
                )
                self.environment_label = ui.label("演示环境").classes("environment")
                with ui.row().classes("service"):
                    ui.icon("circle")
                    ui.label("本地服务运行中")
                self.theme_toggle = (
                    ui.toggle(
                        THEMES,
                        value="system",
                        on_change=self.select_theme,
                    )
                    .props('unelevated no-caps aria-label="主题模式"')
                    .classes("theme-toggle")
                )
                ui.label("Asia/Shanghai · 本地工作台").classes("timezone")
                if self.auth_session is not None:
                    with ui.row().classes("auth-account items-center no-wrap"):
                        ui.icon("account_circle").classes("text-lg")
                        ui.label(self.auth_session.account).classes("auth-account-name").tooltip(
                            self.auth_session.account
                        )
                        with ui.element("form").props("method=post action=/logout"):
                            ui.element("input").props(
                                f'type=hidden name=csrf value="{self.auth_session.csrf}"'
                            )
                            with (
                                ui.element("button")
                                .props('type=submit aria-label="退出登录"')
                                .classes("auth-logout")
                            ):
                                ui.icon("logout")
                                ui.label("退出")

        with (
            ui.left_drawer(value=True, fixed=True)
            .props("width=220 breakpoint=800 bordered")
            .classes("sidebar") as self.drawer
        ):
            with ui.column().classes("sidebar-inner w-full gap-0"):
                ui.label("WORKSPACE").classes("nav-caption")
                for key, name, icon in NAVIGATION:
                    self.nav_buttons[key] = (
                        ui.button(
                            name,
                            icon=icon,
                            color=None,
                            on_click=lambda key=key: self.navigate(key),
                        )
                        .props("flat no-caps")
                        .classes("nav-item" + (" active" if key == self.section else ""))
                    )
                self.topic_divider = ui.element("div").classes("nav-divider")
                self.topic_caption = ui.label("项目专题").classes("nav-caption")
                self.topic_divider.set_visibility(False)
                self.topic_caption.set_visibility(False)
                self.nav_buttons["topic"] = (
                    ui.button(
                        "功率与温度分析",
                        icon="device_thermostat",
                        color=None,
                        on_click=lambda: self.navigate("topic"),
                    )
                    .props("flat no-caps")
                    .classes("nav-item")
                )
                self.nav_buttons["topic"].set_visibility(False)
                self.nav_buttons["flights"] = (
                    ui.button(
                        "今日航班",
                        icon="flight",
                        color=None,
                        on_click=lambda: self.navigate("flights"),
                    )
                    .props("flat no-caps")
                    .classes("nav-item")
                )
                self.nav_buttons["flights"].set_visibility(False)
                self.nav_buttons["ahu-temperature"] = (
                    ui.button(
                        "AHU 回风温度",
                        icon="thermostat",
                        color=None,
                        on_click=lambda: self.navigate("ahu-temperature"),
                    )
                    .props("flat no-caps")
                    .classes("nav-item")
                )
                self.nav_buttons["ahu-temperature"].set_visibility(False)
                self.nav_buttons["daily-energy-share"] = (
                    ui.button(
                        "每日用电占比",
                        icon="bar_chart",
                        color=None,
                        on_click=lambda: self.navigate("daily-energy-share"),
                    )
                    .props("flat no-caps")
                    .classes("nav-item")
                )
                self.nav_buttons["daily-energy-share"].set_visibility(False)
                with ui.column().classes("sidebar-bottom w-full gap-0"):
                    ui.element("div").classes("nav-divider w-full")
                    self.nav_buttons["settings"] = (
                        ui.button(
                            "设置",
                            icon="settings",
                            color=None,
                            on_click=lambda: self.navigate("settings"),
                        )
                        .props("flat no-caps")
                        .classes("nav-item")
                    )
                    ui.label("LOCAL WORKBENCH / 0.1").classes("sidebar-note")

        self.content = ui.column().classes("workspace gap-0")
        self.render()
        ui.on("system-theme", self.system_theme_changed)

    async def initialize_preferences(self) -> None:
        await ui.context.client.connected()
        settings = await ui.run_javascript("""
            return (() => {
              let mode = 'system';
              let project = null;
              try { mode = localStorage.getItem('axiomforge.theme') || 'system'; } catch (_) {}
              try { project = localStorage.getItem('axiomforge.project'); } catch (_) {}
              const media = matchMedia('(prefers-color-scheme: dark)');
              media.addEventListener('change', event => {
                emitEvent('system-theme', {dark: event.matches});
              });
              return {mode, dark: media.matches, project};
            })()
        """)
        if not isinstance(settings, dict):
            return  # 浏览器可能在偏好恢复过程中断开连接。
        self.mode = settings["mode"] if settings["mode"] in THEMES else "system"
        self.os_dark = bool(settings["dark"])
        self.theme_toggle.set_value(self.mode)
        self.apply_theme()
        project_id = settings.get("project")
        # 缓存可能过期或被手动修改；恢复时也必须校验项目范围。
        if not self.project_id and isinstance(project_id, str) and project_id in PROJECT_BY_ID:
            await self.enter_project(project_id)

    def select_theme(self, event: events.ValueChangeEventArguments) -> None:
        self.mode = event.value
        self.apply_theme()

    def system_theme_changed(self, event: events.GenericEventArguments) -> None:
        self.os_dark = bool(event.args["dark"])
        if self.mode == "system":
            self.apply_theme()

    def apply_theme(self) -> None:
        self.dark = self.mode == "dark" or (self.mode == "system" and self.os_dark)
        self.dark_mode.set_value(self.dark)
        if self.section == "flights":
            self.flight_view.set_theme(self.dark)
        if self.project_id == "hangxin-airport" and self.section in {"topic", "data"}:
            self.power_view.set_theme(self.dark)
        if self.project_id == "hangxin-airport" and self.section == "ahu-temperature":
            self.ahu_view.set_theme(self.dark)
        if self.project_id == "hangxin-airport" and self.section == "daily-energy-share":
            self.energy_view.set_theme(self.dark)
        if self.section == "algorithms":
            self.flowchart_view.set_theme(self.dark)
        resolved = json.dumps("dark" if self.dark else "light")
        ui.run_javascript(
            f"document.documentElement.dataset.theme = {resolved};"
            f"try {{ localStorage.setItem('axiomforge.theme', {json.dumps(self.mode)}); }}"
            "catch (_) {}"
        )
        for chart, scatter in self.charts:
            options = chart_options(self.samples, self.params, self.dark, scatter=scatter)
            # Theme changes merge colors without resetting the user's current zoom.
            options.pop("dataZoom", None)
            chart.options.update(options)
            chart.update()

    async def select_project(self, event: events.ValueChangeEventArguments) -> None:
        if isinstance(event.value, str) and event.value != self.project_id:
            await self.enter_project(event.value)

    async def enter_project(self, project_id: str) -> None:
        """统一处理概览入口、快捷切换和缓存恢复，确认成功后才写入偏好。"""
        if project_id not in PROJECT_BY_ID:
            return
        if project_id == self.project_id:
            await self.navigate("topic" if project_id == "hangxin-airport" else "overview")
            return
        other_pending = (
            (
                self.project_id == "hangxin-airport"
                and (
                    self.power_view.dirty
                    or self.power_view.loading
                    or self.ahu_view.dirty
                    or self.ahu_view.loading
                    or self.energy_view.dirty
                    or self.energy_view.loading
                )
            )
            or self.stale
            or (
                self.draft_interval != self.params.interval
                or self.draft_temperature != self.params.temperature
                or self.draft_threshold != self.params.threshold
            )
        )
        flowchart_pending = self.section == "algorithms" and (
            self.flowchart_view.dirty or self.flowchart_view.loading
        )
        if flowchart_pending:
            if not await self.flowchart_view.confirm_leave(
                "其他专题未应用的筛选或参数也将重置。" if other_pending else ""
            ):
                self.project_select.set_value(self.project_id)
                return
        elif other_pending:
            with ui.dialog() as dialog, ui.card():
                ui.label("切换项目？").classes("text-lg font-semibold")
                ui.label("当前未应用的筛选或参数将被重置，进行中的查询结果不会发布到新项目。")
                with ui.row().classes("w-full justify-end"):
                    ui.button("取消", on_click=lambda: dialog.submit(False)).props("flat")
                    ui.button("切换项目", on_click=lambda: dialog.submit(True))
            if not await dialog:
                self.project_select.set_value(self.project_id)
                return
        if self.project_id == "hangxin-airport":
            self.power_view.discard_draft()
            self.ahu_view.discard_draft()
            self.energy_view.discard_draft()
        self.project_id = project_id
        has_topics = project_id == "hangxin-airport"
        self.nav_buttons["topic"].set_visibility(has_topics)
        self.nav_buttons["flights"].set_visibility(has_topics)
        self.nav_buttons["ahu-temperature"].set_visibility(has_topics)
        self.nav_buttons["daily-energy-share"].set_visibility(has_topics)
        self.topic_divider.set_visibility(has_topics)
        self.topic_caption.set_visibility(has_topics)
        # 先更新状态再同步选择器，避免触发重复的切换确认。
        self.project_select.set_value(project_id)
        self.device = PROJECT_BY_ID[self.project_id].devices[0]
        self.result_device = self.device
        self.start, self.end = DEMO_START.isoformat(), DEMO_END.isoformat()
        self.params = Parameters()
        self.draft_interval = self.params.interval
        self.draft_temperature = self.params.temperature
        self.draft_threshold = self.params.threshold
        self.samples = (
            []
            if project_id == "hangxin-airport"
            else generate_samples(
                self.project_id,
                self.device,
                DEMO_START,
                DEMO_END,
                self.params,
            )
        )
        self.stale = False
        self.active_tab = TABS[0]
        ui.run_javascript(
            f"try {{ localStorage.setItem('axiomforge.project', {json.dumps(project_id)}); }}"
            "catch (_) {}"
        )
        await self.navigate("topic" if has_topics else "overview", skip_guard=True)

    async def navigate(self, section: str, *, skip_guard: bool = False) -> None:
        if (
            not skip_guard
            and self.section == "algorithms"
            and section != "algorithms"
            and not await self.flowchart_view.confirm_leave()
        ):
            return
        if (
            section in {"topic", "flights", "ahu-temperature", "daily-energy-share"}
            and self.project_id != "hangxin-airport"
        ):
            section = "overview"
        if not self.project_id and section not in ("overview", "settings"):
            ui.notify("请先从项目概览选择一个项目。", type="info")
            section = "overview"
        self.section = section
        for key, button in self.nav_buttons.items():
            button.classes(
                add="active" if key == section else "", remove="" if key == section else "active"
            )
        self.render()

    def render(self) -> None:
        real_power = self.project_id == "hangxin-airport" and self.section in {"topic", "data"}
        real_ahu = self.project_id == "hangxin-airport" and self.section == "ahu-temperature"
        real_energy = self.project_id == "hangxin-airport" and self.section == "daily-energy-share"
        self.environment_label.set_text(
            "航班只读"
            if self.section == "flights"
            else "小麦只读"
            if real_power or real_ahu or real_energy
            else "本地图示"
            if self.section == "algorithms"
            else "演示环境"
        )
        self.flight_view.deactivate()
        self.power_view.deactivate()
        self.ahu_view.deactivate()
        self.energy_view.deactivate()
        self.flowchart_view.deactivate()
        self.charts.clear()
        self.content.clear()
        with self.content:
            if self.section == "flights":
                self.flight_view.build(self.dark)
            elif self.section == "algorithms":
                self.flowchart_view.build(self.project_id, self.dark)
            elif real_energy:
                self.energy_view.build(self.dark)
            elif real_ahu:
                self.ahu_view.build(self.dark)
            elif self.section in ("topic", "data"):
                if real_power:
                    self.power_view.build(self.dark)
                else:
                    self.render_module()
            elif self.section == "overview":
                self.render_overview()
            elif self.section == "settings":
                self.heading("偏好设置", "按你的习惯调整工作台。")
                with ui.row().classes("info-row"):
                    ui.icon("contrast").classes("text-xl")
                    ui.label("外观主题")
                    ui.label("使用顶部主题开关，选择亮色、暗色或跟随系统。").classes("description")
                with ui.row().classes("info-row"):
                    ui.label("运行环境")
                    ui.label("本地工作台 · 航班可配置只读连接 · 不执行设备控制").classes(
                        "description"
                    )
            else:
                self.render_module()
            with ui.row().classes("page-footer w-full"):
                ui.icon("info_outline")
                ui.label(
                    "航班为只读查询快照；不执行设备控制。"
                    if self.section == "flights"
                    else "小麦功率与温度为只读查询快照；不执行设备控制。"
                    if real_power
                    else "AHU 回风温度为只读统计；缺测不补值，样本占比不代表精确时长。"
                    if real_ahu
                    else "八项电表只读统计；缺测日占比留空，已知小计不代表八项总量。"
                    if real_energy
                    else "流程图用于逻辑说明；图示版本独立保存，不执行设备控制。"
                    if self.section == "algorithms"
                    else "演示数据与示例设备，仅用于界面预览；未连接现场系统。"
                )
                ui.label("AxiomForge v0.1").classes("version")

    def heading(self, title: str, description: str = "") -> None:
        with ui.row().classes("breadcrumb"):
            ui.label(PROJECT_BY_ID[self.project_id].name if self.project_id else "工作台")
            ui.icon("chevron_right").classes("text-sm")
            ui.label("研究画布  Research Canvas")
        with ui.row().classes("page-heading"):
            ui.label(title).props('role="heading" aria-level="1"').classes("page-title")
            ui.label("演示数据").classes("demo-badge")
        if description:
            ui.label(description).classes("description")

    def render_topic(self) -> None:
        self.heading("功率与温度分析", "查看设备运行趋势，探索功率与环境因素之间的关系。")
        with ui.row().classes("section-tabs w-full"):
            for name in TABS:
                ui.button(name, color=None, on_click=lambda name=name: self.change_tab(name)).props(
                    "flat no-caps"
                ).classes("section-tab" + (" active" if name == self.active_tab else ""))
        if self.active_tab == "参数配置":
            self.render_parameters()
        elif self.active_tab == "代码查看":
            with ui.column().classes("code-panel w-full"):
                ui.code(inspect.getsource(generate_samples), language="python").classes("w-full")
            ui.label("当前展示真实的演示数据生成函数。算法修改通过 Codex 完成。").classes(
                "description mt-3"
            )
        elif self.active_tab == "运行记录":
            self.render_history(full=True)
        else:
            self.render_filters()
            with ui.element("div").classes("chart-grid"):
                self.render_chart(False)
                self.render_chart(True)
            with ui.row().classes("insight"):
                ui.icon("info_outline")
                ui.label("当前曲线为可复现的模拟数据，相关性不代表因果。滚轮可缩放时序图。")
            self.render_history()

    def change_tab(self, name: str) -> None:
        self.active_tab = name
        self.render()

    def render_filters(self) -> None:
        with ui.row().classes("filterbar w-full"):
            ui.label("时间范围").classes("filter-label")
            ui.input(value=self.start, on_change=lambda e: self.set_filter("start", e.value)).props(
                'type=date outlined dense aria-label="开始日期"'
            ).classes("date-input")
            ui.label("至").classes("range-separator")
            ui.input(value=self.end, on_change=lambda e: self.set_filter("end", e.value)).props(
                'type=date outlined dense aria-label="结束日期"'
            ).classes("date-input")
            ui.label("设备").classes("filter-label ml-1")
            ui.select(
                list(PROJECT_BY_ID[self.project_id].devices),
                value=self.device,
                on_change=lambda e: self.set_filter("device", e.value),
            ).props('outlined dense aria-label="分析设备"').classes("device-select")
            with ui.row().classes("filter-actions"):
                ui.button("更新分析", icon="refresh", on_click=self.run_analysis).props(
                    "unelevated no-caps"
                ).classes("primary-btn")
                self.export_button = (
                    ui.button(
                        "导出",
                        icon="file_download",
                        color=None,
                        on_click=self.download,
                    )
                    .props("flat no-caps")
                    .classes("secondary-btn")
                )
                self.export_button.set_enabled(not self.stale)
            self.stale_label = ui.label("筛选已变更，请更新分析").classes("stale")
            self.stale_label.set_visibility(self.stale)

    def set_filter(self, key: str, value: str) -> None:
        setattr(self, key, value)
        self.stale = True
        self.stale_label.set_visibility(True)
        self.export_button.disable()

    def render_chart(self, scatter: bool) -> None:
        with ui.element("section").classes("chart-panel"):
            with ui.row().classes("chart-title"):
                ui.icon("scatter_plot" if scatter else "show_chart")
                ui.label("功率与室外温度关系" if scatter else f"功率时序 · {self.result_device}")
                if scatter:
                    ui.label("相关性不代表因果").classes("chart-subtitle")
            if scatter and not self.params.temperature:
                with ui.column().classes("items-center justify-center plot"):
                    ui.icon("scatter_plot").classes("text-3xl")
                    ui.label("已关闭温度因素").classes("description")
            else:
                chart = ui.echart(
                    chart_options(self.samples, self.params, self.dark, scatter=scatter)
                ).classes("plot")
                self.charts.append((chart, scatter))

    def render_parameters(self) -> None:
        with ui.element("div").classes("parameter-grid w-full"):
            with ui.column().classes("parameter-fields"):
                ui.select(
                    {15: "15 分钟", 30: "30 分钟", 60: "1 小时", 120: "2 小时"},
                    value=self.draft_interval,
                    label="采样间隔",
                    on_change=lambda e: setattr(self, "draft_interval", e.value),
                ).props("outlined dense")
                ui.number(
                    "功率参考阈值 (kW)",
                    value=self.draft_threshold,
                    min=0,
                    max=300,
                    on_change=lambda e: setattr(self, "draft_threshold", e.value),
                ).props("outlined dense")
                ui.switch(
                    "包含室外温度因素",
                    value=self.draft_temperature,
                    on_change=lambda e: setattr(self, "draft_temperature", e.value),
                )
                ui.button("应用参数并分析", icon="play_arrow", on_click=self.run_analysis).props(
                    "unelevated no-caps"
                ).classes("primary-btn")
            with ui.column().classes("parameter-help"):
                ui.label("关于本次分析").classes("font-semibold")
                ui.label("参数只影响当前浏览器中的演示分析，不会下发控制或修改现场配置。")
                ui.label("参考阈值作为时序图的虚线显示。因素开关控制温度曲线与散点图。")
                ui.label("如需改变算法逻辑，请通过 Codex 修改代码。")

    def run_analysis(self) -> None:
        try:
            params = Parameters(
                interval=int(self.draft_interval),
                temperature=self.draft_temperature,
                threshold=float(self.draft_threshold),
            )
            samples = generate_samples(
                self.project_id,
                self.device,
                date.fromisoformat(self.start),
                date.fromisoformat(self.end),
                params,
            )
        except (ValueError, TypeError):
            ui.notify("请检查日期范围（最多 31 天）及参数，功率阈值需为 0–300 kW。", type="warning")
            return
        self.samples, self.params = samples, params
        self.result_device = self.device
        self.stale = False
        self.records[self.project_id].insert(
            0,
            {
                "id": f"{self.project_id}-{len(self.records[self.project_id]) + 1}",
                "time": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
                "device": self.device,
                "version": "v0.1",
                "range": f"{self.start} — {self.end}",
                "status": "已完成 · 演示",
                "points": len(samples),
            },
        )
        self.active_tab = TABS[0]
        ui.notify(f"已更新演示分析 · {len(samples)} 个采样点", type="positive")
        self.render()

    def download(self) -> None:
        if self.stale:
            ui.notify("请先更新分析再导出。", type="warning")
            return
        ui.download(
            export_csv(self.project_id, self.device, self.samples),
            f"{self.project_id}-demo-analysis.csv",
            media_type="text/csv",
        )

    def render_history(self, full: bool = False) -> None:
        records = self.records[self.project_id]
        ui.label("运行记录" if full else "最近运行记录").classes("history-title")
        if not records:
            ui.label("暂无运行记录。点击“更新分析”生成当前项目的第一条演示记录。").classes(
                "description py-3"
            )
        else:
            columns = [
                {"name": name, "label": label, "field": name, "align": "left"}
                for name, label in [
                    ("time", "运行时间"),
                    ("device", "设备"),
                    ("range", "数据范围"),
                    ("version", "算法版本"),
                    ("status", "状态"),
                    ("points", "采样点"),
                ]
            ]
            table = (
                ui.table(columns=columns, rows=records if full else records[:3], row_key="id")
                .props("flat hide-bottom separator=horizontal")
                .classes("data-table")
            )
            table.add_slot(
                "body-cell-status",
                """
                <q-td :props="props">
                  <span class="run-success">
                    <q-icon name="check_circle" size="14px" />{{ props.value }}
                  </span>
                </q-td>
            """,
            )
        if full:
            ui.label("记录仅保存在当前浏览器会话，刷新页面后清空。").classes("description mt-3")

    def render_overview(self) -> None:
        self.heading(
            "项目概览", "选择项目查看已接入的数据与专题。下次打开时，将自动恢复上次选择的项目。"
        )
        with ui.element("div").classes("overview-list"):
            for project in PROJECTS:
                with ui.column().classes(
                    "project-tile" + (" current-project" if project.id == self.project_id else "")
                ):
                    ui.icon("domain").classes("text-3xl")
                    ui.label(project.name).classes("project-name")
                    if project.id == self.project_id:
                        ui.label("当前项目").classes("demo-badge")
                    ui.label(project.description).classes("description")
                    ui.label(
                        "小麦只读分析 · 1#空调机组"
                        if project.id == "hangxin-airport"
                        else "现场连接待配置 · 示例设备"
                    ).classes("description")
                    ui.button(
                        "继续分析"
                        if project.id == self.project_id and project.id == "hangxin-airport"
                        else "查看项目"
                        if project.id == self.project_id
                        else "进入项目",
                        icon="arrow_forward",
                        on_click=lambda project_id=project.id: self.enter_project(project_id),
                    ).props(f'unelevated no-caps aria-label="进入{project.name}"').classes(
                        "primary-btn"
                    )

    def render_module(self) -> None:
        content = {
            "data": (
                "数据分析",
                "query_stats",
                "项目数据分析等待接入",
                "当前项目尚未配置数据分析专题，请先完成项目数据源与测点接入。",
            ),
            "algorithms": (
                "算法与规则",
                "code",
                "规则工作区已预留",
                "通过 Codex 开发算法，在这里查看逻辑、配置参数并检验运行结果。",
            ),
            "models": (
                "模型实验",
                "science",
                "准备你的第一个模型实验",
                "后续接入历史数据，选择预测目标与因素，对比模型误差和实验结果。",
            ),
            "tasks": (
                "任务中心",
                "event_repeat",
                "尚未配置定时任务",
                "后续可开启或暂停计划，查看每次执行记录。当前没有运行任何定时任务。",
            ),
            "interfaces": (
                "接口与设备",
                "cable",
                "现场接口等待接入",
                "接口文档已按三个项目建立目录。查询、控制连接与设备测点尚未配置。",
            ),
        }
        title, icon, subtitle, description = content[self.section]
        self.heading(title)
        with ui.column().classes("empty-state"):
            ui.icon(icon)
            ui.label(subtitle).classes("empty-title")
            ui.label(description).classes("empty-copy")
            ui.button(
                "查看功率与温度分析" if self.project_id == "hangxin-airport" else "返回项目概览",
                icon="arrow_forward",
                color=None,
                on_click=lambda: self.navigate(
                    "topic" if self.project_id == "hangxin-airport" else "overview"
                ),
            ).props("flat no-caps").classes("secondary-btn mt-2")


async def create_page(request: Request) -> RedirectResponse | None:
    session = getattr(request.state, "auth_session", None)
    if not isinstance(session, Session) or sessions.get(session.token) is None:
        return RedirectResponse("/login", status_code=303)
    client = ui.context.client
    bind_client(client, session)
    workbench = Workbench(session)

    def revoke_client() -> None:
        # 撤销所有已打开页，后台查询只能退出，不能发布迟到数据。
        workbench.power_view.deactivate()
        workbench.ahu_view.deactivate()
        workbench.energy_view.deactivate()
        workbench.flowchart_view.deactivate()
        workbench.flight_view.deactivate()
        workbench.flight_view.snapshot = None
        if not client.is_deleted:
            client.delete()

    unsubscribe = sessions.on_revoke(session.token, revoke_client)
    client.on_delete(unsubscribe)
    workbench.build()
    ui.context.client.on_disconnect(workbench.flight_view.deactivate)
    ui.context.client.on_connect(workbench.flight_view.reconnect)
    ui.context.client.on_disconnect(workbench.power_view.deactivate)
    ui.context.client.on_connect(workbench.power_view.reconnect)
    ui.context.client.on_disconnect(workbench.ahu_view.deactivate)
    ui.context.client.on_connect(workbench.ahu_view.reconnect)
    ui.context.client.on_disconnect(workbench.energy_view.deactivate)
    ui.context.client.on_connect(workbench.energy_view.reconnect)
    ui.context.client.on_disconnect(workbench.flowchart_view.deactivate)
    ui.context.client.on_connect(workbench.flowchart_view.reconnect)
    await workbench.initialize_preferences()
    await workbench.ahu_view.initialize_preferences()
    await workbench.energy_view.initialize_preferences()
    return None
