"""由TaskManager在北京时间8:00至17:00整点预测，低温末轮延至次日12点的入口。"""

from pathlib import Path
import sys

# 受管脚本的工作目录是scripts，加入项目根目录以复用控制器及认证客户端。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hangxin_start_forecast import main


if __name__ == "__main__":
    raise SystemExit(main(["--execute"]))
