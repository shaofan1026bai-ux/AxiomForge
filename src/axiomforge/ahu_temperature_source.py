"""AHU多点逐窗口只读取数；全部成功后发布快照，不持久化凭据。"""

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

import httpx

from axiomforge.ahu_temperature import (
    POINTS,
    PROJECT_ID,
    SHANGHAI,
    Query,
    Snapshot,
    combine_snapshots,
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


class AhuSourceError(ValueError):
    """供页面显示的脱敏错误，不含响应、账号或令牌。"""


class QueryCancelled(AhuSourceError):
    """仅停止等待与后续分片，不声称撤回已发送的请求。"""


@dataclass(frozen=True, slots=True)
class Progress:
    completed: int
    total: int
    label: str
    elapsed: float


def load_ahu_temperature(
    query: Query,
    *,
    progress: Callable[[Progress], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Snapshot:
    """每个开始日期一个窗口，15点同时读取，1分钟网格保持与原分析一致。"""
    started = clock()

    def check_cancelled() -> None:
        if cancelled and cancelled():
            raise QueryCancelled("已停止等待，本次未发布新的统计结果。")

    def report(completed: int, label: str) -> None:
        if progress:
            progress(Progress(completed, len(query.windows), label, clock() - started))

    try:
        check_cancelled()
        # UI冻结的窗口必须再次在源边界校验，不能绕过领域构造直接请求未来。
        if not query.windows or query.end > datetime.now(SHANGHAI):
            raise AhuSourceError("最后统计窗口尚未结束，请调整日期或每日时段。")
        # Query可由调用方直接构造，不能让错误窗口绕过62日、每日时段和分钟校验。
        frozen_conditions = replace(query.conditions, period="custom")
        if resolve_query(frozen_conditions).windows != query.windows:
            raise AhuSourceError("统计窗口与查询条件不一致，请重新提交查询。")
        config = settings or load_settings()
        with http_client(transport) as client:
            source = XiaomaiClient(client, started + TOTAL_SECONDS, TOTAL_SECONDS, clock)
            report(0, "正在校验项目与15个测点")
            source.authenticate(config, PROJECT_ID)
            check_cancelled()
            source.validate_points(POINTS, PROJECT_ID)
            parts = []
            for i, window in enumerate(query.windows):
                check_cancelled()
                report(i, f"正在读取{window.day.isoformat()}的15个测点")
                values = source.history(
                    POINTS,
                    PROJECT_ID,
                    window.start.strftime("%Y-%m-%d %H:%M"),
                    window.end.strftime("%Y-%m-%d %H:%M"),
                    1,
                )
                check_cancelled()
                part = normalize(Query(query.conditions, (window,)), values, datetime.now(SHANGHAI))
                parts.append(part)
                report(i + 1, f"已完成{window.day.isoformat()}")
            check_cancelled()
            source.check_deadline()
            result = combine_snapshots(query, parts, datetime.now(SHANGHAI))
            check_cancelled()
            source.check_deadline()
            return result
    except AhuSourceError:
        raise
    except XiaomaiSourceError as error:
        raise AhuSourceError(str(error)) from None
    except httpx.TimeoutException:
        raise AhuSourceError("AHU历史请求超时，旧快照已保留，请稍后手动重试。") from None
    except httpx.HTTPError:
        raise AhuSourceError("AHU数据源网络或证书校验失败，旧快照已保留。") from None
    except Exception:
        raise AhuSourceError("AHU历史数据或配置无法解析，旧快照已保留。") from None
