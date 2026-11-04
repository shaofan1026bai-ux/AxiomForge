"""Deterministic synthetic data. This module never accesses site equipment."""

import csv
import io
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    short_name: str
    description: str
    devices: tuple[str, ...]
    offset: float


PROJECTS = (
    Project(
        "rt-mart-donghuan",
        "大润发东环店",
        "东环店",
        "商业建筑 · 设备运行分析",
        ("冷机一号", "冷机二号", "冷冻水泵一号"),
        0,
    ),
    Project(
        "hangxin-airport",
        "航信机场",
        "航信机场",
        "交通建筑 · 设备运行分析",
        ("空调机组一号", "空调机组二号", "循环水泵一号"),
        16,
    ),
    Project(
        "ecnu-library",
        "华师大图书馆",
        "图书馆",
        "公共建筑 · 设备运行分析",
        ("主机一号", "主机二号", "末端机组一号"),
        -12,
    ),
)
PROJECT_BY_ID = {project.id: project for project in PROJECTS}
DEMO_END = date(2026, 11, 4)
DEMO_START = DEMO_END - timedelta(days=6)


@dataclass(frozen=True)
class Parameters:
    interval: int = 60
    temperature: bool = True
    threshold: float = 120


@dataclass(frozen=True)
class Sample:
    time: datetime
    power: float
    temperature: float


def generate_samples(
    project_id: str,
    device: str,
    start: date,
    end: date,
    params: Parameters,
) -> list[Sample]:
    """Generate reproducible, project-scoped fixtures for a bounded date range."""
    project = PROJECT_BY_ID[project_id]
    if device not in project.devices:
        raise ValueError("所选设备不属于当前项目")
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    if (end - start).days > 30:
        raise ValueError("演示分析最多选择 31 天")
    if params.interval not in (15, 30, 60, 120):
        raise ValueError("不支持的采样间隔")
    if not 0 <= params.threshold <= 300:
        raise ValueError("功率参考阈值应在 0–300 kW 之间")
    device_offset = project.devices.index(device) * 7
    first = datetime.combine(start, datetime.min.time())
    count = ((end - start).days + 1) * 1440 // params.interval
    samples = []
    for i in range(count):
        moment = first + timedelta(minutes=i * params.interval)
        hour = moment.hour + moment.minute / 60
        phase = (hour - 7) * math.pi / 12
        day = (moment.date() - DEMO_START).days
        temperature = 17 + 4.7 * math.sin(phase) + math.sin(day * 0.8)
        ripple = 2 * math.sin(i * 2.7) + 1.3 * math.cos(i * 1.3)
        power = 99 + project.offset + device_offset + 23 * math.sin(phase) + ripple
        samples.append(Sample(moment, round(power, 2), round(temperature, 2)))
    return samples


def export_csv(project_id: str, device: str, samples: list[Sample]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["project_id", "device", "time_Asia_Shanghai", "power_kW", "temperature_C", "source"]
    )
    for sample in samples:
        writer.writerow(
            [
                project_id,
                device,
                sample.time.isoformat(),
                sample.power,
                sample.temperature,
                "synthetic_demo",
            ]
        )
    return output.getvalue().encode("utf-8-sig")
