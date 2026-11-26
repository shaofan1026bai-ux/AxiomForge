# 航信策略与任务管理

2026-11-26 从航信服务器 `/opt/task-manager` 导入。线上由 `task-manager.service` 以单个 Uvicorn worker 运行 `app:app`，使用 Python 3.10。FastAPI 提供策略管理、天气和负荷预测接口。APScheduler 在同一进程内加载任务脚本及日负荷预测作业，按北京时间调度。

## 源码入口

- [app.py](app.py) 提供业务 API，管理 AI 总开关、任务选择、子进程生命周期和运行时长。
- [tasks.json](tasks.json) 保存任务定义和初始开关。[scripts/](scripts/) 保留生产调度入口，根目录同名模块实现策略，`algorithms/` 保存配套算法。
- [forecast_runtime.py](forecast_runtime.py) 独立调度日负荷预测，每天 00:05 生成。它使用 [load_forecast_config.json](load_forecast_config.json)，与设备策略总开关独立。
- [deploy/task-manager.service](deploy/task-manager.service) 记录线上 systemd 入口及路径。它取用服务器的外部配置 [task-manager.env.example](deploy/task-manager.env.example) 所列字段。航班任务还需外部 `/etc/task-manager-flight.env`，字段见 [task-manager-flight.env.example](deploy/task-manager-flight.env.example)。

## 调度定义

| 任务 ID | 配置时段 | 入口 |
| --- | --- | --- |
| `chiller_start_stop` | 08:00–17:00 整点 | [启动预测](scripts/hangxin_start_forecast.py) |
| `chiller_temperature` | 11:00–21:00 每半小时 | [出水温度调节](scripts/hangxin_hourly_temperature.py) |
| `ahu_start_stop` | 每 60 秒 | [AHU 启停预览](scripts/hangxin_ahu_start_policy.py) |
| `ahu_temperature` | 每 10 秒 | [演示脚本](scripts/demo.py) |
| `flight_linked_control` | 18:00–20:30 每半小时 | [航班天气联动](scripts/hangxin_flight_shutdown.py) |
| `ahu_fresh_air_schedule` | 每 5 分钟 | [两段新风联动](scripts/hangxin_second_fresh_schedule.py) |
| `vrv_schedule` | 每天 00:00 | [VRV 零点关机](scripts/hangxin_vrv_daily_shutdown.py) |

核对时线上 AI 总开关关闭，七项设备策略均未生效，独立日负荷预测启用。运行开关与累计时长以线上 SQLite 状态和 API 为准。仓库使用关闭的设备策略初始值，并将独立日负荷配置的 `enabled` 设为 `false`。

部分调度脚本会写入现场设备。线上使用原有服务的隔离环境、外部凭据和状态目录。开机预测还使用服务器已有的 `algorithms/hangxin_start_forecast_mean/model.json`，其中包含现场历史样本，作为部署资产单独管理。线上状态库、凭据、缓存、现场回放数据及已过期的一次性作业留在原部署。

## 本地验证

在本目录使用单独的 Python 环境。测试使用模拟平台、合成模型及临时状态目录，可在断网环境运行。

```sh
uv venv --no-project --python 3.10
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt -r requirements-flight.txt pytest
.venv/bin/python -m pytest -q .
```
