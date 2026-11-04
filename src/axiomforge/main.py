"""Start the local NiceGUI workbench."""

import argparse

from nicegui import ui

from axiomforge.ui.workbench import create_page


def main() -> None:
    parser = argparse.ArgumentParser(description="AxiomForge local workbench")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    ui.page("/")(create_page)
    ui.run(
        host="127.0.0.1",
        port=args.port,
        title="AxiomForge · 算法工作台",
        reload=False,
        show=False,
        language="zh-CN",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
