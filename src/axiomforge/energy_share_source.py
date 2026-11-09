"""13块物理电表的只读历史适配；完整成功后一次发布，不落盘原始读数。"""

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import httpx

from axiomforge.energy_share import (
    POINTS,
    PROJECT_ID,
    SHANGHAI,
    Query,
    Snapshot,
    normalize,
    resolve_query,
)
from axiomforge.xiaomai_client import (
    Settings,
    XiaomaiClient,
    XiaomaiSourceError,
    http_client,
    load_settings,
)

TOTAL_SECONDS = 900
PADDING = timedelta(minutes=10)


class EnergySourceError(ValueError):
    """可展示的脱敏源错误，不含凭据或响应正文。"""


class QueryCancelled(EnergySourceError):
    """停止后续片和结果发布；不能撤回已发送的上游请求。"""


@dataclass(frozen=True, slots=True)
class Progress:
    completed: int
    total: int
    label: str
    elapsed: float


def query_chunks(query: Query, now: datetime) -> tuple[tuple[datetime, datetime], ...]:
    """缓冲只提供边界支撑；连续片最长24小时，绝不向未来取数。"""
    end = min(query.end + PADDING, now.astimezone(SHANGHAI).replace(second=0, microsecond=0))
    earliest = datetime.min.replace(tzinfo=SHANGHAI)
    start = query.start - min(PADDING, query.start - earliest)
    chunks = []
    while start < end:
        finish = min(start + timedelta(days=1), end)
        chunks.append((start, finish))
        start = finish
    return tuple(chunks)


def load_energy_share(
    query: Query,
    *,
    progress: Callable[[Progress], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Snapshot:
    """每片同时读取13累计点、1分钟网格，保留完整序列检查观测回退。"""
    started = clock()

    def check_cancelled() -> None:
        if cancelled and cancelled():
            raise QueryCancelled("已停止等待，本次未发布新的统计结果。")

    try:
        check_cancelled()
        now = datetime.now(SHANGHAI)
        # 相对预设提交后已被冻结，跨午夜继续读取原提交范围。
        checked = resolve_query(replace(query.conditions, period="custom"), now)
        if (checked.days, checked.start, checked.end) != (query.days, query.start, query.end):
            raise EnergySourceError("统计窗口与查询条件不一致，请重新提交。")
        chunks = query_chunks(query, now)

        def report(completed: int, label: str) -> None:
            if progress:
                progress(Progress(completed, len(chunks), label, clock() - started))

        config = settings or load_settings()
        with http_client(transport) as client:
            source = XiaomaiClient(client, started + TOTAL_SECONDS, TOTAL_SECONDS, clock)
            report(0, "正在校验项目与13个累计电量测点")
            source.authenticate(config, PROJECT_ID)
            check_cancelled()
            source.validate_points(POINTS, PROJECT_ID)
            values: dict[str, list[dict[str, object]]] = {point.point_id: [] for point in POINTS}
            for index, (start, end) in enumerate(chunks):
                check_cancelled()
                report(index, f"正在读取第{index + 1}/{len(chunks)}片（13个累计点）")
                part = source.history(
                    POINTS,
                    PROJECT_ID,
                    start.strftime("%Y-%m-%d %H:%M"),
                    end.strftime("%Y-%m-%d %H:%M"),
                    1,
                )
                check_cancelled()
                for point in POINTS:
                    # 不在此去重，接缝同刻异值必须由领域层显式识别冲突。
                    values[point.point_id].extend(part.get(point.point_id, ()))
                report(index + 1, f"已完成第{index + 1}/{len(chunks)}片")
            check_cancelled()
            source.check_deadline()
            result = normalize(query, values, datetime.now(SHANGHAI))
            check_cancelled()
            source.check_deadline()
            return result
    except EnergySourceError:
        raise
    except XiaomaiSourceError as error:
        raise EnergySourceError(str(error)) from None
    except ValueError as error:
        # 只展示本领域受控校验文案；非受控解析异常统一脱敏。
        if str(error).startswith(("统计窗口", "只能统计", "起止日期", "日期格式", "电量历史时间")):
            raise EnergySourceError(str(error)) from None
        raise EnergySourceError("电表历史数据或查询条件无法解析，旧快照已保留。") from None
    except httpx.TimeoutException:
        raise EnergySourceError("电表历史请求超时，旧快照已保留，请稍后手动重试。") from None
    except httpx.HTTPError:
        raise EnergySourceError("电表数据源网络或证书校验失败，旧快照已保留。") from None
    except Exception:
        raise EnergySourceError("电表历史数据或配置无法解析，旧快照已保留。") from None
