# 每日用电占比领域合同

## 1. 范围／触发

修改`energy_share.py`、`energy_share_source.py`及相关导出前阅读本合同和[接口口径](../../../docs/interfaces/systems/xiaomai/query/energy-share.md)。只读累计电量，不调用设备控制。八项统计边界固定，不等于机场总量。

## 2. 签名

- `Conditions(period, start_date, end_date)`、`resolve_query(conditions, now=None) -> Query`，日期含端点，Query保存解析后的days/start/end。
- `POINTS`为13块物理表，`GROUPS`为8个不重复分组。
- `Snapshot`保存query、queried_at、daily、interval、complete_days、included_days/excluded_days及boundaries。
- `Aggregate`中total、GroupResult.kwh/share允许为空；known_subtotal不能冒充total。
- `load_energy_share(query, *, progress=None, cancelled=None, ...) -> Snapshot`复用小麦认证／项目／点身份校验。
- `export_daily/export_summary/export_intervals/export_quality/export_notes(snapshot) -> bytes`。

## 3. 数据合同

北京时间完整自然日，最多62日。每次查询13个累计kWh点的完整返回序列，1分钟网格、≤24小时分片，端点最多10分钟额外支撑。只认证一次，总截止、取消和分片检查覆盖认证至快照生成。上游失败抛出脱敏异常，不能变成空记录；成功无值属于缺测。

唯一边界对象复用到相邻日和区间。精确读数优先，缺失时两侧有效支撑总间隔≤300秒才插值。Decimal保留内部精度。精确非法值和冲突不能绕开插值，回退不能裁为0；只使受影响日和区间无效，后续干净日期仍可算。

任一项未知：全部share和total为空，其余kwh保留。所有项已知且total=0：share仍为空。complete_days使用所有8项同一天可计算的共同日期集合，先求kWh和再求share。interval独立按首末边界计算，不被中间日界缺失自动否决。观察不到重置不证明缺口内未重置。

## 4. 验证与错误矩阵

| 条件 | 结果 |
|---|---|
| 未来／倒序／超过62日 | 取数前拒绝 |
| 单侧支撑或跨距>300秒 | 边界缺失 |
| 同刻异值／非法精确值 | 冲突／非法，不用远值掩盖 |
| 观察到负读数／回退 | 受影响增量不可算 |
| 缺少一个AHU子表 | AHU总表未知；全部8项占比未知 |
| 完整真实零日 | total=0，share为空 |
| 请求失败／取消 | 不发布部分快照 |

## 5. 好／基本／坏案例

基本：按午夜累计差得到当日电量。好：11月7～8日interval=3579.05，但中间日界缺失导致两天daily total为空，二者同时保留。坏：把月度4.3kWh缺口均分到每日，或将已知7项归一化。

## 6. 必须验证

域测试覆盖≤5分钟界限、边界复用、异常／重复／回退、真实零、共同日期集合、跨月日期和两种区间口径；源测试验证13点身份、未来裁剪、相邻分片、失败与取消。原包离线参考：10月24～26日total为2140.80、2513.15、2575.50；27日七项小计1655.15且全部share为空。

## 7. 错误与正确

错误：`share = known_kwh / sum(known_items)`在缺项时仍返回100%。正确：先判断8项全部可算且total>0，再计算所有share；否则share均为空，显示已知小计及原因。
