# Proposed Implementation Plan

Status: Milestone 1 style implementation was explicitly authorized and is complete. The prototype opens the selected topic for review. Production startup behavior and feature-specific choices remain deferred; later milestones are not activated.

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
