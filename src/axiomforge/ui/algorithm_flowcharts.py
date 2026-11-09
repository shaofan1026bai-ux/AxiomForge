"""按项目维护算法图示；编辑草稿不等于已发布算法或现场参数。"""

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from nicegui import events, run, ui

from axiomforge.algorithm_flowcharts import (
    FlowchartDraft,
    FlowchartError,
    FlowchartRevision,
    sanitize_svg,
    source_hash,
    validate_draft,
    validate_source,
)
from axiomforge.demo import PROJECT_BY_ID
from axiomforge.flowchart_store import FlowchartStore
from axiomforge.ui.flowchart_canvas import FlowchartCanvas

NEW_SOURCE = 'flowchart TD\n    Start["开始"] --> End["结束"]\n'


class AlgorithmFlowchartsView:
    """每个浏览器独立拥有草稿和预览，仅显式保存写入版本库。"""

    def __init__(self, store: FlowchartStore | None = None) -> None:
        self.store = store
        self.project_id = ""
        self.dark = False
        self.mounted = False
        self.loading = False
        self.rendering = False
        self.ticket = 0
        self.error = ""
        self.catalog: list[FlowchartRevision] = []
        self.revisions: list[FlowchartRevision] = []
        self.saved: FlowchartRevision | None = None
        self.draft: FlowchartDraft | None = None
        self.expected_revision: int | None = None
        self.derived_from_revision: int | None = None
        self.history_read_only = False
        self.valid_hash = ""
        self.valid_ticket = 0
        self.valid_source = ""
        self.valid_svg = ""
        self.valid_nodes: list[dict[str, Any]] = []
        self.selected_node = ""
        self.active_tab = "流程图"
        self._setting = False
        self._preserve = False
        self._pending_hash = ""
        self._pending_source = ""
        self.canvas: FlowchartCanvas | None = None
        self.container: ui.column | None = None

    @property
    def repository(self) -> FlowchartStore:
        if self.store is None:
            self.store = FlowchartStore()
        return self.store

    @property
    def dirty(self) -> bool:
        if self.draft is None:
            return False
        if self.saved is None:
            return True
        if self.derived_from_revision is not None:
            return True
        return any(
            getattr(self.draft, name) != getattr(self.saved, name)
            for name in ("name", "description", "source", "annotations")
        )

    @property
    def preview_stale(self) -> bool:
        return self.draft is not None and source_hash(self.draft.source) != self.valid_hash

    @property
    def can_save(self) -> bool:
        if (
            self.draft is None
            or not self.dirty
            or self.loading
            or self.rendering
            or self.history_read_only
            or not self.valid_svg
            or self.preview_stale
        ):
            return False
        try:
            validate_draft(self.draft)
        except FlowchartError:
            return False
        return True

    @property
    def can_export(self) -> bool:
        return bool(
            self.saved
            and not self.dirty
            and not self.loading
            and not self.rendering
            and not self.preview_stale
            and self.valid_svg
        )

    def deactivate(self) -> None:
        self.mounted = False
        self.ticket += 1
        self.rendering = False

    def reconnect(self) -> None:
        if self.container is not None and not self.container.is_deleted:
            self.mounted = True
            if self.canvas and self.draft:
                self.request_preview(self.valid_source or self.draft.source, preserve=True)
            else:
                self.render_status()

    def _clear_document(self) -> None:
        self.saved = None
        self.draft = None
        self.revisions = []
        self.expected_revision = None
        self.derived_from_revision = None
        self.history_read_only = False
        self.valid_hash = self.valid_source = self.valid_svg = ""
        self.valid_ticket = 0
        self.valid_nodes = []
        self.selected_node = ""
        self.error = ""
        self.rendering = False
        self.ticket += 1

    def _read_revision(self, revision: FlowchartRevision) -> None:
        self._clear_document()
        self.saved = revision
        self.draft = FlowchartDraft(
            revision.name, revision.description, revision.source, revision.annotations
        )
        self.revisions = self.repository.list_revisions(self.project_id, revision.flowchart_id)
        self.expected_revision = max(item.revision for item in self.revisions)
        self.history_read_only = revision.revision != self.expected_revision

    def build(self, project_id: str, dark: bool) -> None:
        if project_id not in PROJECT_BY_ID:
            raise ValueError("请先选择有效项目。")
        if project_id != self.project_id:
            self._clear_document()
            self.project_id = project_id
            try:
                self.catalog = self.repository.list_latest(project_id)
                if self.catalog:
                    self._read_revision(self.catalog[0])
            except FlowchartError as error:
                self.error = str(error)
                self.catalog = []
        self.dark, self.mounted = dark, True
        ui.add_css(Path(__file__).with_name("algorithm_flowcharts.css").read_text())
        self.container = ui.column().classes("flow-page w-full gap-0")
        with self.container:
            with ui.row().classes("breadcrumb"):
                ui.label(PROJECT_BY_ID[project_id].name)
                ui.icon("chevron_right")
                ui.label("算法与规则")
            with ui.row().classes("page-heading"):
                ui.label("算法流程图").props('role="heading" aria-level="1"').classes("page-title")
                ui.label("逻辑图示").classes("demo-badge")
            ui.label("用流程图理解和维护算法逻辑；图示版本独立保存，不下发设备控制。").classes(
                "description"
            )
            with ui.element("div").classes("flow-workspace"):
                self.catalog_container = ui.column().classes("flow-catalog gap-2")
                self.document = ui.column().classes("flow-document w-full gap-0")
            self.render_catalog()
            self.render_document()

    def render_catalog(self) -> None:
        self.catalog_container.clear()
        with self.catalog_container:
            ui.label("本项目图示").classes("flow-section-label")
            ui.label(PROJECT_BY_ID[self.project_id].name).classes("description")
            ui.button("新增流程图", icon="add", on_click=self.new_chart).props(
                "unelevated no-caps"
            ).classes("primary-btn w-full").set_enabled(not self.loading)
            if not self.catalog:
                ui.label("暂无已保存图示").classes("description mt-3")
            for item in self.catalog:
                ui.button(
                    item.name,
                    color=None,
                    on_click=lambda item=item: self.choose_chart(item.flowchart_id),
                ).props("flat no-caps").classes(
                    "flow-catalog-item"
                    + (
                        " active"
                        if self.saved and self.saved.flowchart_id == item.flowchart_id
                        else ""
                    )
                ).set_enabled(not self.loading)
                ui.label(f"图示 v{item.revision}").classes("flow-version-note")

    def render_document(self) -> None:
        self.document.clear()
        self.canvas = None
        if self.draft is None:
            with self.document, ui.column().classes("empty-state"):
                ui.icon("account_tree")
                ui.label("为当前项目创建第一张算法流程图").classes("empty-title")
                ui.label("粘贴Mermaid源码即可预览，保存后可查看历史版本并导出SVG。").classes(
                    "empty-copy"
                )
                if self.error:
                    ui.label(self.error).classes("flight-warning")
            return
        with self.document:
            with ui.row().classes("flow-header-actions w-full"):
                self.version_select = (
                    ui.select(
                        {item.revision: f"图示 v{item.revision}" for item in self.revisions},
                        value=self.saved.revision if self.saved else None,
                        label="图示版本",
                        on_change=self.choose_version,
                    )
                    .props('outlined dense aria-label="图示版本"')
                    .classes("flow-version-select")
                )
                self.version_select.set_enabled(bool(self.saved) and not self.loading)
                self.edit_history_button = (
                    ui.button("以此版本编辑", color=None, on_click=self.edit_history)
                    .props("flat no-caps")
                    .classes("secondary-btn")
                )
                self.edit_history_button.set_visibility(self.history_read_only)
                ui.space()
                self.preview_button = (
                    ui.button("更新预览", icon="refresh", color=None, on_click=self.preview)
                    .props("flat no-caps")
                    .classes("secondary-btn")
                )
                self.save_button = (
                    ui.button("保存新版本", icon="save", on_click=self.save)
                    .props("unelevated no-caps")
                    .classes("primary-btn")
                )
                self.export_button = (
                    ui.button("导出SVG", icon="file_download", color=None, on_click=self.download)
                    .props("flat no-caps")
                    .classes("secondary-btn")
                )
                self.export_button.tooltip("导出完整已保存版本，不受画布缩放影响")
            self.name_input = (
                ui.input(
                    "图示名称",
                    value=self.draft.name,
                    on_change=lambda e: self.edit("name", e.value),
                )
                .props('outlined dense maxlength=100 aria-label="图示名称"')
                .classes("w-full mt-3")
            )
            self.description_input = (
                ui.textarea(
                    "用途说明",
                    value=self.draft.description,
                    on_change=lambda e: self.edit("description", e.value),
                )
                .props('outlined dense autogrow rows=1 maxlength=2000 aria-label="图示用途说明"')
                .classes("w-full mt-3")
            )
            self.status = ui.column().classes("flow-status w-full gap-1")
            with ui.tabs().classes("flow-tabs w-full") as tabs:
                diagram_tab = ui.tab("流程图")
                source_tab = ui.tab("Mermaid源码")
            tabs.set_value(source_tab if self.active_tab == "Mermaid源码" else diagram_tab)
            with ui.element("div").classes("flow-editor-grid w-full") as self.editor_grid:
                with ui.column().classes("flow-source-column gap-2") as self.source_column:
                    ui.label("Mermaid源码").classes("flow-section-label")
                    self.source_input = (
                        ui.textarea(
                            value=self.draft.source,
                            on_change=lambda e: self.edit("source", e.value),
                        )
                        .props('outlined spellcheck=false aria-label="Mermaid源码"')
                        .classes("flow-source-editor w-full")
                    )
                    ui.label("修改后点击更新预览；写错时保留上次有效图。").classes("description")
                with ui.column().classes("flow-canvas-column gap-2"):
                    with ui.row().classes("w-full items-center"):
                        ui.label("逻辑流程图").classes("flow-section-label")
                        ui.space()
                        ui.button(
                            "节点详情", icon="info_outline", color=None, on_click=self.open_details
                        ).props("flat no-caps").classes("flow-details-open secondary-btn")
                    self.canvas = FlowchartCanvas(
                        on_rendered=self.on_rendered,
                        on_error=self.on_render_error,
                        on_node=self.on_node_selected,
                    ).classes("w-full")
                self.detail_panel = ui.column().classes("flow-detail-panel gap-2")
            self.render_details()
            self.apply_tab()
            tabs.on_value_change(self.change_tab)
            self.render_status()
        # 返回页面时优先恢复上次有效源码，不能把错误草稿画成已保存图。
        source = self.valid_source or self.draft.source
        self.request_preview(source, preserve=False)

    def change_tab(self, event: events.ValueChangeEventArguments) -> None:
        self.active_tab = str(event.value)
        self.apply_tab()

    def apply_tab(self) -> None:
        source_mode = self.active_tab == "Mermaid源码"
        self.source_column.set_visibility(source_mode)
        self.document.classes(
            add="source-mode" if source_mode else "", remove="" if source_mode else "source-mode"
        )

    def render_status(self) -> None:
        if not self.mounted or self.draft is None or self.status.is_deleted:
            return
        self.status.clear()
        with self.status:
            with ui.row().classes("items-center gap-2"):
                ui.label(
                    "未保存草稿"
                    if self.dirty
                    else f"历史版本 v{self.saved.revision}"
                    if self.history_read_only and self.saved
                    else f"已保存 · 图示 v{self.saved.revision}"
                    if self.saved
                    else "新图示"
                ).classes("flow-state-badge")
                if self.saved:
                    saved_time = self.saved.created_at.astimezone(ZoneInfo("Asia/Shanghai"))
                    ui.label(f"保存于 {saved_time:%Y-%m-%d %H:%M} · 上海时间").classes(
                        "flow-version-note"
                    )
            if self.loading:
                ui.label("正在保存图示版本…").classes("description")
            if self.rendering:
                ui.label("正在更新预览…").classes("description")
            if self.error:
                ui.label(self.error).classes("flight-warning")
            if self.preview_stale:
                ui.label(
                    "源码已修改，预览尚未更新；画布仍为上次有效源码。"
                    if self.valid_svg
                    else "请先更新有效预览，再保存图示。"
                ).classes("description")
            if self.dirty:
                ui.label("未保存改动不会随刷新恢复；保存后才可导出对应版本。").classes(
                    "description"
                )
        self.save_button.set_enabled(self.can_save)
        self.export_button.set_enabled(self.can_export)
        self.preview_button.set_enabled(not self.loading and not self.rendering)
        self.version_select.set_enabled(bool(self.saved) and not self.loading)
        for control in (self.name_input, self.description_input, self.source_input):
            control.set_enabled(not self.loading and not self.history_read_only)
        if self.canvas:
            self.canvas.set_dirty(self.dirty)

    def edit(self, field: str, value: Any) -> None:
        if self._setting or self.draft is None or self.history_read_only or self.loading:
            return
        if field not in {"name", "description", "source"}:
            return
        text = str(value or "")
        if field == "name":
            self.draft = replace(self.draft, name=text)
        elif field == "description":
            self.draft = replace(self.draft, description=text)
        else:
            self.draft = replace(self.draft, source=text)
        self.error = ""
        if field == "source":
            self.ticket += 1
            self.rendering = False
            if self.canvas:
                self.canvas.invalidate(self.ticket)
        self.render_status()

    def preview(self) -> None:
        if self.draft is not None and not self.loading:
            self.request_preview(self.draft.source, preserve=False)

    def request_preview(self, source: str, *, preserve: bool) -> None:
        if not self.mounted or self.canvas is None:
            return
        self.ticket += 1
        self._pending_source, self._pending_hash = source, source_hash(source)
        self._preserve = preserve
        self.error = ""
        try:
            validate_source(source)
        except FlowchartError as error:
            self.error = str(error)
            self.rendering = False
            self.canvas.invalidate(self.ticket)
            self.render_status()
            return
        self.rendering = True
        self.canvas.request_preview(source, self._pending_hash, self.ticket, self.dark)
        self.render_status()

    def on_rendered(self, event: events.GenericEventArguments) -> None:
        args = event.args
        if (
            not self.mounted
            or self.canvas is None
            or not isinstance(args, dict)
            or args.get("ticket") != self.ticket
            or args.get("hash") != self._pending_hash
        ):
            return
        try:
            svg = sanitize_svg(args.get("svg", ""))
            raw_nodes = args.get("nodes")
            if not isinstance(raw_nodes, list) or len(raw_nodes) > 100:
                raise FlowchartError("流程图节点信息无法核对。")
            nodes = []
            for node in raw_nodes:
                if (
                    not isinstance(node, dict)
                    or not isinstance(node.get("id"), str)
                    or not isinstance(node.get("label"), str)
                    or not isinstance(node.get("domId"), str)
                    or len(node["label"]) > 10000
                ):
                    raise FlowchartError("流程图节点信息无法核对。")
                nodes.append(node)
        except (FlowchartError, TypeError, ValueError):
            self.error = "预览内容未通过校验，上次有效图已保留。"
            self.rendering = False
            self.render_status()
            return
        self.valid_source, self.valid_hash, self.valid_svg = (
            self._pending_source,
            self._pending_hash,
            svg,
        )
        self.valid_nodes = nodes
        self.valid_ticket = self.ticket
        if self.selected_node not in {node["id"] for node in nodes}:
            self.selected_node = ""
        self.rendering = False
        self.canvas.publish(self.ticket, svg, nodes, self._preserve)
        self.render_status()
        self.render_details()

    def on_render_error(self, event: events.GenericEventArguments) -> None:
        args = event.args
        if not self.mounted or not isinstance(args, dict) or args.get("ticket") != self.ticket:
            return
        self.error = str(args.get("message", "Mermaid源码无法解析。"))[:600]
        self.rendering = False
        self.render_status()

    def on_node_selected(self, event: events.GenericEventArguments) -> None:
        args = event.args
        if (
            not self.mounted
            or not isinstance(args, dict)
            or args.get("hash") != self.valid_hash
            or args.get("ticket") != self.valid_ticket
            or args.get("id") not in {node["id"] for node in self.valid_nodes}
        ):
            return
        self.selected_node = str(args["id"])
        self.render_details()

    def render_details(self) -> None:
        self.detail_panel.clear()
        with self.detail_panel:
            self.detail_content()

    def detail_content(self) -> None:
        ui.label("节点详情").classes("flow-section-label")
        node = next((node for node in self.valid_nodes if node["id"] == self.selected_node), None)
        if node is None:
            ui.label("点击图中的节点查看说明。").classes("description")
            ui.label("菱形：判断条件\n矩形：动作／步骤\n黄色：可配置条件").classes(
                "description whitespace-pre-line"
            )
        else:
            ui.label(node["label"]).classes("flow-node-title")
            ui.label(f"节点 {node['id']} · {node.get('type', '步骤')}").classes("description")
            if node.get("configurable"):
                ui.label("可配置条件").classes("flow-configurable")
            annotation = self.draft.annotations.get(node["id"], {}) if self.draft else {}
            if annotation.get("description"):
                ui.label(annotation["description"]).classes("description")
            elif node.get("configurable"):
                ui.label("实际运行参数待关联；图示样式不会修改现场配置。").classes("description")
            if self.preview_stale:
                ui.label("当前节点属于上次有效预览。").classes("description")
        if self.draft:
            unmatched = set(self.draft.annotations) - {node["id"] for node in self.valid_nodes}
            if unmatched and self.valid_nodes:
                ui.label("待核对的节点注释：" + "、".join(sorted(unmatched))).classes("description")
        ui.separator()
        ui.label("仅用于逻辑说明，图示版本未绑定实际代码或设备执行。").classes("description")

    def open_details(self) -> None:
        with ui.dialog() as dialog, ui.card().classes("flow-detail-dialog"):
            self.detail_content()
            ui.button("关闭", on_click=dialog.close).props("flat no-caps")
        dialog.open()

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        if self.mounted and self.draft:
            self.request_preview(self.valid_source or self.draft.source, preserve=True)

    async def confirm_leave(self, extra_note: str = "") -> bool:
        if self.loading:
            ui.notify("正在保存图示，请稍候再切换。", type="info")
            return False
        if not self.dirty:
            return True
        with ui.dialog() as dialog, ui.card().classes("flow-leave-dialog"):
            ui.label("保留未保存的流程图改动？").classes("text-lg font-semibold")
            ui.label("放弃改动后将恢复已保存版本，新图示草稿不会保留。").classes("description")
            if extra_note:
                ui.label(extra_note).classes("description")
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", color=None, on_click=lambda: dialog.submit("cancel")).props(
                    "flat"
                )
                ui.button("放弃改动", color=None, on_click=lambda: dialog.submit("discard")).props(
                    "flat"
                )
                ui.button("保存并继续", on_click=lambda: dialog.submit("save")).set_enabled(
                    self.can_save
                )
        choice = await dialog
        if choice == "save":
            return await self.save()
        if choice == "discard":
            self.discard_draft()
            return True
        return False

    def discard_draft(self) -> None:
        if self.saved:
            self.draft = FlowchartDraft(
                self.saved.name, self.saved.description, self.saved.source, self.saved.annotations
            )
            self.derived_from_revision = None
            self.history_read_only = self.saved.revision != self.expected_revision
            # 已放弃源码的预览不能在返回模块时重新显示；纯元数据改动可复用原图。
            if self.valid_hash != source_hash(self.saved.source):
                self.valid_hash = self.valid_source = self.valid_svg = ""
                self.valid_ticket = 0
                self.valid_nodes = []
                self.selected_node = ""
            self.error = ""
            self.rendering = False
            self.ticket += 1
            if self.canvas:
                self.canvas.invalidate(self.ticket)
        else:
            self._clear_document()

    async def new_chart(self) -> None:
        if not await self.confirm_leave():
            return
        self._clear_document()
        self.draft = FlowchartDraft("新流程图", "", NEW_SOURCE)
        self.active_tab = "Mermaid源码"
        self.render_catalog()
        self.render_document()

    async def choose_chart(self, flowchart_id: str) -> None:
        if self.saved and flowchart_id == self.saved.flowchart_id and not self.history_read_only:
            return
        if not await self.confirm_leave():
            return
        try:
            self._read_revision(self.repository.get(self.project_id, flowchart_id))
        except FlowchartError as error:
            self.error = str(error)
            self.render_status()
            return
        self.render_catalog()
        self.render_document()

    async def choose_version(self, event: events.ValueChangeEventArguments) -> None:
        if self._setting or self.saved is None or event.value is None:
            return
        revision = int(event.value)
        if revision == self.saved.revision:
            return
        old_revision = self.saved.revision
        if not await self.confirm_leave():
            self._setting = True
            self.version_select.set_value(old_revision)
            self._setting = False
            return
        try:
            self._read_revision(
                self.repository.get(self.project_id, self.saved.flowchart_id, revision)
            )
        except FlowchartError as error:
            self.error = str(error)
            self.render_status()
            return
        self.render_document()

    def edit_history(self) -> None:
        if self.saved is None or not self.history_read_only:
            return
        self.derived_from_revision = self.saved.revision
        self.history_read_only = False
        self.edit_history_button.set_visibility(False)
        self.error = ""
        self.render_status()

    async def save(self) -> bool:
        if not self.can_save or self.draft is None:
            ui.notify("请先更新合法预览，并检查图示名称。", type="warning")
            return False
        draft, project_id = self.draft, self.project_id
        flowchart_id = self.saved.flowchart_id if self.saved else None
        expected, derived = self.expected_revision, self.derived_from_revision
        self.loading, self.error = True, ""
        self.render_status()
        try:
            saved = await run.io_bound(
                self.repository.save,
                project_id,
                draft,
                flowchart_id=flowchart_id,
                expected_revision=expected,
                derived_from_revision=derived,
            )
        except FlowchartError as error:
            self.error = str(error)
            self.loading = False
            if self.mounted:
                self.render_status()
            return False
        self.loading = False
        # 主题重绘会改变渲染代号，但不会改变保存的文档；按文档上下文接收写入结果。
        if (
            not self.mounted
            or project_id != self.project_id
            or self.draft is not draft
            or flowchart_id != (self.saved.flowchart_id if self.saved else None)
        ):
            return False
        if saved is None:
            self.error = "保存未返回有效版本，请重新加载后核对。"
            self.render_status()
            return False
        self.saved, self.expected_revision, self.derived_from_revision = saved, saved.revision, None
        self.revisions = self.repository.list_revisions(project_id, saved.flowchart_id)
        self.catalog = self.repository.list_latest(project_id)
        self._setting = True
        self.version_select.set_options(
            {item.revision: f"图示 v{item.revision}" for item in self.revisions},
            value=saved.revision,
        )
        self._setting = False
        ui.notify(f"已保存图示 v{saved.revision}", type="positive")
        self.render_catalog()
        self.render_status()
        return True

    def download(self) -> None:
        if not self.can_export or self.saved is None:
            ui.notify("请先保存当前有效预览，或切回已保存版本。", type="warning")
            return
        try:
            root = ElementTree.fromstring(sanitize_svg(self.valid_svg))
            namespace = "{http://www.w3.org/2000/svg}"
            for child in list(root):
                if child.tag in {namespace + "title", namespace + "desc"}:
                    root.remove(child)
            title = ElementTree.Element(namespace + "title")
            title.text = f"{self.saved.name} · 图示 v{self.saved.revision}"
            description = ElementTree.Element(namespace + "desc")
            description.text = json.dumps(
                {
                    "project": self.project_id,
                    "flowchart_id": self.saved.flowchart_id,
                    "revision": self.saved.revision,
                    "description": self.saved.description,
                    "source_hash": source_hash(self.saved.source),
                    "theme": "dark" if self.dark else "light",
                    "purpose": "逻辑图示，不执行设备控制",
                },
                ensure_ascii=False,
            )
            root.insert(0, description)
            root.insert(0, title)
            markup = sanitize_svg(ElementTree.tostring(root, encoding="unicode"))
        except (FlowchartError, ElementTree.ParseError):
            self.error = "SVG导出校验失败，请重新更新预览。"
            self.render_status()
            return
        name = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", self.saved.name).strip("-")[:80] or "flowchart"
        ui.download(markup.encode("utf-8"), f"{name}-v{self.saved.revision}.svg", "image/svg+xml")
