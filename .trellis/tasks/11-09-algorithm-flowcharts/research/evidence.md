# 流程图设计证据

## 用户请求与当前上下文

- 用户2026-11-09直接提供Mermaid flowchart TD，要求可像专业流程图查看；确认按“编辑、展示、保存、SVG导出”的首版范围建立任务。
- 原文见同目录`hangxin-ahu.mmd`，9个节点、9条边；T与C为configurable节点。没有提供真实夜间时间、温度值或设备绑定，不能自行填值。
- 以上事实已在当前对话可见，依据trellis-session-insight的不重复当前上下文规则，无需再抽取历史会话。

## 代码证据

- `src/axiomforge/ui/workbench.py`已有algorithms导航与“规则工作区已预留”占位内容，可复用入口，不需新增应用或项目专题路由。
- 工作台现有独立View包含航班、功率温度、AHU回风温度和每日用电占比，项目切换已有dirty/loading确认。新模块必须追加自己的草稿保护，不能重置其他View。
- 本地`.venv/lib/python3.13/site-packages/nicegui/elements/mermaid/mermaid.py`提供ui.mermaid(content, config, on_node_click)。组件会把config传给Mermaid初始化。
- 同目录`mermaid.js`使用打包的nicegui-mermaid，无需CDN；捕获render错误后会把原SVG替换成错误图，直接使用不能满足本任务失败保留旧图的需求。
- 同一JS模块存在共享渲染队列和Mermaid对象；主题和配置应由应用控制，按请求渲染、唯一ID及原子发布，不能信任源码配置覆盖全局设置。
- `.gitignore`已忽略`.nicegui/`；当前没有图示版本库。拟在此建立独立SQLite，由标准库实现，不使用浏览器localStorage保存图示版本。

## 官方能力依据

- [Mermaid Usage](https://mermaid.js.org/config/usage.html)：说明文本描述渲染为SVG、API渲染与绑定能力。实际API兼容性以本地打包版本为准，不直接升级或依赖CDN。
- [Mermaid Flowcharts](https://mermaid.js.org/syntax/flowchart.html)：说明节点、边、中文、方向及样式能力；本任务保留用户的flowchart和classDef写法，首版仅覆盖流程图。

## 实现阶段验证项

- 本地API的解析错误格式、节点ID／类型提取、classDef安全白名单、主题重绘及SVG完整导出。
- 严格模式／禁HTML标签下原样例中文与比较符号≥的显示；不为绑定节点点击而放宽脚本执行。
- SQLite路径、初始化、并发与版本派生；数据库失败必须保留草稿。
- 小型自定义NiceGUI组件的打包依赖、错误保留与画布缩放，不在本轮规划中假称已经实现或通过浏览器验证。

## 当前工作区

2026-11-09开始时已有多个在进行的任务和约70条未提交路径。本任务目前仅写自己的规划目录，不修改产品代码，不提交、回退或归档其他任务。
