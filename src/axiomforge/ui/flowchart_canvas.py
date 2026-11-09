"""独立Mermaid画布，只有服务端核验后的SVG才能替换当前有效图。"""

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nicegui import events, ui


class FlowchartCanvas(
    ui.element,
    component="flowchart_canvas.js",
    esm={"nicegui-mermaid": str(Path(inspect.getfile(ui.mermaid)).parent / "dist")},
):
    """浏览器负责排版与画布交互，项目及版本语义由Python视图持有。"""

    def __init__(
        self,
        *,
        on_rendered: Callable[[events.GenericEventArguments], Any],
        on_error: Callable[[events.GenericEventArguments], Any],
        on_node: Callable[[events.GenericEventArguments], Any],
    ) -> None:
        super().__init__()
        self._props["request"] = None
        self._props["dirty"] = False
        self.on("rendered", on_rendered)
        self.on("render_error", on_error)
        self.on("node_selected", on_node)

    def request_preview(self, source: str, fingerprint: str, ticket: int, dark: bool) -> None:
        self._props["request"] = {
            "source": source,
            "hash": fingerprint,
            "ticket": ticket,
            "dark": dark,
        }
        self.update()

    def invalidate(self, ticket: int) -> None:
        self._props["request"] = {"ticket": ticket}
        self.update()

    def set_dirty(self, value: bool) -> None:
        if self._props["dirty"] == value:
            return
        self._props["dirty"] = value
        self.update()

    def publish(self, ticket: int, svg: str, nodes: list[dict[str, Any]], preserve: bool) -> None:
        self.run_method("publish", ticket, svg, nodes, preserve)

    def select_node(self, node_id: str) -> None:
        self.run_method("selectNode", node_id)
