"""功率温度只读适配器；原两点与2分钟／7天合同保持不变。"""

import time
from collections.abc import Callable
from datetime import datetime

import httpx

from axiomforge.power_temperature import (
    POINTS,
    PROJECT_ID,
    SHANGHAI,
    QueryRange,
    Snapshot,
    normalize,
)
from axiomforge.xiaomai_client import (
    BASE_URL as BASE_URL,
)
from axiomforge.xiaomai_client import (
    HISTORY_PATH as HISTORY_PATH,
)
from axiomforge.xiaomai_client import (
    LOGIN_PATH as LOGIN_PATH,
)
from axiomforge.xiaomai_client import (
    POINT_PATH as POINT_PATH,
)
from axiomforge.xiaomai_client import (
    PROJECT_PATH as PROJECT_PATH,
)
from axiomforge.xiaomai_client import (
    PUBLIC_KEY as PUBLIC_KEY,
)
from axiomforge.xiaomai_client import (
    Settings as Settings,
)
from axiomforge.xiaomai_client import (
    XiaomaiClient,
    http_client,
)
from axiomforge.xiaomai_client import (
    XiaomaiSourceError as XiaomaiSourceError,
)
from axiomforge.xiaomai_client import (
    encrypt_password as encrypt_password,
)
from axiomforge.xiaomai_client import (
    load_settings as load_settings,
)


def load_power_temperature(
    query: QueryRange,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Snapshot:
    """一轮登录与分段读取全部成功后才发布结果；不自动重试或重定向。"""
    try:
        query.validate_now()
        config = settings or load_settings()
        deadline = clock() + 90
        with http_client(transport) as client:
            source = XiaomaiClient(client, deadline, 90, clock)
            source.authenticate(config, PROJECT_ID)
            source.validate_points(POINTS, PROJECT_ID)
            series: dict[str, list[dict[str, object]]] = {point.point_id: [] for point in POINTS}
            for chunk in query.chunks():
                values = source.history(
                    POINTS,
                    PROJECT_ID,
                    chunk.start.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M"),
                    chunk.end.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M"),
                    2,
                )
                normalize(chunk, values, datetime.now(SHANGHAI))
                for pid, records in values.items():
                    series[pid].extend(records)
            source.check_deadline()
            return normalize(query, series, datetime.now(SHANGHAI))
    except XiaomaiSourceError:
        raise
    except httpx.TimeoutException:
        raise XiaomaiSourceError("小麦请求超时，原快照已保留，请稍后手动刷新。") from None
    except httpx.HTTPError:
        raise XiaomaiSourceError("小麦网络或证书校验失败，原快照已保留。") from None
    except Exception:
        raise XiaomaiSourceError("小麦数据或配置无法解析，原快照已保留。") from None
