# NiceGUI Personal Algorithm Workbench

## Goal

Design a local workbench for inspecting and operating algorithms developed through Codex. Deliver plans and visual concepts before application implementation.

## Confirmed Background

- NiceGUI is selected. Codex owns AI conversations and code changes; the browser presents results, parameter forms and inspectable code.
- Shared navigation/base modules coexist with project-specific topic pages.
- The user selected displayed visual concept 3, Research Canvas, and requires light/dark theme selection.
- Projects: 大润发东环店 (`rt-mart-donghuan`), 航信机场 (`hangxin-airport`), 华师大图书馆 (`ecnu-library`).
- Repository evidence: `README.md`, `docs/interfaces/README.md`, and `docs/interfaces/projects/README.md`. No application package or dependency manifest exists; frontend/backend guidelines are bootstrap placeholders.

## Requirements

| ID | Requirement |
|---|---|
| R1 | Shared shell with project selection, explicit environment and project-topic navigation. |
| R2 | Query/control interface management with project connections and device/point mappings. |
| R3 | Registered algorithm descriptions, parameters, source inspection and historical trials; no embedded AI chat. |
| R4 | Historical analysis and model experiments with factor selection, repeatable evaluation and comparisons. |
| R5 | Application-owned schedules; distinguish enabling/disabling from current-run cancellation. |
| R6 | Explicit project scope for datasets, models, algorithms, topics, schedules and runs. |
| R7 | Three independent visual concepts with demo data, followed by selection before UI implementation. |
| R8 | Incremental implementation plan; no package installation, server or live-control calls during this design task. |
| R9 | Light/dark themes cover the entire interface, charts, tables and code views while preserving interaction and run state. |

## Acceptance Criteria

- AC1 (R1, R7): Three visual concepts expose common navigation, result space and parameter placement for review.
- AC2 (R2, R6): Design explains shared protocols, project-specific connections/device mappings and control-target resolution.
- AC3 (R3): Codex handoff specifies registration, parameter schema, immutable source version, trial execution and outputs.
- AC4 (R4): Experiment design captures factors, data snapshot, split and evaluation metrics rather than equating training score with accuracy.
- AC5 (R5): Schedule, run and control-confirmation states are separate, including restart and ambiguous outcomes.
- AC6 (R6): Project switching rejects stale forms/results; workers retain original run context.
- AC7 (R8): PRD, design and implementation plan are reviewable; no application code is created before visual selection.
- AC8 (R9): Theme switching preserves project/filters/tabs/zoom and jobs, survives reload, and provides readable chart/UI surfaces in both modes.

## Out of Scope

Embedded AI chat, prompt-to-code in the browser, SaaS accounts, cloud hosting, automatic production control, a browser IDE, drag-and-drop workflows, and remote training clusters.

## Open Product Decisions

1. Production startup behavior remains deferred; the approved style prototype opens the selected analysis topic. The refined theme selector includes system-following mode.
2. Before later functional milestones: first real topic/dataset, model target, schedule missed-run/concurrency policy, and real-control scope. These do not block visual exploration.

## Artifact Status

The user approved the refined concept and explicitly requested style implementation. Authorized scope: Milestone 1 local NiceGUI shell, light/dark/system themes, three project selectors, navigable module previews and a synthetic analysis topic. Later live data, training and scheduling milestones remain unapproved. The prototype opens the selected topic as its review surface; production startup behavior remains deferred.
