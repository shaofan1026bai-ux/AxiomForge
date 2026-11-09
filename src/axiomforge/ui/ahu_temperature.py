"""AHU回风温度专题；条件可恢复，成功快照仅属于当前浏览器。"""

import json
import math
import threading
import time
from dataclasses import replace
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any

from nicegui import events, run, ui

from axiomforge.ahu_temperature import (
    DEVICES,
    PERIODS,
    PREFERENCE_KEY,
    SHANGHAI,
    STATE_LABELS,
    DeviceResult,
    QueryConditions,
    Snapshot,
    Statistics,
    default_conditions,
    export_notes,
    export_snapshot,
    preset_dates,
    resolve_query,
    restore_conditions,
)
from axiomforge.ahu_temperature_source import (
    AhuSourceError,
    Progress,
    QueryCancelled,
    load_ahu_temperature,
)
from axiomforge.ui.ahu_temperature_charts import (
    daily_options,
    detail_options,
    distribution_options,
    range_options,
)

TABS = ("总览对比", "逐日与明细", "数据质量与口径")


def value_text(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def range_text(stats: Statistics, major: bool = False) -> str:
    if not stats.count:
        return "无有效数据"
    low, high = (stats.p05, stats.p95) if major else (stats.minimum, stats.maximum)
    return f"{value_text(low)}～{value_text(high)}"


def format_time(value: datetime | None) -> str:
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") if value else "无"


class AhuTemperatureView:
    """每个请求有独立停止标志和代号，迟到进度与结果不能写入新页面。"""

    def __init__(self) -> None:
        self.conditions = default_conditions()
        self.submitted_conditions: QueryConditions | None = None
        self.snapshot: Snapshot | None = None
        self.active_tab = TABS[0]
        self.device_id = DEVICES[0].id
        self.detail_day = ""
        self.gap_page = 1
        self.running = True
        self.sort_by = "id"
        self.loading = False
        self.dirty = False
        self.error = ""
        self.dark = False
        self.mounted = False
        self.generation = 0
        self.progress: Progress | None = None
        self.started_at = 0.0
        self.container: ui.column | None = None
        self.charts: dict[str, ui.echart] = {}
        self._cancel_event = threading.Event()
        self._preferences_initialized = False
        self._preference_revision = 0
        self._setting_form = False
        self._status_signature: tuple[object, ...] | None = None

    @property
    def stale(self) -> bool:
        return self.snapshot is not None and self.submitted_conditions != self.conditions

    @property
    def can_export(self) -> bool:
        return self.snapshot is not None and not self.loading and not self.stale and not self.error

    async def initialize_preferences(self) -> None:
        """只恢复条件，不自动查询；读取期间的用户编辑优先于旧偏好。"""
        if self._preferences_initialized:
            return
        revision = self._preference_revision
        try:
            settings = await ui.run_javascript(
                f"return (() => {{ try {{ return JSON.parse(localStorage.getItem("
                f"{json.dumps(PREFERENCE_KEY)}) || 'null'); }} catch (_) {{ return null; }} }})()"
            )
        except (TimeoutError, RuntimeError):
            settings = None
        self._preferences_initialized = True
        if revision != self._preference_revision:
            return
        self.conditions = restore_conditions(settings)
        self.dirty = False
        if self.mounted:
            self.sync_form()
            self.render_status()

    def save_preferences(self, conditions: QueryConditions) -> None:
        """仅保存合法提交；浏览器禁用存储不会阻断当前会话查询。"""
        payload = json.dumps(json.dumps(conditions.preference(), ensure_ascii=False))
        ui.run_javascript(
            f"try {{ localStorage.setItem({json.dumps(PREFERENCE_KEY)},"
            f"{payload}); }} catch (_) {{}}"
        )

    def deactivate(self) -> None:
        self._cancel_event.set()
        self.generation += 1
        self.loading = False
        self.mounted = False

    def reconnect(self) -> None:
        if self.container is not None and not self.container.is_deleted:
            self.mounted = True
            self.render_status()

    def discard_draft(self) -> None:
        self.conditions = self.submitted_conditions or default_conditions()
        self._preference_revision += 1
        self.dirty = False

    def build(self, dark: bool) -> None:
        self.dark, self.mounted = dark, True
        self.charts.clear()
        ui.add_css(Path(__file__).with_suffix(".css").read_text())
        self.container = ui.column().classes("ahu-page gap-0")
        with self.container:
            with ui.row().classes("breadcrumb"):
                ui.label("航信机场")
                ui.icon("chevron_right")
                ui.label("芒市机场 · 项目专题")
            with ui.row().classes("page-heading"):
                ui.label("AHU回风温度分析").props('role="heading" aria-level="1"').classes(
                    "page-title"
                )
                ui.label("真实数据 · 只读查询").classes("demo-badge")
            ui.label("AHU1～5 · 比较指定日期与每日时段内的回风温度范围。").classes("description")
            self.build_form()
            self.status = ui.column().classes("ahu-status")
            with ui.row().classes("section-tabs w-full"):
                self.tab_buttons = {}
                for name in TABS:
                    self.tab_buttons[name] = (
                        ui.button(
                            name, color=None, on_click=lambda name=name: self.change_tab(name)
                        )
                        .props("flat no-caps")
                        .classes("section-tab" + (" active" if self.active_tab == name else ""))
                    )
            self.results = ui.column().classes("ahu-panel")
            ui.label(
                "采样统计仅描述本次有效数据，不推断精确开机时长、舒适度达标或设备故障；不执行控制。"
            ).classes("insight")
            ui.timer(0.5, self.poll_progress)
        self.sync_form()
        self.render_status()
        self.render_results()

    def build_form(self) -> None:
        with ui.row().classes("ahu-toolbar w-full"):
            self.preset = (
                ui.select(
                    PERIODS,
                    value=self.conditions.period,
                    label="日期预设",
                    on_change=self.change_period,
                )
                .props('outlined dense aria-label="AHU日期预设"')
                .classes("ahu-preset")
            )
            self.start_input = (
                ui.input(
                    "开始日期",
                    value=self.conditions.start,
                    on_change=lambda e: self.change_date("start", e.value),
                )
                .props('type=date outlined dense aria-label="AHU开始日期"')
                .classes("ahu-date")
            )
            self.end_input = (
                ui.input(
                    "结束日期（含当天）",
                    value=self.conditions.end,
                    on_change=lambda e: self.change_date("end", e.value),
                )
                .props('type=date outlined dense aria-label="AHU结束日期"')
                .classes("ahu-date")
            )
            self.daily_switch = ui.switch(
                "每日固定时段", value=self.conditions.daily, on_change=self.change_daily
            )
            with ui.row().classes("ahu-time-fields") as self.time_fields:
                self.from_input = (
                    ui.input(
                        "开始时刻",
                        value=self.conditions.start_time,
                        on_change=lambda e: self.change_clock("start_time", e.value),
                    )
                    .props('type=time outlined dense aria-label="每日开始时刻"')
                    .classes("ahu-time")
                )
                self.to_input = (
                    ui.input(
                        "结束时刻",
                        value=self.conditions.end_time,
                        on_change=lambda e: self.change_clock("end_time", e.value),
                    )
                    .props('type=time outlined dense aria-label="每日结束时刻"')
                    .classes("ahu-time")
                )
            self.query_button = (
                ui.button("查询统计", icon="search", on_click=self.refresh)
                .props("unelevated no-caps")
                .classes("primary-btn")
            )
            self.stop_button = (
                ui.button("停止等待", color=None, on_click=self.stop_waiting)
                .props("flat no-caps")
                .classes("secondary-btn")
            )
            ui.button("恢复默认", color=None, on_click=self.reset_conditions).props(
                "flat no-caps"
            ).classes("secondary-btn")
            with (
                ui.button("导出完整范围", icon="file_download", color=None)
                .props("flat no-caps")
                .classes("secondary-btn") as self.export_button
            ):
                with ui.menu():
                    for kind, label in (
                        ("summary", "五台汇总 CSV"),
                        ("daily", "逐日统计 CSV"),
                        ("distribution", "温区分布 CSV"),
                        ("quality", "质量明细 CSV"),
                        ("notes", "统计口径说明"),
                    ):
                        ui.menu_item(label, on_click=lambda kind=kind: self.download(kind))

    def sync_form(self) -> None:
        self._setting_form = True
        try:
            self.preset.set_value(self.conditions.period)
            self.start_input.set_value(self.conditions.start)
            self.end_input.set_value(self.conditions.end)
            self.daily_switch.set_value(self.conditions.daily)
            self.from_input.set_value(self.conditions.start_time)
            self.to_input.set_value(self.conditions.end_time)
            self.time_fields.set_visibility(self.conditions.daily)
        finally:
            self._setting_form = False

    def edited(self) -> None:
        self._preference_revision += 1
        self.dirty = True
        self.error = ""
        self.render_status()

    def change_period(self, event: events.ValueChangeEventArguments) -> None:
        if self._setting_form:
            return
        period = str(event.value)
        if period not in PERIODS:
            return
        try:
            start, end = (
                preset_dates(period)
                if period != "custom"
                else (self.conditions.start, self.conditions.end)
            )
        except ValueError as error:
            self.error = str(error)
            self.sync_form()
            self.render_status()
            return
        self.conditions = replace(self.conditions, period=period, start=start, end=end)
        self.sync_form()
        self.edited()

    def change_date(self, key: str, value: Any) -> None:
        if self._setting_form:
            return
        if key == "start":
            self.conditions = replace(self.conditions, period="custom", start=str(value or ""))
        elif key == "end":
            self.conditions = replace(self.conditions, period="custom", end=str(value or ""))
        else:
            return
        self.sync_form()
        self.edited()

    def change_daily(self, event: events.ValueChangeEventArguments) -> None:
        if self._setting_form:
            return
        self.conditions = replace(self.conditions, daily=bool(event.value))
        self.time_fields.set_visibility(self.conditions.daily)
        self.edited()

    def change_clock(self, key: str, value: Any) -> None:
        if self._setting_form:
            return
        if key == "start_time":
            self.conditions = replace(self.conditions, start_time=str(value or ""))
        elif key == "end_time":
            self.conditions = replace(self.conditions, end_time=str(value or ""))
        else:
            return
        self.edited()

    def reset_conditions(self) -> None:
        if self.loading:
            return
        self.conditions = default_conditions()
        self.sync_form()
        self.edited()

    def render_status(self) -> None:
        if not self.mounted or not hasattr(self, "status") or self.status.is_deleted:
            return
        self.status.clear()
        with self.status:
            try:
                ui.label("待查询条件：" + resolve_query(self.conditions).summary).classes(
                    "description"
                )
            except ValueError as error:
                ui.label(str(error)).classes("ahu-warning")
            if self.loading:
                p = self.progress
                ui.linear_progress(value=p.completed / p.total if p and p.total else 0)
                elapsed = int(time.monotonic() - self.started_at)
                ui.label(
                    f"已完成 {p.completed}/{p.total} 片 · {p.label} · 已用 {elapsed} 秒"
                    if p
                    else f"正在核验测点并准备查询 · 已用 {elapsed} 秒"
                ).classes("description")
            if self.error:
                ui.label(self.error).classes("ahu-warning")
            if self.stale:
                ui.label("条件已修改，下方仍为上次成功快照；请查询更新后导出。").classes(
                    "ahu-warning"
                )
            if self.snapshot:
                ui.label("成功快照：" + self.snapshot.query.summary).classes("description")
                ui.label("查询完成：" + format_time(self.snapshot.queried_at)).classes(
                    "description"
                )
        self.query_button.set_enabled(not self.loading)
        self.query_button.set_text("查询中…" if self.loading else "查询统计")
        self.stop_button.set_visibility(self.loading)
        self.export_button.set_enabled(self.can_export)
        for control in (
            self.preset,
            self.start_input,
            self.end_input,
            self.daily_switch,
            self.from_input,
            self.to_input,
        ):
            control.set_enabled(not self.loading)

    def poll_progress(self) -> None:
        if not self.loading or not self.mounted:
            return
        signature = (self.progress, int(time.monotonic() - self.started_at))
        if signature != self._status_signature:
            self._status_signature = signature
            self.render_status()

    async def refresh(self) -> None:
        if self.loading or not self.mounted:
            return
        self._preference_revision += 1
        try:
            query = resolve_query(self.conditions)
        except ValueError as error:
            self.error = str(error)
            self.render_status()
            return
        self.conditions = query.conditions
        if hasattr(self, "preset"):
            self.sync_form()
        self.save_preferences(query.conditions)
        self.loading, self.error, self.progress = True, "", None
        self.started_at = time.monotonic()
        self.generation += 1
        generation = self.generation
        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event
        self.render_status()

        def update_progress(progress: Progress) -> None:
            # 后台线程只替换不可变进度；UI由自身计时器更新。
            if generation == self.generation and not cancel_event.is_set():
                self.progress = progress

        try:
            snapshot = await run.io_bound(
                load_ahu_temperature, query, progress=update_progress, cancelled=cancel_event.is_set
            )
            if not isinstance(snapshot, Snapshot):
                raise AhuSourceError("查询未返回完整快照，请重试；原结果已保留。")
            if self.mounted and generation == self.generation and not cancel_event.is_set():
                self.snapshot = snapshot
                self.submitted_conditions = query.conditions
                self.dirty = False
                self.detail_day = snapshot.query.windows[0].day.isoformat()
                self.gap_page = 1
                self.render_results()
        except QueryCancelled:
            if self.mounted and generation == self.generation:
                self.error = "已停止等待，原快照保留；已发送请求会在完成或超时后退出。"
        except AhuSourceError as error:
            if self.mounted and generation == self.generation:
                self.error = str(error)
        except Exception:
            if self.mounted and generation == self.generation:
                self.error = "统计查询未完成，请稍后重试；原快照已保留。"
        finally:
            if self.mounted and generation == self.generation:
                self.loading = False
                self.render_status()

    def stop_waiting(self) -> None:
        if not self.loading:
            return
        self._cancel_event.set()
        self.generation += 1
        self.loading = False
        self.error = "已停止等待，原快照保留；已发送请求会在完成或超时后退出。"
        self.render_status()

    def change_tab(self, name: str) -> None:
        self.active_tab = name
        for key, button in self.tab_buttons.items():
            button.classes(
                add="active" if key == name else "", remove="" if key == name else "active"
            )
        self.render_results()

    def render_results(self) -> None:
        if not self.mounted or not hasattr(self, "results") or self.results.is_deleted:
            return
        self.charts.clear()
        self.results.clear()
        with self.results:
            if self.snapshot is None:
                if self.active_tab == TABS[2]:
                    self.render_sources()
                else:
                    with ui.column().classes("empty-state"):
                        ui.icon("thermostat")
                        ui.label("查询五台AHU回风温度").classes("empty-title")
                        ui.label(
                            "选择日期与可选每日时段，点击“查询统计”。上次条件会恢复，数据不会自动读取。"
                        ).classes("empty-copy")
                return
            if self.active_tab == TABS[0]:
                self.render_overview()
            elif self.active_tab == TABS[1]:
                self.render_daily()
            else:
                self.render_quality()

    def render_overview(self) -> None:
        if self.snapshot is None:
            return
        with ui.row().classes("ahu-result-title w-full"):
            ui.label("五台设备对比 · ℃").classes("font-semibold")
            ui.label("主表同时保留不限启停和确认开机统计").classes("description")
        ui.select(
            {
                "id": "设备编号",
                "mean_desc": "开机平均：高到低",
                "mean_asc": "开机平均：低到高",
                "p95_desc": "开机P95：高到低",
                "p95_asc": "开机P95：低到高",
            },
            value=self.sort_by,
            label="表格排序（无值始终在末尾）",
            on_change=self.change_sort,
        ).props("outlined dense").classes("ahu-sort")
        columns = [
            {
                "name": key,
                "label": label,
                "field": key,
                "align": "left",
                "sortable": False,
            }
            for key, label in (
                ("id", "设备（点击查看详情）"),
                (
                    "all",
                    "所选时段最低—最高（不限启停）"
                    if self.snapshot.query.conditions.daily
                    else "全天最低—最高",
                ),
                ("running", "开机最低—最高"),
                ("major", "开机P05～P95"),
                ("mean", "开机平均"),
                ("p95", "开机P95"),
                ("counts", "温度／开机有效样本"),
                ("quality", "数据提示"),
            )
        ]
        rows = []
        for device in self.sorted_devices():
            q = device.quality
            notes = []
            if q.temperature_valid < q.expected:
                notes.append(f"温度缺失/无效 {q.expected - q.temperature_valid}")
            if q.unconfirmed:
                notes.append(f"温度有效但状态未确认 {q.unconfirmed}")
            rows.append(
                {
                    "id": device.spec.id,
                    "all": range_text(device.all_stats),
                    "running": range_text(device.running_stats),
                    "major": range_text(device.running_stats, True),
                    "mean": round(device.running_stats.mean, 1)
                    if device.running_stats.mean is not None
                    else None,
                    "p95": round(device.running_stats.p95, 1)
                    if device.running_stats.p95 is not None
                    else None,
                    "counts": f"{device.all_stats.count} / {device.running_stats.count}",
                    "quality": "；".join(notes) or "查看质量口径",
                }
            )
        with ui.element("div").classes("ahu-table-wrap"):
            table = (
                ui.table(columns=columns, rows=rows, row_key="id")
                .props("flat hide-bottom separator=horizontal")
                .classes("data-table ahu-summary")
            )
            for field in ("mean", "p95"):
                table.add_slot(
                    f"body-cell-{field}",
                    '<q-td :props="props">{{ props.value == null ? "—" : '
                    "Number(props.value).toFixed(1) }}</q-td>",
                )
            table.on("rowClick", self.summary_clicked)
        self.render_leaders()
        with ui.row().classes("ahu-view-tools"):
            ui.label("下方两图统计口径").classes("description")
            ui.toggle(
                {True: "确认开机", False: "不限启停"},
                value=self.running,
                on_change=self.change_running,
            ).props("unelevated no-caps")
        with ui.element("div").classes("ahu-chart-grid"):
            with ui.column().classes("chart-panel gap-0"):
                ui.label("温度区间 · ℃").classes("chart-title")
                ui.label("细线：极值 · 粗线：P05～P95 · 圆点：样本平均").classes("description")
                self.charts["range"] = ui.echart(
                    range_options(self.snapshot, self.dark, self.running),
                    on_point_click=self.chart_device_clicked,
                ).classes("ahu-plot")
            with ui.column().classes("chart-panel gap-0"):
                ui.label("温区分布 · 有效采样占比").classes("chart-title")
                ui.label("左闭右开；不代表精确持续时长，无样本设备不绘制百分比。").classes(
                    "description"
                )
                self.charts["distribution"] = ui.echart(
                    distribution_options(self.snapshot, self.dark, self.running),
                    on_point_click=self.chart_device_clicked,
                ).classes("ahu-plot")

    def render_leaders(self) -> None:
        if self.snapshot is None:
            return
        values = [
            device for device in self.snapshot.devices if device.running_stats.mean is not None
        ]
        if not values:
            ui.label("当前窗口没有可确认的有效开机温度样本。").classes("ahu-warning")
            return
        means = [
            device.running_stats.mean for device in values if device.running_stats.mean is not None
        ]
        high, low = max(means), min(means)
        highest = "、".join(d.spec.id for d in values if d.running_stats.mean == high)
        lowest = "、".join(d.spec.id for d in values if d.running_stats.mean == low)
        ui.label(
            f"在有有效开机样本的设备中：{highest}开机样本平均最高（{high:.1f}℃）；"
            f"{lowest}最低（{low:.1f}℃）。"
        ).classes("insight")

    def sorted_devices(self) -> tuple[DeviceResult, ...]:
        if self.snapshot is None:
            return ()
        if self.sort_by == "id":
            return self.snapshot.devices
        field, direction = self.sort_by.split("_", 1)

        def key(device: DeviceResult) -> tuple[bool, float]:
            value = getattr(device.running_stats, field)
            return value is None, (
                value if direction == "asc" else -value
            ) if value is not None else 0

        return tuple(sorted(self.snapshot.devices, key=key))

    def change_sort(self, event: events.ValueChangeEventArguments) -> None:
        if event.value in {"id", "mean_asc", "mean_desc", "p95_asc", "p95_desc"}:
            self.sort_by = str(event.value)
            self.render_results()

    def summary_clicked(self, event: events.GenericEventArguments) -> None:
        args = event.args
        row = (
            args[1]
            if isinstance(args, list) and len(args) > 1
            else args.get("row")
            if isinstance(args, dict)
            else None
        )
        if isinstance(row, dict) and row.get("id") in {d.id for d in DEVICES}:
            self.device_id = str(row["id"])
            self.change_tab(TABS[1])

    def change_running(self, event: events.ValueChangeEventArguments) -> None:
        self.running = bool(event.value)
        self.render_results()

    def chart_device_clicked(self, event: events.EChartPointClickEventArguments) -> None:
        if event.name in {device.id for device in DEVICES}:
            self.device_id = event.name
            self.change_tab(TABS[1])

    def selected_device(self) -> DeviceResult | None:
        if self.snapshot is None:
            return None
        return next((d for d in self.snapshot.devices if d.spec.id == self.device_id), None)

    def render_daily(self) -> None:
        device = self.selected_device()
        if device is None:
            return
        with ui.row().classes("ahu-view-tools"):
            ui.select(
                {d.id: d.name for d in DEVICES},
                value=self.device_id,
                label="设备",
                on_change=self.change_device,
            ).props("outlined dense")
            ui.toggle(
                {True: "确认开机", False: "不限启停"},
                value=self.running,
                on_change=self.change_running,
            ).props("unelevated no-caps")
        ui.label("逐日按窗口开始日期归属；无数据日期保留，点击一行查看完整窗口。").classes(
            "description"
        )
        self.charts["daily"] = ui.echart(daily_options(device, self.dark, self.running)).classes(
            "ahu-plot"
        )
        columns = [
            {"name": key, "label": label, "field": key, "align": "left"}
            for key, label in (
                ("day", "归属日期"),
                ("window", "实际窗口（结束不含）"),
                ("all", "不限启停范围 ℃"),
                ("running", "开机范围 ℃"),
                ("major", "开机P05～P95 ℃"),
                ("mean", "开机平均 ℃"),
                ("count", "温度／开机样本"),
            )
        ]
        rows = [
            {
                "day": d.window.day.isoformat(),
                "window": f"{format_time(d.window.start)} → {format_time(d.window.end)}",
                "all": range_text(d.all_stats),
                "running": range_text(d.running_stats),
                "major": range_text(d.running_stats, True),
                "mean": value_text(d.running_stats.mean),
                "count": f"{d.all_stats.count} / {d.running_stats.count}",
            }
            for d in device.daily
        ]
        with ui.element("div").classes("ahu-table-wrap"):
            table = (
                ui.table(columns=columns, rows=rows, row_key="day", pagination=10)
                .props("flat separator=horizontal")
                .classes("data-table")
            )
            table.on("rowClick", self.daily_clicked)
        options = {d.window.day.isoformat(): d.window.day.isoformat() for d in device.daily}
        if self.detail_day not in options:
            self.detail_day = next(iter(options), "")
        with ui.row().classes("ahu-view-tools"):
            ui.label("窗口详情").classes("font-semibold")
            ui.select(
                options, value=self.detail_day, label="按开始日期定位", on_change=self.change_day
            ).props("outlined dense")
        self.detail_container = ui.column().classes("ahu-panel")
        self.render_detail()

    def change_device(self, event: events.ValueChangeEventArguments) -> None:
        self.device_id = str(event.value)
        self.render_results()

    def change_day(self, event: events.ValueChangeEventArguments) -> None:
        self.detail_day = str(event.value)
        self.render_detail()

    def daily_clicked(self, event: events.GenericEventArguments) -> None:
        args = event.args
        row = (
            args[1]
            if isinstance(args, list) and len(args) > 1
            else args.get("row")
            if isinstance(args, dict)
            else None
        )
        if isinstance(row, dict) and isinstance(row.get("day"), str):
            self.detail_day = row["day"]
            self.render_results()

    def render_detail(self) -> None:
        device = self.selected_device()
        if device is None or not hasattr(self, "detail_container"):
            return
        self.detail_container.clear()
        with self.detail_container:
            ui.label(f"{device.spec.name} · {self.detail_day}开始的窗口").classes("font-semibold")
            ui.label(
                "详情仅定位，导出仍为全部五台完整提交范围；原始反馈和状态判定见下表。"
            ).classes("description")
            self.charts["detail"] = ui.echart(
                detail_options(device, self.detail_day, self.dark)
            ).classes("ahu-detail-plot")
            columns = [
                {"name": key, "label": label, "field": key, "align": "left"}
                for key, label in (
                    ("time", "北京时间"),
                    ("temperature", "回风 ℃"),
                    ("feedback", "运行反馈"),
                    ("frequency", "频率 Hz"),
                    ("state", "运行判定"),
                    ("quality", "温度／反馈／频率质量"),
                )
            ]
            rows = [
                {
                    "time": format_time(row.time),
                    "temperature": row.temperature if row.temperature is not None else "—",
                    "feedback": row.feedback if row.feedback is not None else "—",
                    "frequency": row.frequency if row.frequency is not None else "—",
                    "state": STATE_LABELS[row.state],
                    "quality": " / ".join(
                        {
                            "valid": "有效",
                            "missing": "缺失",
                            "invalid": "无效",
                            "conflict": "冲突",
                        }.get(q, q)
                        for q in (
                            row.temperature_quality,
                            row.feedback_quality,
                            row.frequency_quality,
                        )
                    ),
                }
                for row in device.rows
                if row.day.isoformat() == self.detail_day
            ]
            with ui.element("div").classes("ahu-table-wrap"):
                ui.table(columns=columns, rows=rows, row_key="time", pagination=20).props(
                    "flat separator=horizontal"
                ).classes("data-table")

    def render_quality(self) -> None:
        if self.snapshot is None:
            return
        with ui.element("div").classes("ahu-quality-grid mt-6"):
            for device in self.snapshot.devices:
                q = device.quality
                with ui.column().classes("ahu-quality-card"):
                    ui.label(device.spec.name).classes("font-semibold")
                    for text in (
                        f"温度有效：{q.temperature_valid}/{q.expected}"
                        f"（{q.temperature_valid / q.expected:.1%}）"
                        if q.expected
                        else "无预期网格",
                        f"反馈频率共同有效：{q.status_valid}/{q.expected}",
                        f"温度有效但开机状态未确认：{q.unconfirmed}",
                        f"反馈频率矛盾：{q.state_conflicts}；字段时间戳冲突：{q.conflicts}",
                        f"去重：{q.duplicates}；越界：{q.outside}；非法时间：{q.invalid_times}；网格外：{q.off_grid}",
                        f"首个有效温度：{format_time(q.first_valid)}",
                        f"最后有效温度：{format_time(q.last_valid)}",
                        "无有效温度的归属日："
                        + ("、".join(d.isoformat() for d in q.no_data_days) or "无"),
                    ):
                        ui.label(text).classes("description")
        ui.label("连续缺测（仅限所选窗口内部，不跨每日未选时段连接）").classes("history-title")
        total_gaps = sum(len(device.quality.gaps) for device in self.snapshot.devices)
        self.gap_page = min(self.gap_page, max(1, math.ceil(total_gaps / 10)))
        # 1分钟网格可能产生大量孤立缺点；只向浏览器发送当前10行。
        selected_gaps = islice(
            ((d, gap) for d in self.snapshot.devices for gap in d.quality.gaps),
            (self.gap_page - 1) * 10,
            self.gap_page * 10,
        )
        gap_rows = [
            {
                "id": f"{d.spec.id}-{g.start.isoformat()}",
                "device": d.spec.id,
                "day": g.day.isoformat(),
                "start": format_time(g.start),
                "end": format_time(g.end),
                "minutes": int((g.end - g.start).total_seconds() / 60),
            }
            for d, g in selected_gaps
        ]
        columns = [
            {"name": key, "label": label, "field": key, "align": "left"}
            for key, label in (
                ("device", "设备"),
                ("day", "窗口归属日"),
                ("start", "首个缺失网格"),
                ("end", "下一有效网格／窗口结束"),
                ("minutes", "缺失网格分钟数"),
            )
        ]
        with ui.element("div").classes("ahu-table-wrap"):
            ui.table(columns=columns, rows=gap_rows, row_key="id").props(
                "flat hide-bottom separator=horizontal"
            ).classes("data-table")
        with ui.row().classes("ahu-view-tools"):
            ui.label(f"共{total_gaps}段，每页10段；质量CSV保留完整范围。").classes("description")
            if total_gaps:
                ui.pagination(
                    1,
                    max(1, math.ceil(total_gaps / 10)),
                    value=self.gap_page,
                    direction_links=True,
                    on_change=self.change_gap_page,
                ).props("max-pages=7 boundary-links")
        self.render_sources()

    def change_gap_page(self, event: events.ValueChangeEventArguments) -> None:
        self.gap_page = int(event.value or 1)
        self.render_results()

    def render_sources(self) -> None:
        with ui.column().classes("ahu-notes"):
            ui.label("测点与口径 · 芒市机场（上游项目832）").classes("history-title")
            for spec in DEVICES:
                ui.label(f"{spec.name} · 设备ID {spec.device_id}").classes("font-semibold")
                ui.label(
                    "；".join(f"{p.point_id}（{p.unit or '无量纲'}）" for p in spec.points)
                ).classes("ahu-source-id")
            for text in (
                "窗口均为左闭右开；日期两端包含，跨午夜按开始日期归属，最后一天延伸到次日。",
                "1分钟是查询网格，不等于传感器上报周期或原始瞬时采样。时间为Asia/Shanghai。",
                "确认开机：同刻反馈=1且0<频率≤60Hz；确认停机需反馈=0且频率=0。矛盾和未知单独标记。",
                "不限启停只要求温度有效；开机温度要求同刻确认开机。缺状态不当作停机，缺温度不补0。",
                "P05～P95按(n−1)×p线性插值，是中间约90%的有效采样范围；样本平均不是时长加权平均。",
                "温区按左闭右开计算有效采样占比，不表示精确时长或舒适达标率。真实0保留，极端值不自动剔除。",
                "质量分母只含所选窗口的预期网格；缺测不插值、前向填充或与最近状态匹配。",
                "首次和恢复条件不自动查；仅最近合法提交条件保存至当前浏览器，原始温度和凭据不进入偏好。",
                "本页仅查询，密码和Token只在服务端使用；不执行控制，也不由缺测直接诊断设备故障。",
            ):
                ui.label(text).classes("description")

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        if not self.mounted or self.snapshot is None:
            return
        device = self.selected_device()
        options = {
            "range": range_options(self.snapshot, dark, self.running),
            "distribution": distribution_options(self.snapshot, dark, self.running),
        }
        if device:
            options["daily"] = daily_options(device, dark, self.running)
            options["detail"] = detail_options(device, self.detail_day, dark)
        for key, chart in self.charts.items():
            if chart.is_deleted or key not in options:
                continue
            current = options[key]
            # 主题只更新样式，保留图例显隐与客户端缩放。
            current.pop("dataZoom", None)
            if "legend" in current:
                current["legend"].pop("selected", None)
            chart.options.clear()
            chart.options.update(current)
            chart.update()

    def download(self, kind: str) -> None:
        if not self.can_export or self.snapshot is None:
            ui.notify("请成功查询当前范围后再导出。", type="warning")
            return
        if kind == "notes":
            ui.download(
                export_notes(self.snapshot),
                "hangxin-ahu-statistics-notes.txt",
                media_type="text/plain",
            )
        elif kind in {"summary", "daily", "distribution", "quality"}:
            ui.download(
                export_snapshot(self.snapshot, kind),
                f"hangxin-ahu-{kind}.csv",
                media_type="text/csv",
            )
