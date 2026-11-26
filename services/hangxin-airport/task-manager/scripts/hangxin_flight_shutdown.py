"""晚间每半小时根据真实航班和逐小时天气预测调整关机时间表。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hangxin_flight_shutdown import main

if __name__ == "__main__":
    raise SystemExit(main(["--execute"]))
