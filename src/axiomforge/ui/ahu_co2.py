"""航信机场 AHU 二氧化碳与启停专题。"""

import math
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from nicegui import events, run, ui

from axiomforge.ahu_co2 import Event
from axiomforge.ahu_co2_report import EventRecord, Report, report_for
from axiomforge.ahu_co2_source import (
    DEVICE_BY_ID,
    MAX_RANGE_DAYS,
    REGIONS,
    AhuCo2SourceError,
    DaySnapshot,
    RangeSnapshot,
    load_ahu_co2_range,
    points_for,
)
from axiomforge.ahu_temperature import DEVICES, PROJECT_ID, SHANGHAI
from axiomforge.ui.ahu_co2_charts import METRICS, detail_options, distribution_options
from axiomforge.ui.ahu_temperature import value_text

DEFAULT_THRESHOLD = 700.0
EVENT_NAMES = {"start": "确认开机", "high_stopped": "高值停机"}
FLIGHT_ASSUMPTIONS = "国内／国际出港前 120／180 分钟、出港后 15 分钟，进港前 15／后 60 分钟。"
FLIGHT_STATES = ("参考窗内", "参考窗外", "缺少对应航班时刻", "航班数据未取得", "分拣保障时段待核实")


def _recent_week() -> tuple[str, str]:
    today = datetime.now(SHANGHAI).date()
    return (today - timedelta(days=7)).isoformat(), (today - timedelta(days=1)).isoformat()


def _clock(moment: datetime | None, day: date | None = None) -> str:
    if moment is None:
        return "—"
    local = moment.astimezone(SHANGHAI)
    return local.strftime("%m-%d %H:%M" if day and local.date() != day else "%H:%M")


def _reading(value: float | None, unit: str) -> str:
    return f"{value_text(value)} {unit}" if value is not None else "缺测"


def _flight_context(snapshot: DaySnapshot, moment: datetime) -> str:
    if snapshot.flight_scope is None:
        return "分拣保障时段待核实"
    if snapshot.flights is None:
        return "航班数据未取得"
    if not snapshot.flight_windows:
        return "缺少对应航班时刻"
    return (
        "参考窗内"
        if any(window.start <= moment < window.end for window in snapshot.flight_windows)
        else "参考窗外"
    )


def _event_span(event: Event, day: date) -> str:
    if event.start is None or event.end is None or event.start == event.end:
        return _clock(event.time, day)
    return f"{_clock(event.start, day)}–{_clock(event.end, day)}"


def _record_key(record: EventRecord) -> str:
    event = record.event
    return f"{record.snapshot.device.id}/{event.kind}/{event.time.isoformat()}"


