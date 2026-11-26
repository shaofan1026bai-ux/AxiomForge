"""演示脚本：模拟一次计算，不访问设备或下发控制。"""

import time
from datetime import datetime


print(f"{datetime.now().isoformat()} 计算开始", flush=True)
time.sleep(5)
print("计算完成，示例控制值：24.0", flush=True)
