"""航信专栏：双栏航班与合并进出港的空档视图。"""

from datetime import date, datetime
from typing import Any

from nicegui import run, ui

from axiomforge.flight_source import FlightSourceError, load_flights
from axiomforge.flights import (
    FLIGHT_AREAS,
    REGION_LABELS,
    SHANGHAI,
    Flight,
    FlightGap,
    FlightSnapshot,
    flight_gaps,
    flights_for_area,
    format_moment,
)


class FlightView:
    """查询与筛选均属于当前浏览器；离开页面不重定向后台请求。"""

    def __init__(self) -> None:
        self.day_input = datetime.now(SHANGHAI).date().isoformat()
        self.day = date.fromisoformat(self.day_input)
        self.snapshot: FlightSnapshot | None = None
        self.mode = "planned"
        self.minimum = 30.0
        self.area = "all"
        self.search = ""
        self.direction = "all"
        self.status = "全部"
        self.loading = False
        self.mounted = False
        self.generation = 0
        self.error = ""
        self.dark = False
        self.container: ui.column | None = None
        self.results: ui.column | None = None
        self.lists: ui.element | None = None
        self.chart: ui.echart | None = None

    def deactivate(self) -> None:
        """离页或注销后让正在等待的只读结果失效。"""
        self.mounted = False
        self.generation += 1
        self.loading = False

    def reconnect(self) -> None:
        if self.container is not None and not self.container.is_deleted:
            self.mounted = True

    def build(self, dark: bool) -> None:
        self.mounted = True
        self.dark = dark
        self.container = ui.column().classes("flight-page w-full gap-0")
        self.redraw()

    def redraw(self) -> None:
        if self.container is None or self.container.is_deleted:
            return
        self.container.clear()
        with self.container:
            with ui.row().classes("breadcrumb"):
                ui.label("航信机场")
                ui.icon("chevron_right")
                ui.label("芒市机场 · 项目专题")
            with ui.row().classes("page-heading"):
                ui.label("今日航班").classes("page-title")
                ui.label("只读查询").classes("demo-badge")
            ui.label("按业务计划日查看航班，识别进出港之间的长间隔。").classes("description")
            with ui.row().classes("flight-toolbar w-full"):
                ui.input("业务计划日", value=self.day_input, on_change=self.change_day).props(
                    'type=date outlined dense aria-label="航班计划日"'
                ).classes("flight-date").set_enabled(not self.loading)
                ui.input("航班号 / 城市", value=self.search, on_change=self.change_search).props(
                    'outlined dense debounce=300 clearable aria-label="搜索航班"'
                ).classes("flight-search")
                ui.select(
                    {"all": "全部进出港", "I": "进港", "O": "出港"},
                    value=self.direction,
                    on_change=self.change_direction,
                ).props("outlined dense").classes("flight-direction")
                ui.select(
                    [
                        "全部",
                        "计划",
                        "已到达芒市",
                        "已从芒市起飞",
                        "已从始发站起飞",
                        "催促登机",
                        "状态待复核",
                    ],
                    value=self.status,
                    on_change=self.change_status,
                ).props('outlined dense aria-label="航班状态"')
                ui.space()
                ui.button(
                    "查询中…" if self.loading else "查询刷新", icon="refresh", on_click=self.refresh
                ).props("unelevated no-caps").classes("primary-btn").set_enabled(not self.loading)
            if self.loading:
                ui.linear_progress().props("indeterminate")
            if self.error:
                ui.label(self.error).classes("flight-warning")
            if self.snapshot and self.day_input != self.day.isoformat():
                ui.label("日期已修改，请查询刷新；下方仍显示上次日期的快照。").classes(
                    "flight-warning"
                )
            self.results = ui.column().classes("w-full gap-0")
            self.render_results()

    def change_day(self, event: Any) -> None:
        self.day_input = str(event.value or "")
        self.redraw()

    def change_search(self, event: Any) -> None:
        self.search = str(event.value or "")
        self.render_lists()

    def change_direction(self, event: Any) -> None:
        self.direction = str(event.value)
        self.render_lists()

    def change_status(self, event: Any) -> None:
        self.status = str(event.value)
        self.render_lists()

    async def refresh(self) -> None:
        if self.loading or not self.mounted:
            return
        try:
            requested = date.fromisoformat(self.day_input)
        except ValueError:
            self.error = "请选择有效的业务计划日。"
            self.redraw()
            return
        self.loading = True
        self.generation += 1
        generation = self.generation
        self.error = ""
        self.redraw()
        try:
            snapshot = await run.io_bound(load_flights, requested)
            if self.mounted and generation == self.generation:
                self.snapshot, self.day = snapshot, requested
        except FlightSourceError as exc:
            if self.mounted and generation == self.generation:
                self.error = str(exc)
        finally:
            if self.mounted and generation == self.generation:
                self.loading = False
                self.redraw()

    def selected(self) -> list[Flight]:
        return [f for f in self.snapshot.flights if f.day == self.day] if self.snapshot else []

    def render_results(self) -> None:
        if self.results is None or self.results.is_deleted:
            return
        self.results.clear()
        self.chart = None
        with self.results:
            if not self.snapshot:
                with ui.column().classes("empty-state"):
                    ui.icon("flight")
                    ui.label("查询芒市机场航班").classes("empty-title")
                    ui.label("点击查询刷新，读取所选计划日与相邻计划日的航班快照。").classes(
                        "empty-copy"
                    )
                return
            selected = self.selected()
            with ui.row().classes("flight-summary w-full"):
                for name, value in [
                    ("计划航班", len(selected)),
                    ("进港", sum(f.direction == "I" for f in selected)),
                    ("出港", sum(f.direction == "O" for f in selected)),
                    ("本场已有实际时刻", sum(f.actual is not None for f in selected)),
                ]:
                    with ui.row().classes("flight-metric"):
                        ui.label(name).classes("description")
                        ui.label(str(value)).classes("flight-number")
                ui.space()
                ui.label(f"{self.day:%Y-%m-%d} · 北京时间").classes("description")
                ui.label(f"快照 {self.snapshot.fetched_at:%m-%d %H:%M:%S}").classes("description")
            for warning in self.snapshot.warnings:
                ui.label(warning).classes("flight-warning")
            with ui.row().classes("flight-area-bar w-full"):
                for key, label in FLIGHT_AREAS.items():
                    count = len(flights_for_area(selected, key))
                    ui.button(
                        f"{label} {count}",
                        color=None,
                        on_click=lambda key=key: self.change_area(key),
                    ).props("flat no-caps").classes(
                        "flight-area-button" + (" active" if key == self.area else "")
                    )
            unknown_count = sum(f.region == "unknown" for f in selected)
            if unknown_count:
                ui.label(
                    f"{unknown_count} 班国内／国际属性待确认，未归入具体区域；"
                    "同方向区域的间隔计算会保留这些航班作为阻断项。"
                ).classes("flight-warning")
            with ui.column().classes("flight-gap-panel w-full"):
                with ui.row().classes("w-full items-center"):
                    ui.icon("view_timeline").classes("flight-accent text-xl")
                    ui.label(f"航班间隔 · {FLIGHT_AREAS[self.area]}").classes(
                        "flight-section-title"
                    )
                    ui.space()
                    ui.toggle(
                        {"planned": "计划时刻", "dynamic": "动态时刻"},
                        value=self.mode,
                        on_change=self.change_mode,
                    ).props("no-caps unelevated")
                    ui.number(
                        "显示间隔 ≥ 分钟",
                        value=self.minimum,
                        min=1,
                        max=1440,
                        on_change=self.change_minimum,
                    ).props("outlined dense").classes("flight-minimum")
                ui.label(
                    "按所选区域计算，不受下方搜索／状态／方向筛选影响。"
                    "动态时刻：实际优先，其次预计，再用计划。"
                ).classes("description")
                gaps = self.current_gaps()
                if selected:
                    self.chart = ui.echart(self.timeline_options(gaps)).classes("flight-timeline")
                if gaps:
                    gap_rows = [
                        {
                            "id": i,
                            "period": f"{format_moment(g.start, self.day)} — "
                            f"{format_moment(g.end, self.day)}",
                            "minutes": round(g.minutes, 1),
                            "before": " / ".join(
                                f.number + (" 进港" if f.direction == "I" else " 出港")
                                for f in g.before
                            ),
                            "after": " / ".join(
                                f.number + (" 进港" if f.direction == "I" else " 出港")
                                for f in g.after
                            ),
                        }
                        for i, g in enumerate(gaps)
                    ]
                    cols = [
                        {"name": k, "label": label, "field": k, "align": "left"}
                        for k, label in [
                            ("period", "空档时段（北京时间）"),
                            ("minutes", "时长 / 分钟"),
                            ("before", "前序航班"),
                            ("after", "后续航班"),
                        ]
                    ]
                    ui.table(
                        columns=cols, rows=gap_rows, row_key="id", pagination={"rowsPerPage": 3}
                    ).props("flat wrap-cells").classes("data-table flight-gap-table")
                else:
                    ui.label("当前快照未识别到满足阈值的相邻航班间隔。").classes("description py-4")
                ui.label("保障时间：待配置 · 可用于策略评估的时段：待确定").classes("flight-buffer")
                ui.label(
                    "计划空档不代表无人使用。需扣除到港后保障、出港前准备时间；"
                    "数据缺失或航班调整可能改变间隔，当前不生成停机指令。"
                ).classes("description")
            self.lists = ui.element("div").classes("flight-list-grid w-full")
            self.render_lists()
            with ui.expansion("数据口径与整理记录", icon="info_outline").classes(
                "flight-provenance w-full"
            ):
                ui.label(
                    f"相邻三个计划日原始 {self.snapshot.raw_count} 条；"
                    + "；".join(
                        f"{name} {self.snapshot.counts.get(key, 0)} 条"
                        for key, name in [
                            ("old_versions", "旧版本"),
                            ("shared_aliases", "共享别名"),
                            ("deleted", "删除主号"),
                            ("non_operating", "非营运主号"),
                        ]
                    )
                ).classes("description")
                ui.label(
                    "国内／国际使用 fcategory=0／1 的当前源库样本映射，正式枚举字典待确认；"
                    "不将区域分类等同于空调设备映射。"
                ).classes("description")
                ui.label(
                    "查询本场 LUM 及相邻计划日，未使用 ts 截取。仅报告已知事件之间的间隔，"
                    "不推断首末边界空档；上游漏报、状态冲突与更远跨日仍需复核。"
                ).classes("description")

    def change_mode(self, event: Any) -> None:
        self.mode = str(event.value)
        self.render_results()

    def change_area(self, area: str) -> None:
        self.area = area
        self.direction = "all"
        self.redraw()

    def current_gaps(self) -> list[FlightGap]:
        if self.snapshot is None:
            return []
        return flight_gaps(self.snapshot.flights, self.day, self.mode, self.minimum, area=self.area)

    def change_minimum(self, event: Any) -> None:
        try:
            value = float(event.value)
        except (ValueError, TypeError):
            return
        if 1 <= value <= 1440:
            self.minimum = value
            self.render_results()

    def render_lists(self) -> None:
        if self.lists is None or self.lists.is_deleted:
            return
        self.lists.clear()
        self.lists.classes(
            add="flight-list-single" if self.area != "all" or self.direction != "all" else "",
            remove="flight-list-single" if self.area == "all" and self.direction == "all" else "",
        )
        flights = flights_for_area(self.selected(), self.area)
        query = self.search.casefold()
        filtered = [
            f
            for f in flights
            if (not query or query in (f.number + f.route + " ".join(f.aliases)).casefold())
            and (self.status == "全部" or f.status == self.status)
        ]
        with self.lists:
            for direction, title, icon in [
                ("I", "进港", "flight_land"),
                ("O", "出港", "flight_takeoff"),
            ]:
                if self.direction not in {"all", direction}:
                    continue
                if self.area != "all" and self.area.split(":")[1] != direction:
                    continue
                all_direction = [f for f in flights if f.direction == direction]
                visible = [f for f in filtered if f.direction == direction]
                with ui.column().classes("flight-list w-full gap-0"):
                    with ui.row().classes("flight-list-heading"):
                        ui.icon(icon).classes("flight-accent")
                        ui.label(f"{title} {len(all_direction)}").classes("flight-section-title")
                        ui.label(
                            f"已有实际时刻 {sum(f.actual is not None for f in all_direction)}"
                        ).classes("description")
                    rows = [
                        {
                            "id": f.key,
                            "number": f.number,
                            "region": REGION_LABELS[f.region],
                            "route": f.route,
                            "planned": format_moment(f.planned, self.day),
                            "estimated": format_moment(f.estimated, self.day),
                            "actual": format_moment(f.actual, self.day),
                            "status": f.status,
                            "gate": f.gate or "—",
                            "times": f.planned.isoformat() if f.planned else "9999",
                        }
                        for f in visible
                    ]
                    fields = [
                        ("number", "航班 / 航线"),
                        ("times", "本场时刻"),
                        ("status", "本场状态"),
                    ]
                    if direction == "O":
                        fields.append(("gate", "登机口"))
                    cols = [
                        {"name": k, "label": label, "field": k, "align": "left", "sortable": True}
                        for k, label in fields
                    ]
                    table = (
                        ui.table(
                            columns=cols, rows=rows, row_key="id", pagination={"rowsPerPage": 7}
                        )
                        .props("flat wrap-cells")
                        .classes("data-table flight-table")
                    )
                    table.add_slot(
                        "body-cell-number",
                        """<q-td :props="props"><div class="flight-id">{{ props.value }}</div>
                        <div class="flight-route"><span class="flight-region">
                        {{ props.row.region }}</span> {{ props.row.route }}</div></q-td>""",
                    )
                    table.add_slot(
                        "body-cell-times",
                        """<q-td :props="props"><div class="flight-times">
                        <span><small>计划</small>{{ props.row.planned }}</span>
                        <span><small>预计</small>{{ props.row.estimated }}</span>
                        <span><small>实际</small>{{ props.row.actual }}</span>
                        </div></q-td>""",
                    )
                    table.add_slot(
                        "body-cell-status",
                        """
                        <q-td :props="props"><span class="flight-state"
                        :class="props.value.startsWith('已到达')
                        || props.value.startsWith('已从芒市')
                        ? 'complete' : props.value === '计划' ? 'planned' : 'pending'">
                        {{ props.value }}</span></q-td>""",
                    )

    def timeline_options(self, gaps: list[FlightGap]) -> dict[str, Any]:
        assert self.snapshot is not None
        scoped = flights_for_area(self.snapshot.flights, self.area, include_unknown=True)
        selected = [f for f in scoped if f.day == self.day]
        moments = [t for f in selected if (t := f.event_time(self.mode)) is not None]
        if not moments:
            return {"series": []}
        low, high = min(moments), max(moments)
        if gaps:
            low = min(low, min(g.start for g in gaps))
            high = max(high, max(g.end for g in gaps))
        color = "#a1afc4" if self.dark else "#64748b"
        series: list[dict[str, Any]] = []
        tracks = [
            ("domestic", "I", "国内进港", "#818cf8"),
            ("domestic", "O", "国内出港", "#14b8a6"),
            ("international", "I", "国际进港", "#f59e0b"),
            ("international", "O", "国际出港", "#ec4899"),
        ]
        tracks = [t for t in tracks if self.area == "all" or f"{t[0]}:{t[1]}" == self.area]
        for direction in ("I", "O"):
            if any(f.region == "unknown" and f.direction == direction for f in scoped):
                tracks.append(
                    (
                        "unknown",
                        direction,
                        "待确认" + ("进港" if direction == "I" else "出港"),
                        "#94a3b8",
                    )
                )
        for region, direction, name, tint in tracks:
            series.append(
                {
                    "name": name,
                    "type": "scatter",
                    "symbol": "rect",
                    "symbolSize": [3, 15],
                    "itemStyle": {"color": tint},
                    "data": [
                        [t.isoformat(), name, f.number]
                        for f in scoped
                        if f.direction == direction
                        and f.region == region
                        and (t := f.event_time(self.mode)) is not None
                        and low <= t <= high
                    ],
                }
            )
        series[0]["markArea"] = {
            "silent": True,
            "itemStyle": {"color": "rgba(245, 158, 11, 0.17)"},
            "data": [
                [{"xAxis": g.start.isoformat()}, {"xAxis": g.end.isoformat()}]
                for g in gaps
                if g.start >= low and g.end <= high
            ],
        }
        return {
            "animation": False,
            "backgroundColor": "transparent",
            "useUTC": True,
            "grid": {"top": 16, "left": 85, "right": 30, "bottom": 35},
            "textStyle": {"color": color},
            "tooltip": {
                "trigger": "item",
                "renderMode": "richText",
                ":formatter": "params => params.data ? "
                "params.data[2] + ' · ' + params.seriesName : ''",
            },
            "xAxis": {
                "type": "time",
                "min": low.isoformat(),
                "max": high.isoformat(),
                "axisLabel": {
                    "color": color,
                    ":formatter": "value => new Intl.DateTimeFormat('zh-CN', "
                    "{timeZone:'Asia/Shanghai',"
                    "month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',"
                    "hour12:false}).format(new Date(value))",
                },
                "splitLine": {"show": False},
                "axisLine": {"lineStyle": {"color": color}},
            },
            "yAxis": {
                "type": "category",
                "data": [t[2] for t in reversed(tracks)],
                "axisLabel": {"color": color},
                "axisTick": {"show": False},
                "axisLine": {"show": False},
                "splitLine": {"show": False},
            },
            "series": series,
        }

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        if self.chart and not self.chart.is_deleted and self.snapshot:
            gaps = self.current_gaps()
            self.chart.options.clear()
            self.chart.options.update(self.timeline_options(gaps))
            self.chart.update()
