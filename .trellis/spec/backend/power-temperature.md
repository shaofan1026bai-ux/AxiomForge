# 航信功率与室外温度只读契约

## 1. 范围与触发

修改 `power_temperature.py`、`xiaomai_source.py`、相关启动配置或真实分析图表前阅读。仅适用于已登记的航信小麦环境及用户指定的一组测点，不将这里的编号套用到其他部署。

## 2. 公开签名

- `QueryRange(start: datetime, end: datetime)`：有时区、分钟精度、左闭右开、最长7天；`validate_now()`排除未来结束时间。
- `parse_range(start: str, end: str) -> QueryRange`：浏览器不带时区的输入按 `Asia/Shanghai`解释。
- `normalize(query, series, queried_at) -> Snapshot`：将两条原始历史序列归一化为完整2分钟网格。
- `statistics(rows) -> Statistics`：样本均值／峰值、温度范围、配对比例及Pearson。
- `export_snapshot(snapshot: Snapshot) -> bytes`：完整快照CSV，不接受图表选区或表单草稿。
- `load_power_temperature(query: QueryRange) -> Snapshot`：每次手动查询独立登录与取数，成功后发布完整快照。
- 启动入口：`axiomforge --xiaomai-env-file PATH`，或设置 `AXIOMFORGE_XIAOMAI_ENV_FILE`。

## 3. 数据、认证和配置契约

领域模块拥有唯一测点登记：项目832，功率设备12744／`wh171k_mspm03_totalpower`／kW；温度设备12788／`nfdh9q_mscrk01_c21`／℃。UI、源与导出必须引用同一登记，不复制名称搜索、ID或单位推断逻辑。

外部配置使用 `XIAOMAI_ACCOUNT`、`XIAOMAI_PASSWORD`、`XIAOMAI_LOGIN_URL`；公开公钥可由 `XIAOMAI_PUBLIC_KEY`覆盖。dotenv关闭插值，显式进程环境优先。凭据仅服务端使用，不能进入Snapshot、浏览器、本地偏好、CSV、源码视图及普通日志。

固定航信HTTPS域名与端口36443。密码使用已有公钥的RSA/PKCS#1 v1.5 + Base64，与既有小麦协议兼容。Token来自登录响应头 `Access-Token`，之后用Bearer；只在本次查询生命周期使用，不缓存或落盘。实际公司／用户ID从登录响应获取，随后检查授权项目与两设备测点归属。TLS验证开启，使用系统证书信任；禁止凭据跨源重定向。

历史查询同时请求两条测点，固定 `interval=2`，最长24小时一段。完整查询最长7天；所有片成功才组装快照，不能展示部分请求成功的结果。输入范围包含分钟精度，返回点按精确时间网格配对，不按近邻时间或响应顺序拼接。

真实0保留；缺失、无效值、非有限值都不能变成0。相同时间相同值去重、冲突值失效并标明冲突。缺失时间在网格上保留空值。功率统计只要求功率有效，温度范围只要求温度有效，散点及r要求同时间两个值都有效。配对分母为期望网格点数，不是已有返回点数。

CSV编码UTF-8 BOM，包含单位、现场时区、查询区间、查询时刻、来源、设备／测点及每行质量标记；导出完整成功快照，不随缩放或选区删减。

## 4. 校验与错误矩阵

| 条件 | 行为 |
|---|---|
| 无凭据、错误域名／端口、公钥无效 | 不提交错误目标请求，脱敏XiaomaiSourceError |
| 未来、逆序、超过7天、非分钟范围 | 本地拒绝；不登录 |
| 登录无Token、失败信封、无项目832权限 | 不读取业务历史，不默认为空成功 |
| 测点项目／设备／单位不符 | 拒绝整次结果，不按数组位置猜测 |
| 任一片失败、超时／截止、非法响应结构 | 脱敏错误，UI保留旧快照 |
| 成功空序列／单序列缺失 | 形成明确的空／部分数据快照，不制造数值 |
| 全段时间不兼容 | 协议错误，不能归为正常空数据 |
| 有效配对少于3、任一序列恒定 | r不可计算并说明原因，不返回0冒充弱相关 |

## 5. 基础、良好与错误场景

基础：查询今天，展示两条真实序列与可核对的摘要。

良好：七日分片后去重，在缺失仍存在的完整网格上计算配对比例，选中时段只过滤分析输入。

错误：把上游 `cycle=0`当作2分钟采样周期、把设备最新值标为历史区间当前值、用日用电量替代功率，或由r推断节能空间。

## 6. 必要测试及断言

- 领域：真实0、缺失、乱序、冲突重复、非法／越界／非网格时间；网格分母、边界和分段不重不漏。
- 统计：完全正／负相关，少于3点、恒定序列、两序列独立缺失和首次峰值时间。
- 来源：假上游验证RSA可用测试密钥解密、Token头及Bearer、授权项目、映射校验、重定向／异常／截止与日志脱敏。自动测试不得读真实账号或请求生产环境。
- CSV：来源与范围来自快照，缺失空单元格和状态保留，包含完整查询数据。
- 项目规定的Ruff、mypy、pytest及真实只读短时／分段核对；UI验收另见前端规范。

## 7. 错误与正确示例

错误：`valid_pairs / len(received_pairs)`，所有不完整记录被先删掉，使完整性永远显示100%。

正确：`statistics(snapshot.selected(selection)).pairs / statistics(snapshot.selected(selection)).expected`，分子与分母来自同一选中网格范围。

错误：`export_snapshot(new_draft, visible_rows)`，旧数据被贴上新范围且随缩放丢行。

正确：`export_snapshot(snapshot)`，仅在查询已成功且无待更新草稿时允许导出。
