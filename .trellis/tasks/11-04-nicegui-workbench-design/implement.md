# Proposed Implementation Plan

Status: Milestone 1 style implementation was explicitly authorized and is complete. 用户于 2026-11-04 追加授权项目概览入口与最近项目缓存：首次概览选择，之后恢复上次项目分析页。后续现场接入等里程碑尚未启动。

## 项目入口与缓存补充

- [x] 未选择项目时展示概览，为各项目增加进入按钮。
- [x] 概览和顶部切换复用确认、项目设备重置与偏好保存逻辑。
- [x] 从浏览器本地存储校验并恢复项目 ID，缓存缺失或异常时回退概览。
- [x] 代码检查与 5 项现有测试通过；浏览器验证首次概览、图书馆卡片进入与刷新恢复、参数草稿取消切换后缓存不变、顶部切换机场并刷新恢复、概览当前项目标记。
- [ ] 未知缓存 ID、存储读写异常和不同浏览器之间的隔离尚未执行浏览器故障注入验证；实现已包含校验与异常捕获。

## 1. Local Shell and Read-only Demo

- [x] Read trellis-before-dev and relevant guidelines before coding.
- [x] Select visual direction: concept 3, Research Canvas, with light/dark themes.
- [x] Review dark-theme refinement; confirm base modules. Use the selected topic as the style-review landing page.
- [x] Create a NiceGUI Python package, locked dependencies, loopback startup and instructions.
- [x] Build shared shell, three project entries and a labelled synthetic topic. General topic registration remains a later extension.
- [x] Add per-browser persistent light/dark/system selection; theme UI and chart tokens together.
- [x] Implement one topic with chart filters, parameter form, source inspection and result display.
- [x] Review 1440x1024 and 1280x800, keyboard focus, empty states and dirty-form project switching.
- [x] Verify theme reload, menus, readable chart/UI surfaces and record preservation. OS-change event, chart zoom preservation and separate-browser profiles are implemented but not exhaustively browser-tested.

Validation: Ruff check/format and mypy pass; 5 pytest tests pass. Core page interactions were inspected in the in-app browser. Browser download validation was declined by the permission gate; CSV generation/provenance tests pass. See repository-root design-qa.md for visual evidence and test gaps.

## 2. Read-only Data and Rule Trials

- [ ] Choose a real source and confirm schema, time/units/quality semantics.
- [ ] Add SQLite metadata, scoped files, query adapters and dataset snapshots.
- [ ] Register a Codex-authored algorithm with typed parameters and immutable source version.
- [ ] Support historical trials, honest logs and reproducible outputs; control remains simulated.
- [ ] Verify isolation, invalid parameters, missing data, worker failure and artifact reconstruction.

## 3. Model Experiments

- [ ] Agree on a target, factors, split, baseline and comparison metric.
- [ ] Execute training outside the UI event loop with constrained concurrency.
- [ ] Persist preprocessing, splits, seeds, factors, metrics and model artifacts.
- [ ] Verify no future-data leakage; compare compatible held-out evaluations.

## 4. Scheduling and Optional Real Control

- [ ] Agree on timezone, missed-run, overlap, retry and shutdown policies.
- [ ] Pin a supported APScheduler version and create one application-owned scheduler.
- [ ] Bind schedules to versions and parameter snapshots; distinguish disable from cancel.
- [ ] Verify restart, duplicate startup, browser disconnect, sleep/resume and cancellation.
- [ ] If approved scope includes real control, test target resolution, preconditions, idempotency and result confirmation against a test connector.
- [ ] Ensure ambiguous control outcomes cannot be blindly retried or replayed after restart.

## Validation and Rollback

Exact validation commands depend on the future package configuration. Planned checks: formatting/linting, type checks, meaningful domain/repository tests, worker integration and browser review. No application tests are claimed now.

Keep each milestone scoped. Back up SQLite before schema changes; prefer additive migrations. Disable schedules before rollback, retain logs/input snapshots, and never replay incomplete control commands automatically.

Design checks: local Markdown links, artifact completeness, requirement coverage and explicit pending decisions. No server exists for browser QA yet.
