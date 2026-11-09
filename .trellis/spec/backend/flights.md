# 航班只读数据契约

## 1. 范围

修改 `flights.py`、`flight_source.py` 或 `ui/flights.py` 前阅读。适用于航信LUM数据源，不是通用航空协议。

## 2. 签名

- `load_flights(day: date) -> FlightSnapshot`
- `normalize_flights(rows, fetched_at) -> FlightSnapshot`
- `flight_gaps(flights, day, mode, minimum) -> list[FlightGap]`
- `axiomforge --flight-env-file PATH`
- 固定SQL与字段定义见 `docs/interfaces/systems/mangshi-flights/query/flights.md`。

## 3. 契约

`AXIOMFORGE_FLIGHT_ENV_FILE`或启动参数指定外部文件；`SURREALDB_*`环境值优先。关闭dotenv插值，不提交配置、不打印凭据。SQL只读、参数绑定本场和三个计划日，无ts或limit裁剪。日期字符串格式来自实库验证。

启动其他真实专题时仍须保留航班配置：`--xiaomai-env-file`与`--flight-env-file`是独立入口，不能互相替代。预览服务重启或交付启动文件时必须合并已启用数据源的参数，并实查各入口；只验证新专题会遗漏既有专题的运行配置。

进港用Arrv、出港用Dpt；ts既不是更新时间，也不是出港本场时刻。多版本取最新后处理删除；共享号匹配完整范围，缺主号保留并警示。状态优先使用本场实际时间。空档必须使用完整数据集和邻日事件，相同时刻成组，禁止用列表过滤后的数据计算空档。动态=实际→预计→计划。首末无数据不推断为空档。

## 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 无配置、认证缺失 | 脱敏FlightSourceError，无请求 |
| 查询错误或返回非列表 | 脱敏错误，页面保留旧快照 |
| 成功空数组 | 空列表，不伪造数据 |
| 缺计划时刻 | 航班保留，空档完整性警示 |
| 方向／本场不匹配 | 排除且警示 |
| 找不到共享主号 | 有效记录保留，警示 |
| 切换日期未查询 | 原日期快照及提示 |
| 关闭页面／换项目时请求完成 | 仅更新当前浏览器对象，不渲染已删除容器 |

## 5. 基础／良好／错误情形

基础：手动查当天，双栏显示79条验证快照。良好：补查邻日航班打断跨夜空档、搜索只改变列表。错误：将ts当本场起飞时刻，或认为空档可直接停机。

## 6. 必要检查

Ruff、mypy、pytest；领域测试覆盖最新删除、共享范围、非营运、状态方向、时区、同刻分组、邻日分割、动态优先级。连接器测试验证固定SELECT及错误脱敏。浏览器核对主题、阈值、搜索、分页和项目入口。

## 7. 错误与正确示例

错误：`flight_gaps(filtered_arrivals, ...)`，出港被漏掉后制造长空档。

正确：`flight_gaps(snapshot.flights, selected_day, mode, minimum)`，只在双栏表格应用搜索和方向筛选。

## 8. 国内／国际区域（2026-11-05）

- `Flight.region`：domestic/international/unknown，当前LUM样本fcategory字符串0→domestic、1→international，其余保留unknown。这是样本核对映射，正式字典待确认，不能根据fltAttr或城市名猜测分类。
- `FLIGHT_AREAS`：all、domestic:I、domestic:O、international:I、international:O。
- `flights_for_area(flights, area, include_unknown=False)`提供唯一集合投影，area非法时ValueError。
- `flight_gaps(..., area='all')`保留旧全场行为；指定区域时，仅使用该属性与方向的完整邻日航班。unknown在同方向作为保守阻断，不归入任何已知区域统计；UI独立标识。
- 原“不可对过滤结果计算间隔”约束针对搜索／状态／列表方向，明确业务区域分组属于计算契约。
- 回归测试验证0/1/未知映射、四区域互不干扰、同方向未知阻断、空区域无外推间隔。
