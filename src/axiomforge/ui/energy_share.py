"""每日用电占比；独立客户端草稿、快照及请求生命周期。"""

import json
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from nicegui import events, run, ui

from axiomforge.energy_share import (
    GROUPS,
    MAPPING_VERSION,
    PERIODS,
    PREFERENCE_KEY,
    SHANGHAI,
    SOURCE_VERSION,
    Aggregate,
    Boundary,
    Conditions,
    GroupResult,
    Query,
    Snapshot,
    decode_conditions,
    default_conditions,
    encode_conditions,
    export_daily,
    export_intervals,
    export_notes,
    export_quality,
    export_summary,
    format_number,
    preset_dates,
    resolve_query,
)
from axiomforge.energy_share_source import (
    EnergySourceError,
    Progress,
    QueryCancelled,
    load_energy_share,
)
from axiomforge.ui.energy_share_charts import COLORS, daily_options

TABS = ("每日占比", "区间汇总", "读数与质量")
BOUNDARY_LABELS = {
    "exact": "精确采样",
    "aligned": "短时对齐",
    "missing": "缺失",
    "conflict": "冲突",
    "invalid": "非法读数",
}


def format_time(value: datetime | None) -> str:
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") if value else "—"


def query_summary(query: Query) -> str:
    return (
        f"{query.days[0]}～{query.days[-1]} · {len(query.days)}个自然日；"
        f"实际终点 {format_time(query.end)}（不含）"
    )


def group_status(item: GroupResult) -> str:
    """八行表只显示简明状态，逐表原始原因保留在展开依据中。"""
    if item.kwh is not None:
        return "短时日界对齐" if item.estimated else "精确日界"
    statuses = {boundary.status for meter in item.meters for boundary in (meter.start, meter.end)}
    issue_kinds = {issue.kind for meter in item.meters for issue in meter.issues}
    if "rollback" in issue_kinds:
        return "观察回退，增量不可确定"
    if "conflict" in statuses or "conflict" in issue_kinds:
        return "读数冲突，增量不可确定"
    if "invalid" in statuses or "invalid" in issue_kinds:
        return "读数或支撑异常"
    if "missing" in statuses:
        return "日界缺失，增量不可确定"
    return "增量不可确定"


