"""航信功率与温度专题；筛选、快照和图表选区均属于当前浏览器。"""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any

from nicegui import events, run, ui

from axiomforge.power_temperature import (
    INTERVAL,
    POINTS,
    SHANGHAI,
    QueryRange,
    Snapshot,
    export_snapshot,
    format_time,
    parse_range,
    statistics,
)
from axiomforge.ui.power_temperature_charts import scatter_options, trend_options
from axiomforge.xiaomai_source import XiaomaiSourceError, load_power_temperature


class PowerTemperatureView:
    """网络结果仅在原挂载与请求代号有效时发布，避免迟到响应污染新页面。"""

    def __init__(self) -> None:
        self.snapshot: Snapshot | None = None
        self.selection: QueryRange | None = None
        self.focus: float | None = None
        self.zoom: dict[str, float] = {}
        self.legend: dict[str, bool] = {}
        self.period = "today"
        self.start, self.end = self.preset_range("today")
        self.active_tab = "分析结果"
        self.mode = "zoom"
        self.loading = False
        self.dirty = False
        self.error = ""
        self.dark = False
        self.mounted = False
        self.generation = 0
        self._setting_form = False
        self.submitted_draft: tuple[str, str, str] | None = None
        self.trend: ui.echart | None = None
        self.scatter: ui.echart | None = None
        self.container: ui.column | None = None
        self.detail_selected = False

    @staticmethod
    def preset_range(period: str) -> tuple[str, str]:
        now = datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        midnight = now.replace(hour=0, minute=0)
        start, end = (
            (midnight - timedelta(days=1), midnight) if period == "yesterday" else (midnight, now)
        )
        return start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")

    @property
    def stale(self) -> bool:
        return self.snapshot is not None and self.submitted_draft != (
            self.period,
            self.start,
            self.end,
        )

    def deactivate(self) -> None:
        self.mounted = False
        self.generation += 1
        self.loading = False

    def reconnect(self) -> None:
        """短暂断线后恢复已挂载页面，不重新发布断线前的迟到结果。"""
        if self.container is not None and not self.container.is_deleted:
            self.mounted = True
            self.render_status()

    def discard_draft(self) -> None:
        if self.submitted_draft:
            self.period, self.start, self.end = self.submitted_draft
        else:
            self.period = "today"
            self.start, self.end = self.preset_range("today")
        self.dirty = False

    def build(self, dark: bool) -> None:
        self.dark, self.mounted = dark, True
        self.container = ui.column().classes("power-page w-full gap-0")
        with self.container:
            with ui.row().classes("breadcrumb"):
                ui.label("航信机场")
                ui.icon("chevron_right")
                ui.label("芒市机场 · 项目专题")
            with ui.row().classes("page-heading"):
                ui.label("功率与温度分析").classes("page-title")
                ui.label("真实数据 · 只读查询").classes("demo-badge")
            ui.label("1#空调机组 · 查看功率与室外温度随时间的变化。").classes("description")
            with ui.row().classes("power-toolbar w-full"):
                self.preset = (
                    ui.select(
                        {"today": "今天", "yesterday": "昨天", "custom": "自定义"},
                        value=self.period,
                        label="时间范围",
                        on_change=self.change_period,
                    )
                    .props('outlined dense aria-label="分析时间范围"')
                    .classes("power-preset")
                )
                self.start_input = (
                    ui.input(
                        "开始时间",
                        value=self.start,
                        on_change=lambda e: self.change_time("start", e.value),
                    )
                    .props('type=datetime-local outlined dense aria-label="分析开始时间"')
                    .classes("power-date")
                )
                self.end_input = (
                    ui.input(
                        "结束时间（不含）",
                        value=self.end,
                        on_change=lambda e: self.change_time("end", e.value),
                    )
                    .props('type=datetime-local outlined dense aria-label="分析结束时间"')
                    .classes("power-date")
                )
                self.query_button = (
                    ui.button("查询刷新", icon="refresh", on_click=self.refresh)
                    .props("unelevated no-caps")
                    .classes("primary-btn")
                )
                self.export_button = (
                    ui.button("导出CSV", icon="file_download", color=None, on_click=self.download)
                    .props("flat no-caps")
                    .classes("secondary-btn")
                )
                self.export_button.tooltip("导出完整查询范围，不受图表选区影响")
            ui.label("Asia/Shanghai · 上游2分钟数据 · 最长7天 · 手动查询").classes("description")
            self.status = ui.column().classes("w-full gap-1")
            with ui.tabs(on_change=lambda e: setattr(self, "active_tab", e.value)).classes(
                "power-tabs w-full"
            ) as tabs:
                ui.tab("分析结果")
                ui.tab("数据明细")
                ui.tab("测点与口径")
            with ui.tab_panels(tabs, value=self.active_tab).classes("power-panels w-full"):
                with ui.tab_panel("分析结果").classes("p-0"):
                    self.metrics = ui.column().classes("w-full gap-0")
                    self.results = ui.column().classes("w-full gap-0")
                    self.quality = ui.column().classes("w-full gap-1")
                with ui.tab_panel("数据明细").classes("p-0"):
                    self.details = ui.column().classes("w-full")
                with ui.tab_panel("测点与口径").classes("p-0"):
                    self.render_sources()
            self.render_status()
            self.render_results()

    def change_period(self, event: events.ValueChangeEventArguments) -> None:
        if self._setting_form:
            return
        self.period = str(event.value)
        if self.period != "custom":
            self.start, self.end = self.preset_range(self.period)
            self.sync_form()
        self.dirty = True
        self.render_status()

    def change_time(self, key: str, value: Any) -> None:
        if self._setting_form:
            return
        setattr(self, key, str(value or ""))
        self.period = "custom"
        self._setting_form = True
        self.preset.set_value("custom")
        self._setting_form = False
        self.dirty = True
        self.render_status()

    def sync_form(self) -> None:
        self._setting_form = True
        self.start_input.set_value(self.start)
        self.end_input.set_value(self.end)
        self._setting_form = False

    def render_status(self) -> None:
        if not self.mounted or self.status.is_deleted:
            return
        self.status.clear()
        with self.status:
            if self.loading:
                ui.linear_progress().props("indeterminate")
                ui.label("正在读取小麦数据…").classes("description")
            if self.error:
                ui.label(self.error).classes("flight-warning")
            if self.stale:
                ui.label("筛选已修改，请查询刷新；下方仍为上次成功快照。").classes("flight-warning")
            if self.snapshot:
                ui.label(
                    f"查询范围：{format_time(self.snapshot.query.start)} → "
                    f"{format_time(self.snapshot.query.end)}（不含结束）"
                    f" · 查询完成：{format_time(self.snapshot.queried_at)}"
                ).classes("description")
        self.query_button.set_enabled(not self.loading)
        self.query_button.set_text("查询中…" if self.loading else "查询刷新")
        for control in (self.preset, self.start_input, self.end_input):
            control.set_enabled(not self.loading)
        self.export_button.set_enabled(
            bool(self.snapshot) and not self.loading and not self.stale and not self.error
        )

    async def refresh(self) -> None:
        if self.loading or not self.mounted:
            return
        if self.period in {"today", "yesterday"}:
            self.start, self.end = self.preset_range(self.period)
            self.sync_form()
        try:
            query = parse_range(self.start, self.end)
        except ValueError as error:
            self.error = str(error)
            self.render_status()
            return
        self.loading, self.error = True, ""
        self.generation += 1
        generation = self.generation
        draft = (self.period, self.start, self.end)
        self.render_status()
        try:
            snapshot = await asyncio.wait_for(
                run.io_bound(load_power_temperature, query), timeout=90
            )
            if self.mounted and generation == self.generation:
                self.snapshot, self.submitted_draft = snapshot, draft
                self.selection, self.focus = None, None
                self.zoom, self.legend = {}, {}
                self.dirty = False
                self.render_results()
        except (XiaomaiSourceError, TimeoutError) as error:
            if self.mounted and generation == self.generation:
                self.error = (
                    str(error)
                    if isinstance(error, XiaomaiSourceError)
                    else "查询超过90秒，原快照已保留，请稍后手动刷新。"
                )
        finally:
            if self.mounted and generation == self.generation:
                self.loading = False
                self.render_status()

    def render_results(self) -> None:
        self.results.clear()
        self.trend, self.scatter = None, None
        if self.snapshot is None:
            self.metrics.clear()
            self.quality.clear()
            with self.results, ui.column().classes("empty-state"):
                ui.icon("query_stats")
                ui.label("查询真实功率与室外温度").classes("empty-title")
                configured = bool(
                    os.environ.get("AXIOMFORGE_XIAOMAI_ENV_FILE")
                    or os.environ.get("XIAOMAI_ACCOUNT")
                )
                ui.label(
                    "点击“查询刷新”读取这组测点的历史数据。"
                    if configured
                    else "尚未配置小麦连接，请在服务启动时指定外部配置文件。"
                ).classes("empty-copy")
            self.render_details()
            return
        with self.results:
            with ui.row().classes("power-chart-tools w-full"):
                ui.toggle(
                    {"zoom": "缩放查看", "select": "选择分析时段"},
                    value=self.mode,
                    on_change=self.change_mode,
                ).props("no-caps unelevated").classes("power-mode")
                ui.button(
                    "重置选区", color=None, icon="restart_alt", on_click=self.reset_selection
                ).props("flat no-caps")
                self.selection_label = ui.label().classes("description")
            with ui.element("div").classes("power-chart-grid"):
                with ui.column().classes("chart-panel gap-0"):
                    ui.label("功率与室外温度趋势").classes("chart-title")
                    self.trend = ui.echart(
                        trend_options(
                            self.snapshot,
                            self.dark,
                            self.selection,
                            self.focus,
                            self.zoom,
                            self.legend,
                        )
                    ).classes("power-plot")
                    self.trend.on("chart:brushEnd", self.brushed, ["areas"])
                    self.trend.on("chart:datazoom", self.zoomed, ["start", "end", "batch"])
                    self.trend.on("chart:legendselectchanged", self.legend_changed, ["selected"])
                with ui.column().classes("chart-panel gap-0"):
                    ui.label("功率—室外温度关系").classes("chart-title")
                    self.scatter = ui.echart(
                        scatter_options(self.snapshot, self.dark, self.selection),
                        on_point_click=self.point_clicked,
                    ).classes("power-plot")
                    self.correlation_label = ui.label().classes("power-correlation")
            ui.label(
                "相关性不代表因果；上游2分钟值的聚合含义待确认，本页不做插值或节能推断。"
            ).classes("insight")
            self.focus_label = ui.label().classes("description")
        self.update_analysis()
        if self.mode == "select":
            self.apply_mode()

    def update_analysis(self) -> None:
        if self.snapshot is None:
            return
        rows = self.snapshot.selected(self.selection)
        stats = statistics(rows)
        self.metrics.clear()
        with self.metrics:
            ui.label("选中时段统计" if self.selection else "完整查询范围统计").classes(
                "power-scope"
            )
            with ui.element("div").classes("power-metrics"):
                items = [
                    (
                        "平均功率",
                        "—" if stats.average_power is None else f"{stats.average_power:.2f} kW",
                        "有效功率样本算术平均",
                    ),
                    (
                        "最大功率",
                        "—" if stats.max_power is None else f"{stats.max_power:.2f} kW",
                        format_time(stats.max_at) if stats.max_at else "无有效功率数据",
                    ),
                    (
                        "室外温度范围",
                        "—"
                        if stats.min_temperature is None
                        else f"{stats.min_temperature:.1f}–{stats.max_temperature:.1f} ℃",
                        "有效温度样本范围",
                    ),
                    (
                        "有效配对比例",
                        f"{stats.pairs / stats.expected:.1%}" if stats.expected else "—",
                        f"{stats.pairs} / {stats.expected} 个期望时间点",
                    ),
                ]
                for label, value, note in items:
                    with ui.column().classes("power-metric"):
                        ui.label(label).classes("description")
                        ui.label(value).classes("power-value")
                        ui.label(note).classes("power-note")
        if self.trend and self.scatter:
            self.selection_label.set_text(
                f"选中：{format_time(self.selection.start)} → {format_time(self.selection.end)}"
                if self.selection
                else "分析范围：完整查询时段"
            )
            self.correlation_label.set_text(
                f"Pearson r = {stats.correlation:.3f} · {stats.pairs} 个配对"
                if stats.correlation is not None
                else f"相关系数不可计算：{stats.correlation_reason} · {stats.pairs} 个配对"
            )
        self.quality.clear()
        with self.quality:
            if not stats.power_count and not stats.temperature_count:
                ui.label("该时段两个测点均无有效数据。").classes("flight-warning")
            elif not stats.power_count or not stats.temperature_count:
                ui.label("该时段仅一个测点有有效数据，无法形成散点配对。").classes("flight-warning")
            ui.label(
                f"数据完整性：功率缺失／无效 {stats.expected - stats.power_count} 点；"
                f"温度缺失／无效 {stats.expected - stats.temperature_count} 点。真实0值保留。"
            ).classes("description")
            q = self.snapshot.quality
            ui.label(
                f"完整查询质量检查：非法时间 {q.invalid_times}，越界 {q.outside}，"
                f"网格外 {q.off_grid}，"
                f"重复 {q.duplicates}，冲突 {q.conflicts}。该检查不代表传感器精度。"
            ).classes("description")
            for name, values in (
                ("功率", [row.time for row in rows if row.power is not None]),
                ("温度", [row.time for row in rows if row.temperature is not None]),
            ):
                ui.label(
                    f"{name}最后有效数据：{format_time(values[-1]) if values else '无'}"
                ).classes("description")
        self.render_details()

    def update_charts(self) -> None:
        if not self.snapshot or not self.trend or not self.scatter:
            return
        options = trend_options(
            self.snapshot, self.dark, self.selection, self.focus, self.zoom, self.legend
        )
        # 不把旧dataZoom重新序列化，ECharts即可保留当前客户端的缩放状态。
        options.pop("dataZoom", None)
        options.pop("brush", None)
        self.trend.options.clear()
        self.trend.options.update(options)
        self.trend.update()
        self.scatter.options.clear()
        self.scatter.options.update(scatter_options(self.snapshot, self.dark, self.selection))
        self.scatter.update()

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        if self.mounted:
            self.update_charts()

    def change_mode(self, event: events.ValueChangeEventArguments) -> None:
        self.mode = str(event.value)
        self.apply_mode()

    def apply_mode(self) -> None:
        if self.trend:
            self.trend.run_chart_method(
                "dispatchAction",
                {
                    "type": "takeGlobalCursor",
                    "key": "brush",
                    "brushOption": {"brushType": "lineX" if self.mode == "select" else False},
                },
            )
            self.trend.run_chart_method(
                "setOption", {"dataZoom": [{"disabled": self.mode == "select"}]}
            )

    def brushed(self, event: events.GenericEventArguments) -> None:
        if not self.snapshot or self.mode != "select":
            return
        areas = event.args.get("areas", [])
        if not areas:
            return
        coord = areas[0].get("coordRange")
        if not isinstance(coord, list) or len(coord) != 2:
            return
        try:
            start, end = sorted(float(value) for value in coord)
        except (TypeError, ValueError):
            return
        selected = [
            row for row in self.snapshot.rows if start <= row.time.timestamp() * 1000 <= end
        ]
        if selected:
            self.selection = QueryRange(
                selected[0].time, min(selected[-1].time + INTERVAL, self.snapshot.query.end)
            )
            self.update_charts()
            self.update_analysis()

    def reset_selection(self) -> None:
        self.selection = None
        if self.trend:
            self.trend.run_chart_method("dispatchAction", {"type": "brush", "areas": []})
        self.update_charts()
        self.update_analysis()

    def zoomed(self, event: events.GenericEventArguments) -> None:
        args = event.args
        value = args.get("batch", [args])[0] if args.get("batch") else args
        if isinstance(value, dict):
            self.zoom = {
                key: float(value[key])
                for key in ("start", "end")
                if isinstance(value.get(key), (float, int))
            }

    def legend_changed(self, event: events.GenericEventArguments) -> None:
        self.legend = dict(event.args.get("selected", {}))

    def point_clicked(self, event: events.EChartPointClickEventArguments) -> None:
        if (
            not self.snapshot
            or not self.trend
            or not isinstance(event.data, list)
            or len(event.data) < 3
        ):
            return
        self.focus = float(event.data[2])
        time = datetime.fromtimestamp(self.focus / 1000, SHANGHAI)
        if not any(row.time == time and row.paired for row in self.snapshot.rows):
            return
        self.focus_label.set_text(
            f"已定位：{format_time(time)} · 功率 {event.data[1]} kW · 室外温度 {event.data[0]} ℃"
        )
        self.update_charts()
        # 定位改变视口而不改分析选区；保留约2小时上下文。
        start = max(self.snapshot.query.start, time - timedelta(hours=1))
        end = min(self.snapshot.query.end, time + timedelta(hours=1))
        self.trend.run_chart_method(
            "dispatchAction",
            {
                "type": "dataZoom",
                "startValue": start.timestamp() * 1000,
                "endValue": end.timestamp() * 1000,
            },
        )
        self.trend.run_chart_method(
            "dispatchAction",
            {
                "type": "showTip",
                "seriesIndex": 0,
                "dataIndex": next(
                    i for i, row in enumerate(self.snapshot.rows) if row.time == time
                ),
            },
        )

    def render_details(self) -> None:
        self.details.clear()
        with self.details:
            if self.snapshot is None:
                ui.label("查询后显示数据明细。").classes("description")
                return
            ui.switch(
                "仅看选中分析时段", value=self.detail_selected, on_change=self.change_detail_scope
            )
            rows = self.snapshot.selected(self.selection if self.detail_selected else None)
            ui.label(
                f"{'选中分析时段' if self.detail_selected and self.selection else '完整查询范围'}"
                f" · {len(rows)}行；CSV始终导出完整查询范围。"
            ).classes("description")
            columns = [
                {"name": key, "label": label, "field": key, "align": "left"}
                for key, label in [
                    ("time", "时间 (Asia/Shanghai)"),
                    ("power", "功率 (kW)"),
                    ("temperature", "室外温度 (℃)"),
                    ("status", "数据状态"),
                ]
            ]
            ui.table(
                columns=columns,
                rows=[
                    {
                        "time": format_time(row.time),
                        "power": row.power if row.power is not None else "—",
                        "temperature": row.temperature if row.temperature is not None else "—",
                        "status": "有效配对"
                        if row.paired
                        else f"功率：{row.power_status}；温度：{row.temperature_status}",
                    }
                    for row in rows
                ],
                row_key="time",
                pagination=50,
            ).props("flat separator=horizontal").classes("data-table")

    def change_detail_scope(self, event: events.ValueChangeEventArguments) -> None:
        self.detail_selected = bool(event.value)
        self.render_details()

    def render_sources(self) -> None:
        ui.label("固定分析来源 · 芒市机场（项目832）").classes("history-title")
        for spec in POINTS:
            with ui.column().classes("power-source w-full gap-1"):
                ui.label(f"{spec.device_name} → {spec.point_name}").classes("font-semibold")
                ui.label(f"设备ID：{spec.device_id} · 单位：{spec.unit}").classes("description")
                ui.label(spec.point_id).classes("power-point-id")
        ui.label("口径说明").classes("history-title")
        for text in [
            "时间按Asia/Shanghai解释，查询范围左闭右开；上游返回2分钟数据，具体聚合函数待确认。",
            "平均功率是有效功率样本算术平均；最大功率并列时取首次时间。温度统计不要求功率同时有效。",
            "配对比例分母为请求范围内2分钟网格的期望点数；散点与Pearson只使用同时间戳的有效配对。",
            "少于3个配对，或任一序列恒定，相关系数不可计算。相关性不代表因果、预测效果或节能空间。",
            "缺失保留断点，不插值、不自动剔除极端值；真实0与缺失分开。重复冲突使该时间该测点无效。",
            "CSV导出完整成功查询快照；当前图表选区不会删减导出。查询失败或筛选待更新时不能导出。",
            "本页仅查询，不执行设备控制。认证在服务端完成，密码和Token不会发送到浏览器。",
        ]:
            ui.label(text).classes("description")

    def download(self) -> None:
        if self.snapshot is None or self.loading or self.stale or self.error:
            ui.notify("请先成功查询当前范围再导出。", type="warning")
            return
        ui.download(
            export_snapshot(self.snapshot), "hangxin-power-temperature.csv", media_type="text/csv"
        )
