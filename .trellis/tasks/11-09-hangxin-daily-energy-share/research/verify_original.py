"""从外部只读原包核对产品领域结果；不访问现场、不修改原包。"""
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from axiomforge.energy_share import Conditions, SHANGHAI, normalize, resolve_query

BASE = Path('/Users/shaofan/Desktop/空调算法/航信机场/运行分析')
NOW = datetime(2026, 11, 9, 20, tzinfo=SHANGHAI)


def package(name, start, end):
    raw = json.loads((BASE / name / '采集结果.json').read_text())
    series = {point: [{'ts': ts, 'val': value} for ts, value in values.items()]
              for point, values in raw['series'].items()}
    query = resolve_query(Conditions('custom', start, end), NOW)
    return normalize(query, series, NOW)


snapshot = package('20261109_10月图片电表用电占比', '2026-10-24', '2026-10-27')
assert [d.total for d in snapshot.daily] == [Decimal('2140.80'), Decimal('2513.15'), Decimal('2575.50'), None]
assert snapshot.daily[-1].known_subtotal == Decimal('1655.15')
assert all(i.share is None for i in snapshot.daily[-1].items)
assert snapshot.daily[1].items[1].kwh == 0
assert len(snapshot.included_days) == 3
assert snapshot.complete_days.total == Decimal('7229.45')
for left, right in zip(snapshot.daily, snapshot.daily[1:]):
    for lg, rg in zip(left.items, right.items):
        for lm, rm in zip(lg.meters, rg.meters):
            assert lm.end is rm.start
print('10月24～27日：三天完整日总量、缺测日1655.15与全部空占比、真实0、共同日合计及边界复用通过')

two = package('20261109_图片电表用电占比', '2026-11-07', '2026-11-08')
assert two.interval.total == Decimal('3579.05')
assert all(d.total is None for d in two.daily)
assert two.complete_days is None
print('11月7～8日：区间3579.05可算、两天日占比均不可拆分通过')