class EnergyShareView:
    """每个请求有独立停止标志和代号，迟到进度与结果不能写入新页面。"""

    def __init__(self) -> None:
        self.conditions = default_conditions()
        self.submitted_conditions: Conditions | None = None
        self.snapshot: Snapshot | None = None
        self.active_tab = TABS[0]
        self.detail_day = ""
        self.mode = "share"
        self.highlight = ""
        self.viewport = 14
        self.zoom: dict[str, float] = {}
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
        self._query_interrupted = False
        self._status_signature: tuple[object, ...] | None = None

    @property
    def stale(self) -> bool:
        return self.snapshot is not None and self.submitted_conditions != self.conditions

    @property
    def can_export(self) -> bool:
        return (
            self.snapshot is not None
            and not self.loading
            and not self.stale
            and not self.error
            and not self._query_interrupted
        )

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
        self.conditions = decode_conditions(settings)
        self.dirty = False
        if self.mounted:
            self.sync_form()
            self.render_status()

    def save_preferences(self, conditions: Conditions) -> None:
        """仅保存合法提交；浏览器禁用存储不会阻断当前会话查询。"""
        payload = json.dumps(json.dumps(encode_conditions(conditions), ensure_ascii=False))
        ui.run_javascript(
            f"try {{ localStorage.setItem({json.dumps(PREFERENCE_KEY)},"
            f"{payload}); }} catch (_) {{}}"
        )

    def deactivate(self) -> None:
        if self.loading:
            self._query_interrupted = True
            self.error = "查询已中断，原快照保留；请重新查询后导出。"
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
        self.container = ui.column().classes("energy-page gap-0")
        with self.container:
            with ui.row().classes("breadcrumb"):
                ui.label("航信机场")
                ui.icon("chevron_right")
                ui.label("芒市机场 · 项目专题")
            with ui.row().classes("page-heading"):
                ui.label("每日用电占比").props('role="heading" aria-level="1"').classes(
                    "page-title"
                )
                ui.label("真实数据 · 只读查询").classes("demo-badge")
            ui.label("同八项电表 · 北京时间自然日 · 小麦只读").classes("description")
            self.build_form()
            self.status = ui.column().classes("energy-status")
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
            self.results = ui.column().classes("energy-panel")
            ui.label("八项合计不代表机场总用电；缺测不补0、不分摊，所有查询均为只读。").classes(
                "insight"
            )
            ui.timer(0.5, self.poll_progress)
        self.sync_form()
        self.render_status()
        self.render_results()

    def build_form(self) -> None:
        with ui.row().classes("energy-toolbar w-full"):
            self.preset = (
                ui.select(
                    PERIODS,
                    value=self.conditions.period,
                    label="日期预设",
                    on_change=self.change_period,
                )
                .props('outlined dense aria-label="用电日期预设"')
                .classes("energy-preset")
            )
            self.start_input = (
                ui.input(
                    "开始日期",
                    value=self.conditions.start_date,
                    on_change=lambda e: self.change_date("start", e.value),
                )
                .props('type=date outlined dense aria-label="用电开始日期"')
                .classes("energy-date")
            )
            self.end_input = (
                ui.input(
                    "结束日期（含当天）",
                    value=self.conditions.end_date,
                    on_change=lambda e: self.change_date("end", e.value),
                )
                .props('type=date outlined dense aria-label="用电结束日期"')
                .classes("energy-date")
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
                        ("daily", "每日八项 CSV"),
                        ("summary", "每日汇总 CSV"),
                        ("intervals", "区间两口径 CSV"),
                        ("quality", "物理表边界与质量 CSV"),
                        ("notes", "统计口径说明"),
                    ):
                        ui.menu_item(label, on_click=lambda kind=kind: self.download(kind))

    def sync_form(self) -> None:
        self._setting_form = True
        try:
            self.preset.set_value(self.conditions.period)
            self.start_input.set_value(self.conditions.start_date)
            self.end_input.set_value(self.conditions.end_date)
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
                else (self.conditions.start_date, self.conditions.end_date)
            )
        except ValueError as error:
            self.error = str(error)
            self.sync_form()
            self.render_status()
            return
        self.conditions = replace(self.conditions, period=period, start_date=start, end_date=end)
        self.sync_form()
        self.edited()

    def change_date(self, key: str, value: Any) -> None:
        if self._setting_form:
            return
        if key == "start":
            self.conditions = replace(self.conditions, period="custom", start_date=str(value or ""))
        elif key == "end":
            self.conditions = replace(self.conditions, period="custom", end_date=str(value or ""))
        else:
            return
        self.sync_form()
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
                ui.label("待查询条件：" + query_summary(resolve_query(self.conditions))).classes(
                    "description"
                )
            except ValueError as error:
                ui.label(str(error)).classes("energy-warning")
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
                ui.label(self.error).classes("energy-warning")
            if self.stale:
                ui.label("条件已修改，下方仍为上次成功快照；请查询更新后导出。").classes(
                    "energy-warning"
                )
            if self.snapshot:
                ui.label("成功快照：" + query_summary(self.snapshot.query)).classes("description")
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
        self._query_interrupted = True
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
                load_energy_share, query, progress=update_progress, cancelled=cancel_event.is_set
            )
            if not isinstance(snapshot, Snapshot):
                raise EnergySourceError("查询未返回完整快照，请重试；原结果已保留。")
            if self.mounted and generation == self.generation and not cancel_event.is_set():
                self.snapshot = snapshot
                self.zoom = {}
                self._query_interrupted = False
                self.submitted_conditions = query.conditions
                self.dirty = False
                self.detail_day = next(
                    (
                        d.day.isoformat()
                        for d in reversed(snapshot.daily)
                        if d.total is not None and d.day
                    ),
                    snapshot.query.days[-1].isoformat(),
                )
                self.render_results()
        except QueryCancelled:
            if self.mounted and generation == self.generation:
                self.error = "已停止等待，原快照保留；已发送请求会在完成或超时后退出。"
        except EnergySourceError as error:
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
                        ui.icon("bar_chart")
                        ui.label("查看八项每日用电构成").classes("empty-title")
                        ui.label(
                            "选择完整日期范围，点击“查询统计”。记住上次条件，数据仅在手动查询后读取。"
                        ).classes("empty-copy")
                return
            if self.active_tab == TABS[0]:
                self.render_daily()
            elif self.active_tab == TABS[1]:
                self.render_intervals()
            else:
                self.render_quality()

    def selected_day(self) -> Aggregate | None:
        if self.snapshot is None:
            return None
        return next(
            (
                day
                for day in self.snapshot.daily
                if day.day and day.day.isoformat() == self.detail_day
            ),
            None,
        )

    def day_selector(self) -> None:
        if self.snapshot is None:
            return
        ui.select(
            {day.day.isoformat(): day.day.isoformat() for day in self.snapshot.daily if day.day},
            value=self.detail_day,
            label="查看日期",
            on_change=self.change_day,
        ).props('outlined dense aria-label="用电查看日期"').classes("energy-day-select")

    def change_day(self, event: events.ValueChangeEventArguments) -> None:
        self.detail_day = str(event.value)
        self.render_results()

    def change_mode(self, event: events.ValueChangeEventArguments) -> None:
        if event.value in {"share", "kwh"}:
            self.mode = str(event.value)
            self.render_results()

    def change_viewport(self, event: events.ValueChangeEventArguments) -> None:
        if event.value in {7, 14, 31}:
            self.viewport = int(event.value)
            self.zoom = {}
            self.render_results()

    def zoomed(self, event: events.GenericEventArguments) -> None:
        """保留实际日期视口，选日／高亮重建内容时不跳回范围末端。"""
        args = event.args
        value = args.get("batch", [args])[0] if args.get("batch") else args
        if isinstance(value, dict):
            self.zoom = {
                key: float(value[key])
                for key in ("start", "end")
                if isinstance(value.get(key), (int, float))
                and not isinstance(value[key], bool)
                and 0 <= value[key] <= 100
            }

    def daily_chart_options(self) -> dict[str, Any]:
        if self.snapshot is None:
            return {}
        options = daily_options(self.snapshot, self.dark, self.mode, self.highlight, self.viewport)
        if self.zoom:
            for control in options["dataZoom"]:
                control.pop("startValue", None)
                control.pop("endValue", None)
                control.update(self.zoom)
        return options

    def focus_group(self, key: str) -> None:
        self.highlight = "" if self.highlight == key else key
        self.render_results()

    def chart_day_clicked(self, event: events.EChartPointClickEventArguments) -> None:
        if self.snapshot and event.name in {
            day.day.isoformat() for day in self.snapshot.daily if day.day
        }:
            self.detail_day = event.name
            self.render_results()

    def render_daily(self) -> None:
        if self.snapshot is None:
            return
        with ui.row().classes("energy-tools"):
            ui.toggle(
                {"share": "占比 %", "kwh": "用电 kWh"}, value=self.mode, on_change=self.change_mode
            ).props("unelevated no-caps")
            if len(self.snapshot.daily) > 7:
                ui.select(
                    {7: "7日一屏", 14: "14日一屏", 31: "31日一屏"},
                    value=self.viewport,
                    on_change=self.change_viewport,
                ).props("outlined dense").classes("energy-viewport")
            self.day_selector()
        with ui.element("div").classes("energy-main-grid"):
            with ui.column().classes("chart-panel energy-chart-panel"):
                ui.label(
                    "每日构成 · 占八项合计" if self.mode == "share" else "每日用电 · kWh"
                ).classes("chart-title")
                ui.label("缺测日占比留空；kWh仅叠加已知项，属于部分小计。").classes("description")
                self.charts["daily"] = ui.echart(
                    self.daily_chart_options(),
                    on_point_click=self.chart_day_clicked,
                ).classes("energy-plot")
                self.charts["daily"].on("chart:datazoom", self.zoomed, ["start", "end", "batch"])
                with ui.element("div").classes("energy-legend"):
                    for index, group in enumerate(GROUPS):
                        ui.button(
                            group.name,
                            icon="circle",
                            color=None,
                            on_click=lambda key=group.key: self.focus_group(key),
                        ).props("flat dense no-caps").classes(
                            "energy-legend-item"
                            + (" energy-focused" if self.highlight == group.key else "")
                        ).style(f"color:{COLORS[index]}")
                ui.label(
                    "图例仅高亮；八项分母始终固定。拖动底部滑块查看日期，导出仍为完整范围。"
                ).classes("description")
            with ui.column().classes("energy-day-panel"):
                day = self.selected_day()
                if day:
                    ui.label(f"{self.detail_day} · 当日明细").classes("chart-title")
                    self.aggregate_header(day)
                    self.aggregate_table(day)
        ui.label(
            "显示保留两位小数，舍入可能使占比合计为99.99%或100.01%；不对最后一项补差。"
        ).classes("description mt-3")
        day = self.selected_day()
        if day:
            with ui.expansion("展开八项组成与物理表依据", icon="account_tree").classes(
                "energy-expansion"
            ):
                self.render_meter_groups(day)

    def aggregate_header(self, aggregate: Aggregate) -> None:
        if aggregate.total is None:
            ui.label("八项合计不可确定 · 全部占比留空").classes("energy-warning")
            ui.label(
                f"已知小计 {format_number(aggregate.known_subtotal)} kWh，非八项总量"
                f"（{aggregate.known_count}/8项已知）"
            ).classes("description")
        else:
            ui.label(f"{format_number(aggregate.total)} kWh").classes("energy-total")
            ui.label(
                "八项均可计算"
                + ("；总量为0，占比无定义。" if aggregate.total == 0 else " · 未观察回退")
            ).classes("description")

    def aggregate_table(self, aggregate: Aggregate) -> None:
        rows = [
            {
                "key": item.group.key,
                "name": item.group.name,
                "color": COLORS[
                    next(i for i, group in enumerate(GROUPS) if group.key == item.group.key)
                ],
                "kwh": format_number(item.kwh),
                "share": format_number(item.share) + ("%" if item.share is not None else ""),
                "state": group_status(item),
            }
            for item in aggregate.items
        ]
        columns = [
            {
                "name": field,
                "label": title,
                "field": field,
                "align": "left" if field in {"name", "state"} else "right",
            }
            for field, title in [
                ("name", "电表"),
                ("kwh", "用电 kWh"),
                ("share", "占八项合计"),
                ("state", "数据状态"),
            ]
        ]
        with ui.element("div").classes("energy-table-wrap"):
            table = (
                ui.table(columns=columns, rows=rows, row_key="key")
                .props("flat hide-bottom separator=horizontal wrap-cells")
                .classes("data-table energy-eight-table")
            )
            table.add_slot(
                "body-cell-name",
                '<q-td :props="props"><span :style="{color: props.row.color}">●</span> '
                "{{props.value}}</q-td>",
            )

    def render_intervals(self) -> None:
        if self.snapshot is None:
            return
        snap = self.snapshot
        with ui.element("div").classes("energy-interval-grid"):
            with ui.column().classes("energy-card"):
                ui.label("A · 区间累计差").classes("chart-title")
                ui.label("逐块物理表按整段首末边界差计算，不要求每个内部午夜都有读数。").classes(
                    "description"
                )
                self.aggregate_header(snap.interval)
                self.aggregate_table(snap.interval)
            with ui.column().classes("energy-card"):
                ui.label("B · 可计算日合计").classes("chart-title")
                ui.label(
                    f"共同可计算日 {len(snap.included_days)}/{len(snap.daily)} 天；"
                    "先加总电量，再计算占比。"
                ).classes("description")
                if snap.complete_days:
                    self.aggregate_header(snap.complete_days)
                    self.aggregate_table(snap.complete_days)
                else:
                    ui.label("没有八项均可计算的日期，合计与占比无定义。").classes("energy-warning")
                ui.label(
                    "排除日：" + ("、".join(str(day) for day in snap.excluded_days) or "无")
                ).classes("description")
        if snap.interval.total is not None and snap.excluded_days:
            ui.label(
                "区间可计算，部分逐日不可拆分；两种口径分别反映首末读数与完整日覆盖，不能互相填补。"
            ).classes("energy-warning")
        ui.label(
            "未观察到回退不等于证明缺口内没有清零或换表。首版不自动推算长缺口上下界，不把已有月度范围摊到每日。"
        ).classes("description mt-4")
        with ui.expansion("区间首末物理表依据", icon="fact_check").classes("energy-expansion"):
            self.render_meter_groups(snap.interval)

    def boundary_text(self, boundary: Boundary) -> str:
        label = BOUNDARY_LABELS.get(boundary.status, boundary.status)
        text = f"{format_time(boundary.target)} · {label} · {format_number(boundary.value)} kWh"
        if boundary.before or boundary.after:
            if boundary.before and boundary.after:
                text += (
                    f"；支撑 {format_time(boundary.before.timestamp)} / "
                    f"{format_number(boundary.before.value)} → "
                    f"{format_time(boundary.after.timestamp)} / "
                    f"{format_number(boundary.after.value)}"
                )
                seconds = int(
                    (boundary.after.timestamp - boundary.before.timestamp).total_seconds()
                )
                text += f"，间隔{seconds}秒"
            else:
                support = boundary.before or boundary.after
                assert support is not None
                side = "前" if boundary.before else "后"
                text += (
                    f"；仅{side}侧支撑 {format_time(support.timestamp)} / "
                    f"{format_number(support.value)} kWh"
                )
        if boundary.reason:
            text += "；" + boundary.reason
        return text

    def render_meter_groups(self, aggregate: Aggregate) -> None:
        for item in aggregate.items:
            with ui.expansion(
                f"{item.group.name} · {format_number(item.kwh)} kWh", icon="electric_meter"
            ).classes("energy-expansion"):
                ui.label(
                    f"平台表 {item.group.platform_id} · {len(item.meters)}块物理表；"
                    "仅八项加入分母，子表不重复累加。"
                ).classes("description")
                for result in item.meters:
                    ui.label(
                        f"{result.meter.name} · {format_number(result.kwh)} kWh · "
                        f"{result.reason or '未观察回退'}"
                    ).classes("font-semibold")
                    ui.label(f"设备 {result.meter.device_id} · {result.meter.point_id}").classes(
                        "energy-source-id"
                    )
                    ui.label("期初：" + self.boundary_text(result.start)).classes("description")
                    ui.label("期末：" + self.boundary_text(result.end)).classes("description")
                    ui.label(
                        f"返回有效采样 {result.observed_count}，空网格 {result.missing_count}；"
                        "采样缺口不等于电量缺失比例。"
                    ).classes("description")
                    for issue in result.issues:
                        ui.label(
                            f"{format_time(issue.start)}～{format_time(issue.end)}：{issue.reason}"
                        ).classes("energy-warning")

    def render_quality(self) -> None:
        if self.snapshot is None:
            return
        snap = self.snapshot
        aligned = sum(boundary.status == "aligned" for boundary in snap.boundaries)
        zero = sum(day.total == 0 for day in snap.daily if day.total is not None)
        with ui.row().classes("energy-quality-summary"):
            for label, value in [
                ("八项可计算日", f"{len(snap.included_days)}/{len(snap.daily)}"),
                ("部分／缺测日", str(len(snap.excluded_days))),
                ("总量为0日期", str(zero)),
                ("短时对齐边界", str(aligned)),
                ("回退／异常依据", str(len(snap.issues))),
            ]:
                with ui.column().classes("energy-quality-metric"):
                    ui.label(value).classes("energy-metric-value")
                    ui.label(label).classes("description")
        with ui.row().classes("energy-tools"):
            self.day_selector()
            ui.label("相邻日共享同一个午夜边界；质量CSV导出全部日期。").classes("description")
        day = self.selected_day()
        if day:
            self.render_meter_groups(day)
        self.render_sources()

    def render_sources(self) -> None:
        with ui.column().classes("energy-notes"):
            ui.label("统计边界与来源").classes("history-title")
            ui.label(
                f"来源 {SOURCE_VERSION} · 映射 {MAPPING_VERSION} · 项目832 · Asia/Shanghai"
            ).classes("description")
            for group in GROUPS:
                ui.label(f"{group.name}（平台表{group.platform_id}）").classes("font-semibold")
                ui.label(" + ".join(group.point_ids)).classes("energy-source-id")
            for text in (
                "八项由13块不重复物理表组成；平台公式核对不等于现场接线验证。",
                "自然日00:00至次日00:00，结束不含；只接受已结束完整日，最长62天。",
                "累计总有功电量期末减期初，单位kWh；累计表底或瞬时功率不能作为占比。",
                "精确午夜优先；缺少时仅允许两侧包围、总间隔≤300秒的线性对齐，并保留支撑值。",
                "单侧或长缺口不补值；同刻冲突、非法读数、观测回退使涉及增量不可计算，不截为0。",
                "任一项不可算时八项总量及全部占比留空，保留已知项kWh；总量为0时占比无定义。",
                "区间累计差与共同可计算日合计独立展示；不平均每日百分比，不跨日分摊。",
                "1分钟是查询网格，不等于实际上报周期；内部空网格不直接表示电量缺失比例。",
                "首次及恢复不取数，仅保存最近合法查询条件；账号、Token和原始读数不进入浏览器偏好。",
            ):
                ui.label(text).classes("description")

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        if not self.mounted or self.snapshot is None:
            return
        chart = self.charts.get("daily")
        if chart and not chart.is_deleted:
            options = daily_options(self.snapshot, dark, self.mode, self.highlight, self.viewport)
            options.pop("dataZoom", None)
            chart.options.clear()
            chart.options.update(options)
            chart.update()

    def download(self, kind: str) -> None:
        if not self.can_export or self.snapshot is None:
            ui.notify("请成功查询当前范围后再导出。", type="warning")
            return
        functions = {
            "daily": export_daily,
            "summary": export_summary,
            "intervals": export_intervals,
            "quality": export_quality,
            "notes": export_notes,
        }
        if kind in functions:
            ui.download(
                functions[kind](self.snapshot),
                f"hangxin-energy-{kind}.{'txt' if kind == 'notes' else 'csv'}",
                media_type="text/plain" if kind == "notes" else "text/csv",
            )
