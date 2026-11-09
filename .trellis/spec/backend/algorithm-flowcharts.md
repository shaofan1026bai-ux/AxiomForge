# 算法流程图与版本存储契约

## 1. 范围与触发

新增或修改`algorithm_flowcharts.py`、`flowchart_store.py`、种子资源及相关SVG导出边界前阅读。图示是说明资料，不能变成运行、控制或动态Python执行入口。

## 2. 公开签名

- `FlowchartDraft(name, description, source, annotations)`：待保存内容。
- `FlowchartRevision(flowchart_id, project_id, revision, name, description, source, annotations, content_hash, created_at, derived_from_revision)`：不可变版本视图。
- `source_hash(source) -> str`：预览与源码一致性的指纹，不等同于全部版本内容指纹。
- `validate_source(source) -> None`、`validate_draft(draft) -> None`：基本类型、范围、功能子集校验，不宣称完整替代Mermaid解析器。
- `sanitize_svg(svg: str) -> str`：服务端导出边界的SVG检查及规范化。
- `FlowchartStore(path=None)`：默认本机运行目录，测试使用临时库。
- `list_latest(project_id)`、`get(project_id, flowchart_id, revision=None)`、`list_revisions(project_id, flowchart_id)`。
- `save(project_id, draft, *, flowchart_id=None, expected_revision=None, derived_from_revision=None) -> FlowchartRevision`。
- 业务错误`FlowchartError`，并发冲突`FlowchartConflictError`，都可显示脱敏说明。

## 3. 契约

SQLite默认路径`.nicegui/algorithm-flowcharts.sqlite3`，可由`AXIOMFORGE_FLOWCHART_DB`指定。只保存图示及版本，不混入凭据或现场数据；不覆盖其他模块数据库。初始化、种子和保存必须使用事务。

每个Store读取与写入接口都携带并校验项目，项目来自既有工作台登记。图示随机ID不能替代归属校验；跨项目、非法ID、无效版本不得返回其他项目内容。

旧版本不可原地更新。保存先核对`expected_revision`，冲突保留调用方草稿；相同内容不制造空版本。`derived_from_revision`指向同图历史版本，不得借此引用其他图或项目。

种子只属于航信，来源为应用内资源，保留用户的原始9节点9边及T／C样式。首次导入幂等，后续启动不覆盖用户已保存内容，也不把种子复制到华师大或大润发。

Mermaid源码上限64KiB，浏览器解析上限100节点／200边，SVG上限2MiB，允许flowchart／graph子集，不接受脚本、HTML、链接、初始化或任意外部资源。classDef只支持必要节点样式；不能仅靠源码字符串过滤宣称SVG安全。浏览器负责真正语法解析，保存还必须验证预览指纹与当前草稿一致。

SVG解析有体积限制；禁止DTD／实体、脚本／事件属性、foreignObject、外链及活动内容，保留安全内部fragment引用、文本、节点、路径与markers。导出不使用屏幕裁切，完整viewBox和元数据必须对应所选已保存版本。

## 4. 校验与错误矩阵

| 条件 | 行为 |
|---|---|
| 空名称、超长源码、不支持的类型／指令 | 可显示的校验错误，不写库 |
| 跨项目读取或保存 | 拒绝，不回退为另一项目或默认图 |
| 基线不是最新版本 | 冲突错误，不改写任何版本 |
| 内容与最新相同 | 返回已有版本，不增加空版本 |
| SQLite写失败、格式不兼容 | 脱敏错误，不吞掉或假报保存成功 |
| SVG含活动内容或外部资源 | 拒绝导出，不执行、不透传 |
| 原始Mermaid语法错误 | 由UI保留旧有效预览，不能冒用旧图保存新稿 |

## 5. 基础、良好与错误场景

基础：航信读种子，修改名称／源码，预览后保存v2；重新打开Store可读相同版本。

良好：两个客户端持有v2，第一次保存到v3，第二次使用旧基线时收到冲突并保留草稿；历史v1仍可读取和派生。

错误：只在目录列表按项目过滤，但按ID读取或保存时不验证项目；初始化每次用种子覆盖已修改v1；以客户端传来的SVG代替服务器边界检查。

## 6. 必要测试

临时库覆盖持久化、幂等种子、不可变历史、相同内容、并发事务、派生与跨项目拒绝、磁盘错误脱敏。源码覆盖用户原始classDef与正常注释、不支持类型、危险内容和超限。SVG覆盖完整安全Mermaid输出及恶意标签／属性／URL／实体，不能仅测一个简单rect就声称兼容流程图。

## 7. 错误与正确示例

错误：`get(flowchart_id)`依赖前端当前目录，其他项目同ID路径可绕过过滤。

正确：`get(project_id, flowchart_id, revision)`在Store中校验归属。

错误：收到预览成功事件就允许保存，未核对事件所属源码和选择上下文。

正确：只有当前项目、图ID、请求代号和源码指纹全部匹配时接受预览，再用该草稿内容创建新版本。
