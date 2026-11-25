"""航信机场区域温湿度专题，分析状态与成功快照属于当前浏览器。"""

import math
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from nicegui import events, run, ui

from axiomforge.ahu_temperature import SHANGHAI
from axiomforge.region_climate import (
    DEFAULT_LOWER,
    DEFAULT_UPPER,
    TIME_CELL_MINUTES,
    Excursion,
    RegionAnalysis,
    StartComparison,
    TemperatureDistribution,
    TemperatureRange,
    analyze_region,
    probe_distributions,
    probe_temperature_cells,
    region_distribution,
    region_temperature_cells,
)
from axiomforge.region_climate_source import REGIONS, ClimateSnapshot, Region, load_region_climate
from axiomforge.ui.ahu_temperature_charts import temperature_distribution_options
from axiomforge.ui.region_climate_charts import temperature_time_options, trend_options
from axiomforge.xiaomai_client import XiaomaiSourceError

REGION_BY_ID = {region.id: region for region in REGIONS}
EXCURSION_LABELS = {"cold": "过冷", "hot": "过热"}
DISTRIBUTION_VIEWS = {"time": "按时间", "share": "全天占比"}


def _today() -> date:
    return datetime.now(SHANGHAI).date()


def _clock(moment: datetime, day: date) -> str:
    local = moment.astimezone(SHANGHAI)
    return "24:00" if local.date() > day else local.strftime("%H:%M")


def _range_text(low: float | None, high: float | None, unit: str) -> str:
    return "无有效数据" if low is None or high is None else f"{low:.1f}～{high:.1f} {unit}"


def _range_of(values: list[float], unit: str) -> str:
    return _range_text(min(values), max(values), unit) if values else "无有效数据"


def _distribution_rows(
    distributions: list[TemperatureDistribution] | tuple[TemperatureDistribution, ...],
) -> list[tuple[str, tuple[int, ...]]]:
    rows = []
    for distribution in distributions:
        suffix = (
            "（待确认）" if distribution.pending else "（无样本）" if not distribution.count else ""
        )
        rows.append((distribution.label + suffix, distribution.bins))
    return rows


