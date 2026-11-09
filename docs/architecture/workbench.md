# Workbench Architecture

Status: architecture proposal with the first NiceGUI style prototype implemented, 2026-11-04. 航信航班只读查询已接入；持久化执行、训练、设备控制和调度仍待实现。See [screen design](workbench-ui.md) and [planning task](../../.trellis/tasks/11-04-nicegui-workbench-design/prd.md).

## Product Boundary

Codex creates and modifies algorithms, model workflows and topic pages in the repository. NiceGUI presents outputs, supported parameter controls, source inspection, experiment comparisons and task management. No embedded AI chat or arbitrary browser code execution is planned.

One application serves three projects. Code and protocol definitions can be shared; connections, device mappings, data and execution records retain project scope.

## Proposed Code Layout

```text
src/axiomforge/
  main.py                 # composition and application lifecycle
  ui/
    shell.py              # navigation and project/environment indicator
    pages/                # six shared modules
    components/           # filters, forms, status and output viewers
    topics/               # project-specific page factories
  domain/                 # typed records and invariants
  services/               # use cases independent of page instances
  repositories/           # SQLite metadata and scoped artifact access
  connectors/             # query/control adapters and target resolution
  algorithms/             # registered Codex-authored Python algorithms
  execution/              # worker supervision, cancellation and logs
  scheduling/             # application-owned scheduler
  config/                 # settings and project/topic registration
tests/
docs/interfaces/          # existing human/AI-readable documentation
```

已实现 `main.py`、`ui/`、独立演示模块 `demo.py`，以及航班领域模块 `flights.py` 和只读适配模块 `flight_source.py`。其余目录仍为提案。航班当前直接调用服务，不新增独立HTTP接口；详见[数据契约](../interfaces/systems/mangshi-flights/query/flights.md)。No separate React frontend or duplicate standalone API service is needed initially.

航信功率与温度专题新增 `power_temperature.py`领域层、`xiaomai_source.py`只读适配层及独立UI／图表模块。每浏览器持有查询快照、选区与状态；服务端使用外部小麦凭据，每次手动查询登录并核验项目和测点后分片读取历史。只有完整成功结果才发布，密码与Token不进入快照或前端；没有新增独立API服务、数据库或控制模块。

算法流程图使用独立领域／存储及UI画布模块，图示以本机SQLite保存不可变版本，默认`.nicegui/algorithm-flowcharts.sqlite3`。这是说明图的持久化，不意味着运行记录、模型或定时任务数据库已经落地。用户草稿和渲染状态每浏览器独立，保存接口验证项目归属和并发基线；Mermaid渲染不访问现场系统。

## Execution Boundaries

Pages call services; services validate scope/parameters and use repositories, connectors and execution supervision. Rendering does not itself issue control calls.

- SQLite holds registrations, parameter snapshots, datasets, experiments, schedules, runs and artifact indexes.
- Project-scoped files hold data snapshots, models, charts and logs outside public static roots.
- Training runs outside the UI event loop. A supervised process supports cancellation and logs; simpler bounded operations may use NiceGUI CPU/I/O helpers.
- One scheduler belongs to application lifecycle and submits through the same service as manual runs. Never create it per page, browser connection or worker import.
- Page refresh timers only refresh displays. Closing a browser leaves application-owned jobs running. Quitting the service stops local scheduling; persisted schedules cannot execute while the computer is off.

## Codex-to-Workbench Handoff

1. Codex reads project/interface documentation and creates a registered Python algorithm or topic.
2. Registration supplies ID, immutable version/source revision, description, parameter schema, input compatibility and declared query/control capabilities.
3. Workbench validates registration and renders parameters, source and accepted output viewers.
4. Submission snapshots project, environment, connection, version, validated parameters, dataset and execution mode.
5. Worker persists structured results/artifacts; UI reads persisted status and presents charts, tables or experiment comparisons.
6. Code updates create new versions. Schedules do not silently follow them. Changed page code may initially require service restart; instant hot reload is not promised.

Markdown documentation is not executable configuration. Runtime calls require validated registrations/adapters; merely adding an interface document never enables control execution.

## Core Records

| Record | Essential fields/relationships |
|---|---|
| Project | stable ID, name, timezone, registered topics |
| Connection | project, ID, system/version, environment, secret reference, target scope |
| Dataset | project, source, immutable snapshot, range, field schema and quality/provenance |
| AlgorithmVersion | ID/version, source revision, parameters, accepted inputs, output kinds, capabilities |
| Run | project/environment/connection, version, parameters, inputs, mode, status, timestamps, logs/artifacts |
| Experiment | project/run, target, factors, preprocessing, split, seed, metrics and fitted artifact |
| Schedule | project, version, parameters, source policy, timezone/trigger, enabled, overlap/missed-run policies |
| ControlAttempt | project/connection, target/command, correlation/idempotency key, acceptance and confirmation |
| Topic | project, topic ID, page factory, compatible algorithms/output references |

Reusable algorithms contain no project credentials. Secret values resolve locally from references and are absent from source viewers, exports and ordinary logs.

## Isolation

- All services/repository lookups require project context and check resource ownership.
- Project switching clears incompatible selections and asks before discarding dirty forms. Each browser has its own context; no process-global mutable current project.
- Workers retain submission context while a browser navigates elsewhere.
- Artifact paths cannot bypass project ownership checks.
- Control routing checks project, environment, connection and device together, even if sites reuse IDs.

## State and Recovery

Run: queued → running → succeeded / failed / cancelled / interrupted. Cancellation first enters cancelling and is final only after stop confirmation. Service restart reconciles stale running records as interrupted or recovery-required, never success.

Schedule enabled/paused is separate from run state. Disabling blocks future submissions, not an active run. Missed-run, retry and overlap policies must be recorded before activation.

Control acceptance and physical confirmation are separate from computation success. An ambiguous timeout leaves a control attempt outcome-unknown and requires reconciliation. Cancellation cannot undo a command already accepted. Generic retry and restart recovery must not replay it.

Historical trials report proposed commands without calling real control adapters. Real control requires separately approved functional scope, known interface constraints and explicit runtime configuration. No live connections are configured here.

## Model Reproducibility

Record data snapshot, target, factors, range/exclusions, split, seed, preprocessing, model version and metrics. Temporal evaluation trains before validation/test windows; preprocessing fits training data only. Compare compatible test sets; feature importance does not establish causality. Model choice awaits a real dataset and target.

## References and Limits

- [NiceGUI foundations](https://nicegui.io/documentation/section_foundations): Python UI, Quasar and Tailwind.
- [NiceGUI documentation](https://nicegui.io/documentation): charts, data elements and CPU/I/O helpers.
- [APScheduler 3.x guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html): durable scheduling concepts; confirm a supported release/API before implementation.

SQLite, process supervision and reliable scheduling are application design proposals, not automatic framework guarantees. Dependency versions, migrations and performance remain unverified until implementation.
