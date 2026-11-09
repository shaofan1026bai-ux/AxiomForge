# 设计证据（2026-11-09）

- 原统计会话：`01a1be0e-d1a5-7ea0-b81f-e63f98e96c1e`，cwd为`/Users/shaofan/Desktop/空调算法`，经trellis mem找回原请求和统计说明。
- 原包：`/Users/shaofan/Desktop/空调算法/航信机场/运行分析/20261109_AHU回风温度范围_1001至1108/`，只引用路径，不复制原始数据与凭据入库。
- `阅读说明.txt`：39日半开区间、1分钟网格、三类点与开机判定、样本比例非时长、缺测不填补。
- `analyze.py`：线性分位数、精确同刻开机筛选、全样本均值和保留无数据的逐日输出。
- `空调AHU—中央空调末端/点位清单.csv`：设备12783～12787，每台airin/v20/v01，共15点身份及单位。
- `src/axiomforge/ui/workbench.py`：航信入口、显隐与项目隔离、独立View、主题及项目切换。
- `src/axiomforge/ui/power_temperature.py`：快照／草稿分离、挂载失效、请求代号；其2分钟／7天和90秒限制不直接移植。
- `src/axiomforge/xiaomai_source.py`及`docs/interfaces/systems/xiaomai/query/history.md`：现有只读认证与分片能力；新专题需独立点位与1分钟合同。
- `.trellis/spec/backend/flights.md`：重启或新增专题须保留各已启用源的联合启动配置。

本轮未访问现场源、未重新统计数据、未修改产品代码。设计参考使用用户提供的39日汇总，预览更换条件不生成假统计。

交互参考位于当前聊天的可视化目录`ahu-temperature-design.html`，无网络请求。静态ID引用及脚本模拟检查涵盖全天39日936小时、每日4小时共156小时、3个跨午夜窗口共24小时及最后实际结束日、相同时刻拒绝、更改条件后不生成假统计。未执行浏览器视觉验收，不将脚本检查称为产品验收。