class RegionClimateView:
    """仅日期查询取数，区域、探头、开机事件和观察阈值均使用当前快照。"""

    def __init__(self) -> None:
        self.day = _today().isoformat()
        self.lower, self.upper = DEFAULT_LOWER, DEFAULT_UPPER
        self.lower_input_value: float | None = self.lower
        self.upper_input_value: float | None = self.upper
        self.snapshot: ClimateSnapshot | None = None
        self.analyses: dict[str, RegionAnalysis] = {}
        self.detail_analysis: RegionAnalysis | None = None
        self.region_id = REGIONS[0].id
        self.probe_id: int | None = None
        self.event_id = ""
        self.loading = False
        self.dirty = False
        self.mounted = False
        self.dark = False
        self.error = ""
        self.threshold_error = False
        self.container: ui.column | None = None
        self.chart: ui.echart | None = None
        self.region_distribution_chart: ui.echart | None = None
        self.probe_distribution_chart: ui.echart | None = None
        self.distribution_view = "time"
        self.distribution_minutes = 30
        self._generation = 0
        self._cancel_event = threading.Event()

    def build(self, dark: bool) -> None:
        self.dark, self.mounted = dark, True
        self.chart = None
        self.region_distribution_chart = self.probe_distribution_chart = None
        ui.add_css(Path(__file__).with_suffix(".css").read_text())
        self.container = ui.column().classes("region-climate")
        with self.container:
            with ui.row().classes("breadcrumb"):
                ui.label("航信机场")
                ui.icon("chevron_right")
                ui.label("芒市机场 · 项目专题")
            with ui.row().classes("page-heading"):
                ui.label("区域温湿度").props('role="heading" aria-level="1"').classes("page-title")
                ui.label("小麦 · 只读查询").classes("demo-badge")
            with ui.row().classes("region-climate-toolbar"):
                self.date_input = (
                    ui.input("查询日期", value=self.day, on_change=self._edit_day)
                    .props(
                        f'dense outlined type=date max={_today().isoformat()} aria-label="查询日期"'
                    )
                    .classes("region-climate-date")
                )
                self.query_button = (
                    ui.button("查询全天", icon="search", on_click=self.refresh)
                    .props("unelevated no-caps")
                    .classes("primary-btn")
                )
                self.stop_button = (
                    ui.button("停止等待", color=None, on_click=self.stop_waiting)
                    .props("flat no-caps")
                    .classes("secondary-btn")
                )
                ui.space()
                self.lower_input = self._threshold_field("过冷线（℃）", self.lower_input_value)
                self.upper_input = self._threshold_field("过热线（℃）", self.upper_input_value)
            self.status = ui.column().classes("region-climate-status").props('aria-live="polite"')
            self.results = ui.column().classes("region-climate-results")
        self._render_status()
        self._render_results()

    def deactivate(self) -> None:
        self._cancel_event.set()
        self._generation += 1
        if self.loading:
            self.error = "查询已停止等待。"
        self.loading = self.mounted = False

    def reconnect(self) -> None:
        if self.container is not None and not self.container.is_deleted:
            self.mounted = True
            self._render_status()

    def discard_draft(self) -> None:
        self.day = (self.snapshot.day if self.snapshot else _today()).isoformat()
        if self.mounted:
            self.date_input.set_value(self.day)
        self.dirty = False
        self.error = ""

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        if (
            self.mounted
            and self.chart is not None
            and not self.chart.is_deleted
            and self.detail_analysis is not None
        ):
            options = self._chart_options(self.detail_analysis)
            options.pop("dataZoom", None)
            self.chart.options.clear()
            self.chart.options.update(options)
            self.chart.update()
        if self.mounted and self.snapshot is not None:
            for chart, region in (
                (self.region_distribution_chart, None),
                (self.probe_distribution_chart, REGION_BY_ID[self.region_id]),
            ):
                if chart is not None and not chart.is_deleted:
                    options = self._distribution_options(region)
                    options.pop("dataZoom", None)
                    chart.options.update(options)
                    chart.update()

    def _edit_day(self, event: events.ValueChangeEventArguments) -> None:
        self.day = event.value if isinstance(event.value, str) else ""
        self.dirty = self.snapshot is None or self.day != self.snapshot.day.isoformat()
        self.error = ""
        self._render_status()

    def _threshold_field(self, title: str, value: float | None) -> ui.number:
        return (
            ui.number(title, value=value, step=0.5, on_change=self._edit_thresholds)
            .props(f'dense outlined debounce=250 aria-label="{title}"')
            .classes("region-climate-threshold")
        )

    def _edit_thresholds(self) -> None:
        self.lower_input_value, self.upper_input_value = (
            self.lower_input.value,
            self.upper_input.value,
        )
        low, high = self.lower_input_value, self.upper_input_value
        if (
            low is None
            or high is None
            or not math.isfinite(low)
            or not math.isfinite(high)
            or low >= high
        ):
            self.threshold_error = True
            self._render_status()
            return
        self.threshold_error = False
        changed = (low, high) != (self.lower, self.upper)
        self.lower, self.upper = low, high
        if changed and self.snapshot is not None:
            self._calculate_analyses(self.snapshot)
            self._render_results()
        self._render_status()

    def _render_status(self) -> None:
        if not self.mounted:
            return
        self.query_button.set_enabled(not self.loading)
        self.query_button.set_text("查询中…" if self.loading else "查询全天")
        self.date_input.set_enabled(not self.loading)
        self.stop_button.set_visibility(self.loading)
        self.status.clear()
        with self.status:
            if self.snapshot is not None:
                snapshot = self.snapshot
                ui.label(
                    f"{snapshot.day.isoformat()} · {_clock(snapshot.start, snapshot.day)}～"
                    f"{_clock(snapshot.end, snapshot.day)} · 北京时间 · 5 分钟聚合"
                ).classes("description")
            if self.dirty and self.snapshot is not None:
                ui.label("日期已修改，图表仍显示上次查询。点击查询更新。").classes("stale")
            if self.loading:
                ui.linear_progress().props("indeterminate rounded")
            if self.error:
                ui.label(self.error).classes("region-climate-warning")
            if self.threshold_error:
                ui.label(f"过冷线须低于过热线。当前使用 {self.lower:g}～{self.upper:g}℃。").classes(
                    "region-climate-warning"
                )

    async def refresh(self) -> None:
        if self.loading or not self.mounted:
            return
        try:
            day = date.fromisoformat(self.day)
            if day > _today():
                raise ValueError
        except ValueError:
            self.error = "请选择有效的历史日期或今天。"
            self._render_status()
            return

        self.loading, self.error = True, ""
        self._generation += 1
        generation = self._generation
        self._cancel_event = cancel = threading.Event()
        self._render_status()
        try:
            snapshot = await run.io_bound(load_region_climate, day, cancelled=cancel.is_set)
            if not isinstance(snapshot, ClimateSnapshot):
                raise XiaomaiSourceError("查询未返回完整结果，请重试。")
            if self.mounted and self._generation == generation:
                self._calculate_analyses(snapshot)
                self.snapshot = snapshot
                self.dirty = False
                self.event_id = ""
                self._render_results()
        except XiaomaiSourceError as error:
            if self.mounted and self._generation == generation:
                self.error = str(error)
        except Exception:
            if self.mounted and self._generation == generation:
                self.error = "区域温湿度查询未完成，请稍后重试。"
        finally:
            if self.mounted and self._generation == generation:
                self.loading = False
                self._render_status()

    def stop_waiting(self) -> None:
        if self.loading:
            self._cancel_event.set()
            self._generation += 1
            self.loading = False
            self.error = "已停止等待。"
            self._render_status()

    def _calculate_analyses(self, snapshot: ClimateSnapshot) -> None:
        self.analyses = {
            region.id: analyze_region(snapshot, region, lower=self.lower, upper=self.upper)
            for region in REGIONS
        }

    def _render_results(self) -> None:
        if not self.mounted:
            return
        self.results.clear()
        self.chart = None
        self.region_distribution_chart = self.probe_distribution_chart = None
        with self.results:
            if self.snapshot is None:
                with ui.column().classes("empty-state"):
                    ui.icon("thermostat")
                    ui.label("选择日期查看区域温湿度").classes("empty-title")
                    ui.label(
                        "查询当天七个区域的曲线、AHU 开机前后温度范围和温度越线时段。"
                    ).classes("empty-copy")
                return
            self._render_overview()
            with ui.column().classes("region-climate-panel region-climate-distribution"):
                with ui.row().classes("region-climate-panel-heading"):
                    ui.label("七个区域 · 温区分布").classes("region-climate-section-title")
                    with ui.row().classes("region-climate-distribution-controls"):
                        ui.toggle(
                            DISTRIBUTION_VIEWS,
                            value=self.distribution_view,
                            on_change=self._select_distribution_view,
                        ).props('dense no-caps aria-label="温区显示方式"')
                        self.distribution_interval = ui.select(
                            {minutes: f"{minutes} 分钟" for minutes in TIME_CELL_MINUTES},
                            value=self.distribution_minutes,
                            on_change=self._select_distribution_interval,
                        ).props('dense outlined aria-label="方格时间间隔"')
                        self.distribution_interval.set_visibility(self.distribution_view == "time")
                self.region_distribution_body = ui.column().classes("w-full gap-2")
                self._render_region_distribution()
            self.detail = ui.column().classes("region-climate-detail")
        self._render_detail()

    def _select_distribution_view(self, event: events.ValueChangeEventArguments) -> None:
        if (
            isinstance(event.value, str)
            and event.value in DISTRIBUTION_VIEWS
            and event.value != self.distribution_view
        ):
            self.distribution_view = event.value
            self.distribution_interval.set_visibility(self.distribution_view == "time")
            self._update_distributions()

    def _select_distribution_interval(self, event: events.ValueChangeEventArguments) -> None:
        if (
            isinstance(event.value, int)
            and event.value in TIME_CELL_MINUTES
            and event.value != self.distribution_minutes
        ):
            self.distribution_minutes = event.value
            self._update_distributions()

    def _update_distributions(self) -> None:
        self._render_region_distribution()
        self._render_probe_distribution(REGION_BY_ID[self.region_id])

    def _distribution_options(self, region: Region | None) -> dict[str, Any]:
        assert self.snapshot is not None
        if self.distribution_view == "share":
            distributions = (
                [
                    region_distribution(f"{item.floor} · {item.name}", self.analyses[item.id].trend)
                    for item in REGIONS
                ]
                if region is None
                else probe_distributions(self.snapshot, region)
            )
            return temperature_distribution_options(
                _distribution_rows(distributions), self.dark, show_missing=True
            )
        minutes = self.distribution_minutes
        if region is None:
            rows = [
                (
                    f"{item.floor} · {item.name}",
                    region_temperature_cells(self.analyses[item.id].trend, minutes),
                )
                for item in REGIONS
            ]
        else:
            rows = [
                (probe.name + ("（待确认）" if probe.temperature is None else ""), cells)
                for probe, cells in zip(
                    region.probes,
                    probe_temperature_cells(self.snapshot, region, minutes),
                    strict=True,
                )
            ]
        return temperature_time_options(rows, self.dark, minutes=minutes)

    def _render_region_distribution(self) -> None:
        self.region_distribution_body.clear()
        with self.region_distribution_body:
            self.region_distribution_chart = self._render_distribution("七个区域 · 温区分布", None)
            self.region_distribution_chart.on_point_click(self._select_distribution_region)

    def _render_probe_distribution(self, region: Region) -> None:
        self.probe_distribution_body.clear()
        with self.probe_distribution_body:
            self.probe_distribution_chart = self._render_distribution(
                f"{region.name} · 探头温区分布", region
            )

    def _render_distribution(self, title: str, region: Region | None) -> ui.echart:
        row_count = len(region.probes) if region else len(REGIONS)
        time_view = self.distribution_view == "time"
        if time_view:
            caption = f"北京时间 · 每格 {self.distribution_minutes} 分钟 · 颜色表示时段均温。"
            if region is None:
                caption += "点击方格查看该区域的探头。"
            detail = "按时间"
            height = max(230, 126 + row_count * 27)
        else:
            caption = "按有效 5 分钟温度样本统计各温区占比，温区左闭右开。"
            detail = "有效采样占比"
            height = max(240, 122 + row_count * 36)
        ui.label(caption).classes("description")
        return (
            ui.echart(self._distribution_options(region))
            .props(f'role="img" aria-label="{title} · {detail}"')
            .classes("region-climate-distribution-chart")
            .style(f"height: {height}px")
        )

    def _render_overview(self) -> None:
        ui.label("七个区域 · 当日范围").classes("region-climate-section-title")
        columns = [
            {"name": name, "field": name, "label": label, "align": "left"}
            for name, label in (
                ("region", "区域"),
                ("ahu", "服务 AHU"),
                ("temperature", "温度范围"),
                ("humidity", "相对湿度范围"),
                ("excursions", "温度越线"),
            )
        ]
        self.overview = (
            ui.table(columns=columns, rows=self._overview_rows(), row_key="key")
            .props("flat hide-bottom separator=horizontal :rows-per-page-options='[0]'")
            .classes("data-table region-climate-table")
        )
        self.overview.add_slot(
            "body-cell-region",
            """
            <q-td :props="props">
                <q-btn flat dense no-caps class="region-climate-region-button"
                    :class="{active: props.row.active}" :aria-pressed="props.row.active"
                    :label="props.value" @click="$parent.$emit('inspect', props.row.key)" />
            </q-td>
            """,
        )
        self.overview.on("inspect", self._select_region)

    def _overview_rows(self) -> list[dict[str, Any]]:
        rows = []
        for region in REGIONS:
            analysis = self.analyses[region.id]
            temperatures = [
                point.temperature for point in analysis.trend if point.temperature is not None
            ]
            humidities = [point.humidity for point in analysis.trend if point.humidity is not None]
            cold = sum(e.probe_id is None and e.kind == "cold" for e in analysis.excursions)
            hot = sum(e.probe_id is None and e.kind == "hot" for e in analysis.excursions)
            probes = len({e.probe_id for e in analysis.excursions if e.probe_id is not None})
            rows.append(
                {
                    "key": region.id,
                    "region": f"{region.floor} · {region.name}",
                    "ahu": region.ahu_id,
                    "temperature": _range_of(temperatures, "℃"),
                    "humidity": _range_of(humidities, "%RH"),
                    "excursions": (
                        f"均温冷 {cold} 段／热 {hot} 段 · {probes} 个探头越线"
                        if cold or hot or probes
                        else "无已观测越线"
                        if temperatures
                        else "温度缺测"
                    ),
                    "active": self.region_id == region.id,
                }
            )
        return rows

    def _select_region(self, event: events.GenericEventArguments) -> None:
        if not isinstance(event.args, str) or event.args not in REGION_BY_ID:
            return
        self._show_region(event.args)

    def _select_distribution_region(self, event: events.EChartPointClickEventArguments) -> None:
        if event.sender is not self.region_distribution_chart:
            return
        index = (
            event.value[1]
            if event.series_type == "heatmap"
            and isinstance(event.value, list)
            and len(event.value) > 1
            else event.data_index
        )
        if isinstance(index, int) and 0 <= index < len(REGIONS):
            self._show_region(REGIONS[index].id)

    def _show_region(self, region_id: str) -> None:
        self.region_id, self.probe_id, self.event_id = region_id, None, ""
        self.overview.update_rows(self._overview_rows())
        self._render_detail()

    def _select_probe(self, event: events.ValueChangeEventArguments) -> None:
        self.probe_id = int(event.value) if event.value else None
        self._render_detail()

    def _select_start(self, event: events.ValueChangeEventArguments) -> None:
        self.event_id = event.value if isinstance(event.value, str) else ""
        if self.detail_analysis is not None and self.snapshot is not None:
            self._render_comparison(self.detail_analysis.events, self.snapshot.day)

    def _probe_name(self) -> str:
        return next(
            (
                probe.name
                for probe in REGION_BY_ID[self.region_id].probes
                if probe.id == self.probe_id
            ),
            "区域均值",
        )

    def _chart_options(self, analysis: RegionAnalysis) -> dict[str, Any]:
        return trend_options(
            analysis.trend,
            tuple(event.at for event in analysis.events),
            tuple(
                excursion
                for excursion in analysis.excursions
                if excursion.probe_id == self.probe_id
            ),
            self.lower,
            self.upper,
            self.dark,
            label=self._probe_name(),
        )

    def _render_detail(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            return
        region = REGION_BY_ID[self.region_id]
        analysis = (
            self.analyses[self.region_id]
            if self.probe_id is None
            else analyze_region(
                snapshot, region, lower=self.lower, upper=self.upper, probe_id=self.probe_id
            )
        )
        self.detail_analysis = analysis
        self.detail.clear()
        with self.detail:
            with ui.column().classes("region-climate-panel"):
                ui.label(f"{region.name} · 探头温区分布").classes("region-climate-section-title")
                self.probe_distribution_body = ui.column().classes("w-full gap-2")
                self._render_probe_distribution(region)
            with ui.column().classes("region-climate-panel"):
                with ui.row().classes("region-climate-panel-heading"):
                    with ui.column().classes("gap-0"):
                        ui.label(f"{region.name} · 全天趋势").classes(
                            "region-climate-section-title"
                        )
                        ui.label(f"{region.floor} · 芒市 {region.ahu_id}").classes("description")
                    options = {"": "区域均值"}
                    for probe in region.probes:
                        suffix = "（点位待确认）" if probe.temperature is None else ""
                        options[str(probe.id)] = probe.name + suffix
                    ui.select(
                        options,
                        label="探头",
                        value=str(self.probe_id) if self.probe_id else "",
                        on_change=self._select_probe,
                    ).props('dense outlined aria-label="选择探头"').classes("region-climate-probe")
                self.chart = ui.echart(self._chart_options(analysis)).classes(
                    "region-climate-chart"
                )
                with ui.row().classes("region-climate-legend description"):
                    for style, text in (
                        ("running", "AHU 运行"),
                        ("start", "实际开启"),
                        ("cold", "过冷"),
                        ("hot", "过热"),
                    ):
                        with ui.row().classes(f"region-climate-key {style}"):
                            ui.element("span").props('aria-hidden="true"')
                            ui.label(text)
                valid_temperature = sum(point.temperature is not None for point in analysis.trend)
                valid_humidity = sum(point.humidity is not None for point in analysis.trend)
                note = (
                    f"有效温度 {valid_temperature}/{len(analysis.trend)} 桶 · "
                    f"有效湿度 {valid_humidity}/{len(analysis.trend)} 桶"
                )
                pending = sum(probe.temperature is None for probe in region.probes)
                if pending and self.probe_id is None:
                    note += f" · {pending} 个探头点位待确认"
                if valid_temperature < len(analysis.trend) or valid_humidity < len(analysis.trend):
                    note += " · 曲线断点表示缺测"
                ui.label(note).classes("description")
            with ui.element("div").classes("region-climate-pair"):
                self._render_starts(analysis.events, snapshot.day, region)
                self._render_excursions(analysis, snapshot.day, region)

    def _render_starts(
        self, comparisons: tuple[StartComparison, ...], day: date, region: Region
    ) -> None:
        with ui.column().classes("region-climate-panel"):
            ui.label(f"{region.ahu_id} 开机前后").classes("region-climate-section-title")
            if not comparisons:
                ui.label("当天无已确认的停机转开机事件。").classes("description")
                return
            choices = {event.at.isoformat(): _clock(event.at, day) for event in comparisons}
            if self.event_id not in choices:
                self.event_id = next(iter(choices))
            ui.select(
                choices, label="实际开机时刻", value=self.event_id, on_change=self._select_start
            ).props('dense outlined aria-label="实际开机时刻"').classes("region-climate-event")
            self.comparison = ui.column().classes("region-climate-comparison")
            self._render_comparison(comparisons, day)

    def _render_comparison(self, comparisons: tuple[StartComparison, ...], day: date) -> None:
        comparison = next(
            (item for item in comparisons if item.at.isoformat() == self.event_id), None
        )
        self.comparison.clear()
        if comparison is None:
            return
        with self.comparison:
            with ui.element("div").classes("region-climate-ranges"):
                self._temperature_range("开启前", comparison.before, day)
                self._temperature_range("开启后", comparison.after, day)
            if comparison.probe_count:
                ui.label(f"前后使用同一组 {comparison.probe_count} 个探头。").classes("description")
            else:
                ui.label("开机前后缺少共同的有效探头，温度范围无法直接对比。").classes(
                    "description"
                )

    @staticmethod
    def _temperature_range(label: str, period: TemperatureRange, day: date) -> None:
        with ui.column().classes("region-climate-range"):
            ui.label(label).classes("description")
            ui.label(_range_text(period.minimum, period.maximum, "℃")).classes(
                "region-climate-value"
            )
            ui.label(
                f"{_clock(period.start, day)}～{_clock(period.end, day)} · "
                f"{period.sample_count} 个有效桶 · {period.probe_count} 个探头"
            ).classes("description")

    def _render_excursions(self, analysis: RegionAnalysis, day: date, region: Region) -> None:
        with ui.column().classes("region-climate-panel"):
            ui.label("过冷与过热时段").classes("region-climate-section-title")
            ui.label(
                f"低于 {self.lower:g}℃ 判为过冷，高于 {self.upper:g}℃ 判为过热。连续有效桶合并。"
            ).classes("description")
            if not analysis.excursions:
                valid = sum(point.temperature is not None for point in analysis.trend)
                if not valid:
                    text = "缺少有效温度数据。"
                elif valid < len(analysis.trend):
                    text = "有效温度数据中未观测到越线，缺测时段无法判断。"
                else:
                    text = "有效温度数据中未观测到越线。"
                ui.label(text).classes("description")
                return
            names = {probe.id: probe.name for probe in region.probes}
            columns = [
                {"name": name, "field": name, "label": label, "align": "left"}
                for name, label in (
                    ("type", "状态"),
                    ("probe", "范围"),
                    ("time", "时段"),
                    ("temperature", "温度"),
                )
            ]
            rows = [
                self._excursion_row(index, item, day, names)
                for index, item in enumerate(analysis.excursions)
            ]
            table = (
                ui.table(columns=columns, rows=rows, row_key="key", pagination=6)
                .props("flat separator=horizontal :rows-per-page-options='[6]'")
                .classes("data-table region-climate-table")
            )
            table.add_slot(
                "body-cell-type",
                '<q-td :props="props"><span :class="[\'region-climate-tag\', props.row.kind]">'
                "{{ props.value }}</span></q-td>",
            )

    @staticmethod
    def _excursion_row(
        index: int, item: Excursion, day: date, names: dict[int, str]
    ) -> dict[str, Any]:
        return {
            "key": index,
            "type": EXCURSION_LABELS[item.kind],
            "kind": item.kind,
            "probe": "区域均值" if item.probe_id is None else names[item.probe_id],
            "time": f"{_clock(item.start, day)}～{_clock(item.end, day)}",
            "temperature": _range_text(item.minimum, item.maximum, "℃"),
        }
