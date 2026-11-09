"""Start the local NiceGUI workbench."""

import argparse
import os

from nicegui import ui

from axiomforge.ui.workbench import create_page


def main() -> None:
    parser = argparse.ArgumentParser(description="AxiomForge local workbench")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--flight-env-file", help="航班只读数据源的外部配置文件路径")
    parser.add_argument("--xiaomai-env-file", help="小麦只读数据源的外部配置文件路径")
    args = parser.parse_args()
    if args.flight_env_file:
        os.environ["AXIOMFORGE_FLIGHT_ENV_FILE"] = args.flight_env_file
    if args.xiaomai_env_file:
        os.environ["AXIOMFORGE_XIAOMAI_ENV_FILE"] = args.xiaomai_env_file
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
