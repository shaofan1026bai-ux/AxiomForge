"""有界缺测修复及历史日回退的固定预测，实际负荷独立刷新。

平台能量百分比仅作为制冷量的线性代理。输入时间戳表示采样时刻，
没有把它解释为区间平均负荷；本模块不计算累计冷量，也不处理认证。
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Callable
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")


class DailyLoadForecastError(ValueError):
    """配置、时间、历史数据或固定预测缓存不可使用。"""


class HistoryValidationError(DailyLoadForecastError):
    """平台历史数据不符合约定，不能生成完整预测。"""


class ForecastCacheError(DailyLoadForecastError):
    """已有缓存损坏或不属于请求的日期、设备、采样网格。"""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _local_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DailyLoadForecastError("at 必须是带时区的 datetime")
    return value.astimezone(TIMEZONE)


def _day(value: date) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise DailyLoadForecastError("目标日期必须是 date")
    return value


class DailyLoadForecast:
    """fetch_history(start, end) 接受上海时区的左闭右开查询范围。

    该回调只需返回平台原 JSON；认证、刷新和 HTTP 错误处理由调用方负责。
    generate 按日期加进程锁，并以不覆盖的原子创建发布成功缓存。
    response 只查询目标当天实际数据，绝不隐式生成或更新预测。
    """

    def __init__(self, config: dict, cache_dir: Path,
                 fetch_history: Callable[[datetime, datetime], dict]):
        self.config = deepcopy(config)
        self.cache_dir = Path(cache_dir)
        self.fetch_history = fetch_history
        try:
            self.device_id = config["device_id"]
            self.device_name = config["device_name"]
            self.rated = _number(config["rated_cooling_capacity_kw"])
            self.heads = deepcopy(config["heads"])
            self.interval = config.get("interval_minutes", 2)
            self.alpha = _number(config.get("smoothing_alpha", 0.85))
            self.adjustment = _number(config.get("adjustment_factor", 0.985))
            self.lookback = config.get('reference_lookback_days',7)
            self.minimum_coverage = _number(config.get('minimum_reference_coverage',0.95))
            self.maximum_gap_minutes = config.get('maximum_interpolation_gap_minutes',10)
            identifiers = [head["identifier_name"] for head in self.heads]
            capacities = [_number(head["rated_cooling_capacity_kw"]) for head in self.heads]
            point_ids = [head["point_id"] for head in self.heads]
        except (KeyError, TypeError) as exc:
            raise DailyLoadForecastError("机组配置缺少必需字段") from exc
        if (isinstance(self.device_id, bool) or not isinstance(self.device_id, int)
                or not isinstance(self.device_name, str) or not self.device_name
                or not isinstance(self.heads, list) or not self.heads
                or any(not isinstance(item, str) or not item for item in identifiers + point_ids)
                or len(set(identifiers)) != len(identifiers)
                or len(set(point_ids)) != len(point_ids)):
            raise DailyLoadForecastError("设备或机头点位配置无效")
        if (self.rated is None or self.rated <= 0
                or any(capacity is None or capacity <= 0 for capacity in capacities)
                or not math.isclose(math.fsum(capacities), self.rated, rel_tol=1e-9, abs_tol=1e-6)):
            raise DailyLoadForecastError("各机头额定制冷量必须为正且合计等于整机额定量")
        if (isinstance(self.interval, bool) or not isinstance(self.interval, int)
                or self.interval <= 0 or 1440 % self.interval):
            raise DailyLoadForecastError("采样间隔必须为能够整除1440的正整数分钟")
        if (self.alpha is None or not 0 < self.alpha <= 1
                or self.adjustment is None or self.adjustment <= 0
                or not math.isfinite(self.rated * self.adjustment)):
            raise DailyLoadForecastError("平滑或调整参数无效")
        if not callable(fetch_history):
            raise DailyLoadForecastError("fetch_history 必须可调用")
        if (type(self.lookback)is not int or not 1<=self.lookback<=30
                or self.minimum_coverage is None or not 0.8<=self.minimum_coverage<=1
                or type(self.maximum_gap_minutes)is not int or not 0<=self.maximum_gap_minutes<=60):
            raise DailyLoadForecastError('历史回退天数、最小覆盖率或短缺口上限无效')

    def _bounds(self, day: date):
        start = datetime.combine(_day(day), time.min, tzinfo=TIMEZONE)
        return start, start + timedelta(days=1)

    def _grid(self, day: date):
        start, _ = self._bounds(day)
        return [start + timedelta(minutes=index * self.interval)
                for index in range(1440 // self.interval)]

    def _path(self, day: date):
        return self.cache_dir / f"{_day(day).isoformat()}.json"

    @staticmethod
    def _source_time(value):
        try:
            if not isinstance(value, str):
                raise ValueError
            stamp = datetime.fromisoformat(value)
            return stamp.replace(tzinfo=TIMEZONE) if stamp.tzinfo is None else stamp.astimezone(TIMEZONE)
        except (ValueError, TypeError, OverflowError) as exc:
            raise HistoryValidationError("历史数据含无法解析的采样时间") from exc

    def _loads(self, payload: dict, day: date, require_complete: bool):
        if (not isinstance(payload, dict) or payload.get("success") is not True
                or not isinstance(payload.get("data"), list)):
            raise HistoryValidationError("历史接口未成功返回 data 数组")
        grid = self._grid(day)
        expected = set(grid)
        traces = {}
        flags = Counter()
        for head in self.heads:
            identifier = head["identifier_name"]
            matches = [series for series in payload["data"]
                       if isinstance(series, dict)
                       and series.get("deviceId") == self.device_id
                       and series.get("identifierName") == identifier]
            if len(matches) != 1:
                raise HistoryValidationError(f"点位 {identifier} 必须有且仅有一条设备序列")
            series = matches[0]
            if series.get("pointId") != head["point_id"] or series.get("unit") != "%":
                raise HistoryValidationError(f"点位 {identifier} 的 pointId 或单位不符")
            rows = series.get("pointDataList")
            if not isinstance(rows, list):
                raise HistoryValidationError(f"点位 {identifier} 缺少 pointDataList 数组")
            trace = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise HistoryValidationError(f"点位 {identifier} 含无效采样记录")
                stamp = self._source_time(row.get("ts"))
                if stamp not in expected:
                    raise HistoryValidationError(f"点位 {identifier} 的采样时间越界或不在采样网格上")
                if stamp in trace:
                    raise HistoryValidationError(f"点位 {identifier} 含重复采样时间")
                raw = row.get("val")
                value = _number(raw)
                flag = None
                if raw is None:
                    flag = "missing_value"
                elif value is None:
                    flag = "invalid_number"
                elif not 0 <= value <= 100:
                    flag = "out_of_range"
                if flag:
                    flags[f"{flag}:{identifier}"] += 1
                    if require_complete:
                        raise HistoryValidationError(f"点位 {identifier} 存在缺测、非有限数或越界百分数")
                    value = None
                trace[stamp] = value
            missing = expected - trace.keys()
            if missing:
                flags[f"missing_timestamp:{identifier}"] += len(missing)
                if require_complete:
                    raise HistoryValidationError(f"点位 {identifier} 未覆盖完整参考日")
            traces[identifier] = trace
        loads = []
        for stamp in grid:
            values = [(traces[head["identifier_name"]].get(stamp), head) for head in self.heads]
            if any(value is None for value, _ in values):
                loads.append(None)
            else:
                loads.append(round(math.fsum(
                    head["rated_cooling_capacity_kw"] * value / 100 for value, head in values
                ), 6))
        return loads, {"expected_sample_count": len(grid),
                       "valid_sample_count": sum(value is not None for value in loads),
                       "quality_flag_counts": dict(flags)}

    def _prepare_reference(self,loads,quality):
        """只修复被有效样本两侧夹住的短缺口；原始实际数据不进入此修复路径。"""
        observed=sum(v is not None for v in loads)
        quality={**quality,'observed_sample_count':observed,'coverage_ratio':observed/len(loads),
                 'interpolated_sample_count':0,'interpolation_ranges':[]}
        if observed/len(loads)<self.minimum_coverage:
            return None,quality,'有效采样覆盖率低于参考日门槛'
        repaired=list(loads);index=0;ranges=[]
        while index<len(repaired):
            if repaired[index] is not None:index+=1;continue
            start=index
            while index<len(repaired) and repaired[index] is None:index+=1
            size=index-start
            if start==0 or index==len(repaired):
                return None,quality,'参考日边缘缺测，缺少双侧样本，未外推'
            if size*self.interval>self.maximum_gap_minutes:
                return None,quality,'连续缺口超过短缺口修复上限'
            left,right=repaired[start-1],repaired[index]
            for n in range(size):repaired[start+n]=round(left+(right-left)*(n+1)/(size+1),6)
            ranges.append({'start_index':start,'sample_count':size,'missing_minutes':size*self.interval})
        quality.update(interpolated_sample_count=sum(r['sample_count'] for r in ranges),
                       interpolation_ranges=ranges,prediction_sample_count=len(repaired),
                       repair_method='bounded_linear_interpolation' if ranges else 'none')
        return repaired,quality,None

    def read_forecast(self, day: date) -> dict | None:
        """缓存不存在返回 None；损坏、错日期或错设备显式报错。"""
        path = self._path(day)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ForecastCacheError("预测缓存无法读取") from exc
        try:
            snapshot = json.loads(text)
            if not isinstance(snapshot,dict):raise ValueError
            version=snapshot.get('version')
            reference=date.fromisoformat(snapshot['reference_date'])
            age=(_day(day)-reference).days
            limit=1 if version==1 else snapshot.get('reference_policy',{}).get('lookback_days')
            if (type(limit)is not int or not 1<=limit<=30 or not 1<=age<=limit
                    or version==2 and snapshot.get('fallback_used')is not (age>1)):
                raise ValueError
            if (type(version)is not int or version not in (1,2)
                    or snapshot.get("status") != "success"
                    or snapshot.get("date") != day.isoformat()
                    or snapshot.get("timezone") != "Asia/Shanghai"
                    or snapshot.get("scope") != "single_chiller"
                    or snapshot.get("device_id") != self.device_id
                    or snapshot.get("source") != ('previous_day_baseline' if age==1 else 'historical_day_baseline')
                    or snapshot.get("model_trained") is not False
                    or snapshot.get("interval_minutes") != self.interval
                    or snapshot.get("time_semantics") != "sample_time"
                    or snapshot.get("load_unit") != "kW"):
                raise ValueError
            generated_at = datetime.fromisoformat(snapshot["generated_at"])
            if generated_at.tzinfo is None or generated_at.utcoffset() is None:
                raise ValueError
            if generated_at.astimezone(TIMEZONE) < self._bounds(day)[0]:
                raise ValueError
            parameters = snapshot["parameters"]
            alpha = _number(parameters.get("smoothing_alpha"))
            adjustment = _number(parameters.get("adjustment_factor"))
            rated = _number(snapshot["rated_cooling_capacity_kw"])
            if (alpha is None or not 0 < alpha <= 1 or adjustment is None or adjustment <= 0
                    or rated is None or rated <= 0):
                raise ValueError
            grid = self._grid(day)
            points = snapshot["points"]
            if not isinstance(points, list) or len(points) != len(grid):
                raise ValueError
            for point, stamp in zip(points, grid):
                value = _number(point.get("predicted_load_kw"))
                if point.get("time") != stamp.isoformat() or value is None or value < 0:
                    raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            raise ForecastCacheError("预测缓存损坏，或日期、设备、采样网格不符") from exc
        return snapshot

    def _publish(self, day: date, snapshot: dict) -> dict:
        """先落完整临时文件，再用硬链接排他发布，绝不覆盖成功文件。"""
        descriptor, temporary = tempfile.mkstemp(prefix=f".{day.isoformat()}-", suffix=".tmp", dir=self.cache_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, self._path(day))
            except FileExistsError:
                existing = self.read_forecast(day)
                if existing is None:
                    raise ForecastCacheError("预测缓存发布期间发生冲突")
                return existing
            return snapshot
        finally:
            Path(temporary).unlink(missing_ok=True)

    def generate(self, target_date: date, at: datetime) -> dict:
        """优先前日，短缺口有界插值，超限向前回退；已有成功缓存不覆盖。"""
        local_at = _local_time(at)
        day = _day(target_date)
        reference_date = day - timedelta(days=1)
        _, reference_end = self._bounds(reference_date)
        if reference_end > local_at:
            raise DailyLoadForecastError("参考日尚未结束，不能生成该日预测")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.cache_dir / f".{day.isoformat()}.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                existing = self.read_forecast(day)
                if existing is not None:
                    return existing
                attempts=[];loads=None
                for offset in range(1,self.lookback+1):
                    reference_date=day-timedelta(days=offset)
                    start,end=self._bounds(reference_date)
                    try:
                        raw,quality=self._loads(self.fetch_history(start,end),reference_date,require_complete=False)
                        prepared,quality,reason=self._prepare_reference(raw,quality)
                    except HistoryValidationError as error:
                        attempts.append({'date':reference_date.isoformat(),'status':'rejected','reason':str(error)})
                        continue
                    attempts.append({'date':reference_date.isoformat(),'status':'selected' if prepared is not None else 'rejected',
                                     'reason':reason,'quality':quality})
                    if prepared is not None:loads=prepared;break
                if loads is None:
                    error=HistoryValidationError(f'近{self.lookback}天没有满足覆盖率及缺口限制的参考日，未生成预测')
                    error.reference_attempts=attempts
                    raise error
                previous = None
                points = []
                for stamp, value in zip(self._grid(day), loads):
                    previous = value if previous is None else self.alpha * value + (1 - self.alpha) * previous
                    points.append({"time": stamp.isoformat(),
                                   "predicted_load_kw": round(previous * self.adjustment, 6)})
                snapshot = {
                    "version": 2, "status": "success", "date": day.isoformat(),
                    "timezone": "Asia/Shanghai", "scope": "single_chiller",
                    "device_id": self.device_id, "device_name": self.device_name,
                    "source": "previous_day_baseline" if offset==1 else "historical_day_baseline", "model_trained": False,
                    "fallback_used": offset>1,
                    "reference_policy":{'lookback_days':self.lookback,'minimum_coverage':self.minimum_coverage,
                                        'maximum_interpolation_gap_minutes':self.maximum_gap_minutes},
                    "reference_attempts":attempts,
                    "generated_at": local_at.isoformat(), "reference_date": reference_date.isoformat(),
                    "reference_start": start.isoformat(), "reference_end_exclusive": end.isoformat(),
                    "interval_minutes": self.interval, "time_semantics": "sample_time", "load_unit": "kW",
                    "rated_cooling_capacity_kw": self.rated,
                    "heads": [{"identifier_name": head["identifier_name"], "point_id": head["point_id"],
                               "rated_cooling_capacity_kw": head["rated_cooling_capacity_kw"]} for head in self.heads],
                    "parameters": {"smoothing_alpha": self.alpha, "adjustment_factor": self.adjustment},
                    "reference_quality": quality,
                    "method_notes": [
                        "优先前日形状基线；覆盖率合格且有双侧样本的短缺口允许线性估计，否则向前回退到最近合格日。",
                        "能量百分比按配置的机头额定制冷量线性换算，为估算制冷负荷。",
                        "调整因子为预设参数，未经预测误差校准；不是训练得到的天气预测模型。",
                        "成功快照全天固定，展示时的实际数据不会反向修改预测。",
                    ],
                    "points": points,
                }
                return self._publish(day, snapshot)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def response(self, at: datetime, target_date: date | None = None) -> dict:
        """仅查询目标当日实际数据一次，并叠加既有固定预测。"""
        local_at = _local_time(at)
        day = _day(target_date) if target_date is not None else local_at.date()
        if day > local_at.date():
            raise DailyLoadForecastError("不能查询未来日期的实际负荷")
        forecast = self.read_forecast(day)
        start, end = self._bounds(day)
        loads, quality = self._loads(self.fetch_history(start, end), day, require_complete=False)
        points = []
        for index, (stamp, load) in enumerate(zip(self._grid(day), loads)):
            points.append({
                "time": stamp.isoformat(),
                "actual_load_kw": load if stamp <= local_at else None,
                "predicted_load_kw": forecast["points"][index]["predicted_load_kw"] if forecast else None,
            })
        quality["visible_valid_sample_count"] = sum(point["actual_load_kw"] is not None for point in points)
        return {
            "date": day.isoformat(), "timezone": "Asia/Shanghai", "scope": "single_chiller",
            "device_id": self.device_id, "device_name": self.device_name, "source": "live_api",
            "interval_minutes": self.interval, "time_semantics": "sample_time",
            "load_unit": "kW", "energy_unit": "kWh",
            "actual_energy_kwh": None, "predicted_energy_kwh": None,
            "energy_method": "not_computed_from_point_samples", "energy_coverage": None,
            "energy_note": "接口值仅确认是采样时刻值，当前未计算区间积分或累计冷量。",
            "actual_method": "linear_compressor_energy_proxy",
            "actual_quality": quality,
            "forecast_status": "ready" if forecast else "missing",
            "forecast_metadata": ({
                "generated_at": forecast["generated_at"], "reference_date": forecast["reference_date"],
                "source": forecast["source"], "model_trained": False,
                "parameters": forecast["parameters"], "fixed_snapshot": True,
                "rated_cooling_capacity_kw": forecast["rated_cooling_capacity_kw"],
                "fallback_used":forecast.get('fallback_used',False),
                "reference_quality":forecast.get('reference_quality'),
                "reference_attempts":forecast.get('reference_attempts',[]),
                "degraded":forecast.get('fallback_used',False) or forecast.get('reference_quality',{}).get('interpolated_sample_count',0)>0,
            } if forecast else None),
            "points": points,
        }