class AhuCo2View:
    """查询草稿、成功快照及分析选择均属于当前浏览器。"""

    def __init__(self) -> None:
        self.start_day, self.end_day = _recent_week()
        self.threshold = DEFAULT_THRESHOLD
        self.metric = "temperature"
        self.loading = False
        self.dirty = False
        self.mounted = False
        self.dark = False
        self.snapshot: RangeSnapshot | None = None
        self.days: dict[tuple[str, str], DaySnapshot] = {}
        self.report: Report | None = None
        self.records: dict[str, EventRecord] = {}
        self.detail: DaySnapshot | None = None
        self.selected: Event | None = None
        self._syncing_detail = False
        self.device_filter = ""
        self.event_filter = ""
        self.flight_filter = ""
        self.error = ""
        self.threshold_error = False
        self.container: ui.column | None = None
        self.chart: ui.echart | None = None
        self.distribution: ui.echart | None = None
        self.progress = (0, 0)
        self._rendered_progress = (0, 0)
        self._generation = 0
        self._cancel_event = threading.Event()

    def build(self, dark: bool) -> None:
        self.dark = dark
        self.mounted = True
        ui.add_css(Path(__file__).with_suffix(".css").read_text())
        self.container = ui.column().classes("ahu-co2-page")
        with self.container:
            with ui.row().classes("breadcrumb"):
                ui.label("航信机场")
                ui.icon("chevron_right")
                ui.label("芒市机场 · 项目专题")
            with ui.row().classes("page-heading"):
                ui.label("AHU 二氧化碳与启停分析").props('role="heading" aria-level="1"').classes(
                    "page-title"
                )
                ui.label("真实数据 · 只读查询").classes("demo-badge")
            ui.label(
                "比较五台 AHU 的开机前 CO₂ 浓度，并结合回风温度和航班保障参考窗查看事件。"
            ).classes("description")
            with ui.row().classes("ahu-co2-toolbar"):
                self.start_input = (
                    ui.input("开始日期", value=self.start_day, on_change=self._edit_start)
                    .props('dense outlined type=date aria-label="开始日期"')
                    .classes("ahu-co2-date")
                )
                self.end_input = (
                    ui.input("结束日期", value=self.end_day, on_change=self._edit_end)
                    .props('dense outlined type=date aria-label="结束日期"')
                    .classes("ahu-co2-date")
                )
                self.week_button = (
                    ui.button("最近 7 个完整日", color=None, on_click=self._use_recent_week)
                    .props("flat no-caps")
                    .classes("secondary-btn")
                )
                ui.number(
                    "高值观察线（ppm）",
                    value=self.threshold,
                    on_change=self._edit_threshold,
                ).props('dense outlined debounce=250 aria-label="高值观察线（ppm）"').classes(
                    "ahu-co2-threshold"
                )
                self.query_button = (
                    ui.button("查询历史", icon="search", on_click=self.refresh)
                    .props("unelevated no-caps")
                    .classes("primary-btn")
                )
                self.stop_button = (
                    ui.button("停止等待", color=None, on_click=self.stop_waiting)
                    .props("flat no-caps")
                    .classes("secondary-btn")
                )
            self.status = ui.column().classes("ahu-co2-status").props('aria-live="polite"')
            self.results = ui.column().classes("ahu-co2-results")
            self._render_sources()
            ui.timer(0.5, self._poll_progress)
        self._render_status()
        self._render_results()

    def deactivate(self) -> None:
        self._cancel_event.set()
        self._generation += 1
        self.loading = False
        self.mounted = False

    def reconnect(self) -> None:
        if self.container is not None and not self.container.is_deleted:
            self.mounted = True
            self._render_status()

    def discard_draft(self) -> None:
        if self.snapshot is None:
            self.start_day, self.end_day = _recent_week()
        else:
            self.start_day = self.snapshot.start_day.isoformat()
            self.end_day = self.snapshot.end_day.isoformat()
        if self.mounted:
            self.start_input.set_value(self.start_day)
            self.end_input.set_value(self.end_day)
        self.dirty = False
        self.error = ""

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        self._update_chart(preserve_zoom=True)
        if self.mounted and self.distribution is not None and not self.distribution.is_deleted:
            self.distribution.options.clear()
            self.distribution.options.update(self._distribution_options())
            self.distribution.update()

    def _edit_start(self, event: events.ValueChangeEventArguments) -> None:
        self.start_day = event.value if isinstance(event.value, str) else ""
        self._edited()

    def _edit_end(self, event: events.ValueChangeEventArguments) -> None:
        self.end_day = event.value if isinstance(event.value, str) else ""
        self._edited()

    def _use_recent_week(self) -> None:
        self.start_day, self.end_day = _recent_week()
        self.start_input.set_value(self.start_day)
        self.end_input.set_value(self.end_day)
        self._edited()

    def _edited(self) -> None:
        self.dirty = self.snapshot is None or (
            self.start_day != self.snapshot.start_day.isoformat()
            or self.end_day != self.snapshot.end_day.isoformat()
        )
        self.error = ""
        self._render_status()

    def _edit_threshold(self, event: events.ValueChangeEventArguments) -> None:
        value = event.value
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            self.threshold_error = True
            self._render_status()
            return
        self.threshold_error = False
        self.threshold = float(value)
        self._render_status()
        if self.snapshot is not None:
            previous = (
                _record_key(EventRecord(self.detail, self.selected))
                if self.detail is not None and self.selected is not None
                else ""
            )
            self._set_report(report_for(self.snapshot, self.threshold))
            record = self.records.get(previous)
            self.selected = record.event if record else None
            self._update_chart(preserve_zoom=True)
            self._render_event()
            self._update_summary()
            self._update_table()

    def _render_status(self) -> None:
        if not self.mounted or self.status.is_deleted:
            return
        for control in (self.start_input, self.end_input, self.week_button, self.query_button):
            control.set_enabled(not self.loading)
        self.query_button.set_text("查询中…" if self.loading else "查询历史")
        self.stop_button.set_visibility(self.loading)
        self.status.clear()
        with self.status:
            ui.label(f"北京时间 · 含首尾日期 · 一次最多 {MAX_RANGE_DAYS} 天").classes("description")
            if self.snapshot is not None:
                days = (self.snapshot.end_day - self.snapshot.start_day).days + 1
                ui.label(
                    f"已查询 {self.snapshot.start_day} 至 {self.snapshot.end_day} · "
                    f"{days} 天 · 五台 AHU"
                ).classes("ahu-co2-title")
            if self.loading:
                self._rendered_progress = self.progress
                done, total = self._rendered_progress
                ui.linear_progress(value=done / total if total else 0)
                phase = "正在读取设备历史" if done < total else "正在整理航班保障参考数据"
                ui.label(f"设备历史已完成 {done}/{total} 天 · {phase}").classes("description")
            if self.error:
                ui.label(self.error).classes("ahu-co2-warning")
            if self.threshold_error:
                ui.label(f"请输入大于 0 的观察值，当前仍按 {self.threshold:g} ppm 分析。").classes(
                    "ahu-co2-warning"
                )
            if self.snapshot and self.dirty:
                ui.label("日期范围已修改，下方保留上次查询结果。").classes("ahu-co2-warning")

    def _poll_progress(self) -> None:
        if self.loading and self.mounted and self.progress != self._rendered_progress:
            self._render_status()

    async def refresh(self) -> None:
        if self.loading or not self.mounted:
            return
        try:
            start = date.fromisoformat(self.start_day)
            end = date.fromisoformat(self.end_day)
            if (
                start.isoformat() != self.start_day
                or end.isoformat() != self.end_day
                or start <= date.min
                or end >= date.max
                or start > end
                or end > datetime.now(SHANGHAI).date()
            ):
                raise ValueError
        except ValueError:
            self.error = "请选择今天或之前的有效日期，结束日期需等于或晚于开始日期。"
            self._render_status()
            return
        total = (end - start).days + 1
        if total > MAX_RANGE_DAYS:
            self.error = f"查询范围最多 {MAX_RANGE_DAYS} 天，请缩短日期范围。"
            self._render_status()
            return
        self.loading, self.error, self.progress = True, "", (0, total)
        self._generation += 1
        generation = self._generation
        self._cancel_event = threading.Event()
        cancelled = self._cancel_event
        self._render_status()

        def update_progress(done: int, total: int) -> None:
            # 后台线程只更新进度值，由页面计时器刷新界面。
            if generation == self._generation and not cancelled.is_set():
                self.progress = (done, total)

        try:
            snapshot = await run.io_bound(
                load_ahu_co2_range, start, end, cancelled=cancelled.is_set, progress=update_progress
            )
            if self.mounted and generation == self._generation and not cancelled.is_set():
                if not isinstance(snapshot, RangeSnapshot):
                    raise AhuCo2SourceError("查询未返回完整历史。")
                report = report_for(snapshot, self.threshold)
                self.snapshot = snapshot
                self.days = {(day.device.id, day.day.isoformat()): day for day in snapshot.days}
                self._set_report(report)
                first = next(
                    (record for record in report.records if record.event.kind == "start"),
                    next(iter(report.records), None),
                )
                self.detail = first.snapshot if first else next(iter(snapshot.days), None)
                self.selected = first.event if first else None
                self.dirty = False
                self._render_results()
        except AhuCo2SourceError as error:
            if self.mounted and generation == self._generation:
                self.error = str(error)
        except Exception:
            if self.mounted and generation == self._generation:
                self.error = "查询未完成，请稍后重试。"
        finally:
            if self.mounted and generation == self._generation:
                self.loading = False
                self._render_status()

    def stop_waiting(self) -> None:
        if self.loading:
            self._cancel_event.set()
            self._generation += 1
            self.loading = False
            self.error = "已停止等待。"
            self._render_status()

    def _set_report(self, report: Report) -> None:
        self.report = report
        self.records = {_record_key(record): record for record in report.records}

    def _render_results(self) -> None:
        self.results.clear()
        self.chart, self.distribution = None, None
        with self.results:
            if self.report is None:
                with ui.column().classes("empty-state ahu-co2-empty"):
                    ui.icon("timeline")
                    ui.label("查询五台 AHU 的历史").classes("empty-title")
                    ui.label("选择日期范围，查询开机前浓度分布和高值停机记录。").classes(
                        "empty-copy"
                    )
                return
            self._render_distribution()
            self._render_comparison()
            self._render_daily()

    def _distribution_options(self) -> dict[str, Any]:
        values = (
            {device.device_id: device.co2_values for device in self.report.devices}
            if self.report
            else {}
        )
        return distribution_options(values, self.dark)

    def _render_distribution(self) -> None:
        with ui.column().classes("ahu-co2-panel ahu-co2-section"):
            ui.label("各台 AHU 开机前 CO₂ 分布").classes("ahu-co2-title")
            ui.label(
                "每个有效配对对应一次确认开机。浓度取最后确认停机时刻或此前 5 分钟内最近的采样。"
            ).classes("description")
            self.distribution = (
                ui.echart(self._distribution_options())
                .props('aria-label="各台 AHU 开机前 CO₂ 浓度分布"')
                .classes("ahu-co2-distribution")
            )
            columns = [
                {"name": name, "field": name, "label": label, "align": "left"}
                for name, label in (
                    ("device", "设备"),
                    ("starts", "确认开机"),
                    ("count", "开机前 CO₂ 有效样本"),
                    ("median", "中位数"),
                    ("range", "最低～最高"),
                    ("high_stopped", "高值停机"),
                    ("high_peak", "停机峰值"),
                )
            ]
            self.summary_table = (
                ui.table(columns=columns, rows=[], row_key="device")
                .props("flat hide-bottom separator=horizontal")
                .classes("data-table ahu-co2-table")
            )
            self.summary_note = ui.label().classes("description")
            self._update_summary()

    def _update_summary(self) -> None:
        if self.report is None:
            return
        rows = []
        for device in self.report.devices:
            values = device.co2_values
            empty = "缺测" if device.starts else "—"
            rows.append(
                {
                    "device": device.device_id,
                    "starts": f"{device.starts} 次",
                    "count": f"{len(values)} 个",
                    "median": _reading(median(values), "ppm") if values else empty,
                    "range": f"{value_text(min(values))}～{value_text(max(values))} ppm"
                    if values
                    else empty,
                    "high_stopped": f"{device.high_stopped} 段",
                    "high_peak": _reading(device.high_peak, "ppm")
                    if device.high_peak is not None
                    else "—",
                }
            )
        self.summary_table.rows = rows
        self.summary_note.set_text(
            f"当前观察线 {self.threshold:g} ppm。0 段表示没有观察到符合条件的高值停机采样。"
        )

    def _render_comparison(self) -> None:
        with ui.column().classes("ahu-co2-panel ahu-co2-section ahu-co2-comparison"):
            ui.label("开机与高值停机记录").classes("ahu-co2-title")
            ui.label("点击时刻查看单日时序。回风温度与 CO₂ 取自同次采样。").classes("description")
            with ui.row().classes("ahu-co2-filters"):
                for label, options, field in (
                    (
                        "筛选设备",
                        {"": "全部设备", **{name: name for name in DEVICE_BY_ID}},
                        "device_filter",
                    ),
                    ("筛选事件", {"": "全部事件", **EVENT_NAMES}, "event_filter"),
                    (
                        "筛选保障状态",
                        {"": "全部保障状态", **{state: state for state in FLIGHT_STATES}},
                        "flight_filter",
                    ),
                ):
                    ui.select(
                        options,
                        value=getattr(self, field),
                        label=label,
                        on_change=lambda event, field=field: self._edit_filter(field, event),
                    ).props(f'dense outlined aria-label="{label}"').classes(
                        f"ahu-co2-filter ahu-co2-{field.removesuffix('_filter')}-filter"
                    )
                self.event_count = ui.label().classes("description")
            columns = [
                {"name": name, "field": name, "label": label, "align": "left"}
                for name, label in (
                    ("date", "日期"),
                    ("device", "设备"),
                    ("time", "开机／峰值时刻"),
                    ("kind", "观测状态"),
                    ("co2", "开机前／停机峰值 CO₂"),
                    ("temperature", "回风温度"),
                    ("flight", "开机／峰值时刻的保障参考"),
                )
            ]
            self.table = (
                ui.table(columns=columns, rows=[], row_key="key", pagination=10)
                .props(
                    'flat separator=horizontal no-data-label="当前没有符合条件的开机或高值停机记录"'
                )
                .classes("data-table ahu-co2-table")
            )
            self.table.add_slot(
                "body-cell-time",
                """
                <q-td :props="props">
                    <q-btn flat dense no-caps class="ahu-co2-event-button"
                        :class="{active: props.row.active}" :aria-pressed="props.row.active"
                        :aria-label="props.row.inspect"
                        :label="props.value" @click="$parent.$emit('inspect', props.row.key)" />
                </q-td>
                """,
            )
            self.table.add_slot(
                "body-cell-kind",
                '<q-td :props="props"><span class="ahu-co2-tag" :class="props.row.state">'
                "{{ props.value }}</span></q-td>",
            )
            self.table.on("inspect", self._select_event)
            self._update_table()

    def _edit_filter(self, field: str, event: events.ValueChangeEventArguments) -> None:
        if isinstance(event.value, str):
            setattr(self, field, event.value)
            self.table.pagination["page"] = 1
            self._update_table()

    def _update_table(self) -> None:
        rows = []
        for key, record in self.records.items():
            snapshot, event = record.snapshot, record.event
            if self.device_filter and self.device_filter != snapshot.device.id:
                continue
            if self.event_filter and self.event_filter != event.kind:
                continue
            flight = _flight_context(snapshot, event.time)
            if self.flight_filter and self.flight_filter != flight:
                continue
            clock = _clock(event.time)
            rows.append(
                {
                    "key": key,
                    "date": snapshot.day.isoformat(),
                    "device": snapshot.device.id,
                    "time": clock,
                    "kind": EVENT_NAMES[event.kind],
                    "state": event.kind,
                    "co2": _reading(event.sample.co2, "ppm"),
                    "temperature": _reading(event.sample.temperature, "℃"),
                    "flight": flight,
                    "active": snapshot is self.detail and event == self.selected,
                    "inspect": f"查看 {snapshot.day} {snapshot.device.id} {clock} 时序",
                }
            )
        self.table.rows = rows
        self.event_count.set_text(f"显示 {len(rows)} 条 · 共 {len(self.records)} 条")

    def _select_event(self, event: events.GenericEventArguments) -> None:
        record = self.records.get(event.args) if isinstance(event.args, str) else None
        if record is None:
            return
        self.detail, self.selected = record.snapshot, record.event
        self._syncing_detail = True
        self.day_select.set_value(record.snapshot.day.isoformat())
        self.device_select.set_value(record.snapshot.device.id)
        self._syncing_detail = False
        self._render_day_chart()
        self._update_table()
        ui.run_javascript(
            "document.querySelector('.ahu-co2-daily')?.scrollIntoView({block:'start'})"
        )

    def _render_daily(self) -> None:
        if self.detail is None:
            return
        with ui.row().classes("ahu-co2-panel-heading ahu-co2-daily"):
            ui.label("单日时序").classes("ahu-co2-title")
            with ui.row().classes("ahu-co2-filters"):
                self.day_select = (
                    ui.select(
                        sorted({day for _, day in self.days}),
                        value=self.detail.day.isoformat(),
                        label="时序日期",
                        on_change=self._edit_detail,
                    )
                    .props('dense outlined aria-label="时序日期"')
                    .classes("ahu-co2-date ahu-co2-detail-day")
                )
                self.device_select = (
                    ui.select(
                        list(DEVICE_BY_ID),
                        value=self.detail.device.id,
                        label="时序设备",
                        on_change=self._edit_detail,
                    )
                    .props('dense outlined aria-label="时序设备"')
                    .classes("ahu-co2-device ahu-co2-detail-device")
                )
                ui.button(
                    "返回事件记录",
                    color=None,
                    on_click=lambda: ui.run_javascript(
                        "document.querySelector('.ahu-co2-comparison')?.scrollIntoView({block:'start'})"
                    ),
                ).props("flat no-caps").classes("secondary-btn")
        self.day_content = ui.column().classes("ahu-co2-day-content")
        self._render_day_chart()

    def _edit_detail(self, _event: events.ValueChangeEventArguments) -> None:
        # 同时切换设备和日期时，跳过由控件赋值触发的中间组合。
        if self._syncing_detail:
            return
        device_id, day = self.device_select.value, self.day_select.value
        if self.detail is None or not isinstance(device_id, str) or not isinstance(day, str):
            return
        snapshot = self.days.get((device_id, day))
        if snapshot is None or snapshot is self.detail:
            return
        self.detail = snapshot
        self.selected = None
        self._render_day_chart()
        self._update_table()

    def _render_day_chart(self) -> None:
        snapshot = self.detail
        if snapshot is None:
            return
        self.day_content.clear()
        with self.day_content:
            co2_count = sum(row.co2 is not None for row in snapshot.samples)
            end_text = _clock(snapshot.end) if snapshot.end.date() == snapshot.day else "次日 00:00"
            with ui.row().classes("ahu-co2-scope"):
                ui.label(f"{snapshot.device.id} · {snapshot.day:%Y-%m-%d}").classes("ahu-co2-title")
                ui.label(
                    f"{_clock(snapshot.start)}–{end_text} 北京时间 · "
                    f"CO₂ 有效采样 {co2_count}/{len(snapshot.samples)}"
                ).classes("description")
                ui.label(f"参考服务区 {REGIONS[snapshot.device.id]}").classes("description")
            if not co2_count:
                ui.label("该查询时段无有效 CO₂ 采样。").classes("ahu-co2-warning")
            with ui.element("div").classes("ahu-co2-detail"):
                with ui.column().classes("ahu-co2-panel"):
                    with ui.row().classes("ahu-co2-panel-heading"):
                        ui.label("事件时序").classes("ahu-co2-title")
                        ui.select(
                            {field: label for field, (label, _) in METRICS.items()},
                            value=self.metric,
                            label="对照曲线",
                            on_change=self._edit_metric,
                        ).props('dense outlined aria-label="对照曲线"').classes("ahu-co2-metric")
                    ui.label("虚线标记所选事件，可拖动底部滑块查看全天。").classes("description")
                    self.chart = (
                        ui.echart(
                            detail_options(
                                snapshot, self.selected, self.metric, self.threshold, self.dark
                            )
                        )
                        .classes("ahu-co2-chart")
                        .props('aria-label="二氧化碳与启停事件时序"')
                    )
                    self._render_flight_note(snapshot)
                self.event_panel = ui.column().classes("ahu-co2-panel ahu-co2-snapshot")
            self._render_event()

    def _render_flight_note(self, snapshot: DaySnapshot) -> None:
        if snapshot.flight_error:
            ui.label(snapshot.flight_error).classes("ahu-co2-warning")
        if snapshot.flight_scope is None:
            ui.label("下轨为本场进出港航班时刻。分拣保障时段待核实。").classes("description")
        elif snapshot.flights is not None:
            ui.label(f"蓝色底纹 · 航班保障参考窗 · {snapshot.flight_scope}").classes(
                "ahu-co2-flight-note"
            )
            ui.label(f"既有研究口径。{FLIGHT_ASSUMPTIONS} 现场保障边界待核实。").classes(
                "description"
            )
        if snapshot.flights is not None:
            for warning in snapshot.flights.warnings:
                ui.label(warning).classes("ahu-co2-warning")

    def _edit_metric(self, event: events.ValueChangeEventArguments) -> None:
        if isinstance(event.value, str) and event.value in METRICS:
            self.metric = event.value
            self._update_chart(preserve_zoom=True)

    def _update_chart(self, *, preserve_zoom: bool = False) -> None:
        if not self.mounted or self.detail is None or self.chart is None or self.chart.is_deleted:
            return
        options = detail_options(self.detail, self.selected, self.metric, self.threshold, self.dark)
        if preserve_zoom:
            options.pop("dataZoom")
        self.chart.options.clear()
        self.chart.options.update(options)
        self.chart.update()

    def _render_event(self) -> None:
        snapshot = self.detail
        if snapshot is None or self.chart is None or self.chart.is_deleted:
            return
        self.event_panel.clear()
        with self.event_panel:
            event = self.selected
            with ui.row().classes("ahu-co2-panel-heading"):
                ui.label("事件快照").classes("ahu-co2-title")
                if event:
                    ui.label(EVENT_NAMES[event.kind]).classes(f"ahu-co2-tag {event.kind}")
            if event is None:
                has_events = any(record.snapshot is snapshot for record in self.records.values())
                ui.label(
                    "在记录表点选此日事件查看详情。"
                    if has_events
                    else "该设备当天暂无符合条件的事件。"
                ).classes("description")
                return
            day = snapshot.day
            start = event.kind == "start"
            with ui.column().classes("ahu-co2-reading"):
                ui.label("开机前 CO₂" if start else "停机段 CO₂ 峰值").classes("description")
                ui.label(_reading(event.sample.co2, "ppm")).classes("ahu-co2-value")
                if event.sample.co2 is not None:
                    ui.label(f"{_clock(event.sample.time, day)} 采样").classes("description")
            transition = (
                (
                    ("最后确认停机", _clock(event.previous_stopped, day)),
                    ("首次确认运行", _clock(event.time, day)),
                )
                if start
                else (
                    ("同刻停机证据", f"{event.count} 个高值采样"),
                    ("首末采样", _event_span(event, day)),
                )
            )
            with ui.element("dl").classes("ahu-co2-facts"):
                for name, value in (
                    *transition,
                    ("同次采样回风温度", _reading(event.sample.temperature, "℃")),
                    ("同次采样新风阀", _reading(event.sample.damper, "%")),
                    ("同次采样 CO₂ 设定值", _reading(event.sample.setpoint, "ppm")),
                    (
                        "开机时刻的保障参考" if start else "峰值时刻的保障参考",
                        _flight_context(snapshot, event.time),
                    ),
                ):
                    with ui.element("div"):
                        with ui.element("dt"):
                            ui.label(name)
                        with ui.element("dd"):
                            ui.label(value)
            ui.label("CO₂ 设定值来自设备参数。实际开机条件待核实。").classes("description")

    def _render_sources(self) -> None:
        with ui.expansion("设备与测点来源", icon="info_outline").classes("ahu-co2-sources"):
            ui.label(
                f"小麦项目 {PROJECT_ID} · CO₂、回风、风机反馈、频率、新风阀和 CO₂ 设定值。"
                " 服务区来自现场着色图，传感器安装位置待核实。"
            ).classes("description")
            ui.label(
                "反馈和频率同刻确认风机状态。高值停机按同刻证据分段，图中空白保留缺测。"
            ).classes("description")
            for device in DEVICES:
                co2_point = next(point for point in points_for(device) if point.field == "co2")
                with ui.row().classes("ahu-co2-source-row"):
                    ui.label(f"{device.id} · {REGIONS[device.id]}")
                    ui.label(co2_point.point_id).classes("ahu-co2-point-id")
