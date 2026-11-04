# NiceGUI Workbench Contract

## 1. Scope / Trigger

Applies to `src/axiomforge/main.py`, `ui/`, and `demo.py`. The current app is a local style prototype with synthetic data, not a device controller or scheduler. NiceGUI is the selected implementation framework; do not introduce a React scaffold.

## 2. Signatures

- Startup: `uv run axiomforge [--port PORT]`, default loopback `127.0.0.1:8080`.
- `generate_samples(project_id: str, device: str, start: date, end: date, params: Parameters) -> list[Sample]`.
- `export_csv(project_id: str, device: str, samples: list[Sample]) -> bytes`.
- `chart_options(samples, params, dark, *, scatter=False) -> dict[str, Any]` creates fresh options per client.
- `create_page()` creates one `Workbench` instance per browser client.

## 3. Contracts

- Project IDs come from `demo.PROJECTS`; device selection must belong to that project.
- `Parameters`: interval in minutes (15/30/60/120), temperature factor boolean, threshold 0–300 kW.
- Dates are inclusive calendar dates; samples run from start midnight through the end day at interval boundaries. Display/export timezone is Asia/Shanghai.
- Synthetic measurements are deterministic. CSV contains scope, units and `synthetic_demo` provenance, encoded UTF-8 with BOM.
- UI state and session records live in the client-owned Workbench. No global mutable current project or global chart theme.
- Browser-local `axiomforge.theme` stores `light`, `dark` or `system`; only this non-sensitive preference persists. System mode listens to OS preference changes.
- CSS owns semantic surface/text tokens; ECharts owns plot/axis/tooltip colors. Theme updates merge options and do not reset data zoom or recompute samples.
- Parameter drafts persist across tabs. Project switch confirms before discarding drafts. Changed filters mark results stale and disable CSV export until recomputed.
- Static code inspection uses a known repository function; never evaluate browser-submitted Python.
- NiceGUI 3.19 puts Quasar `!important` helpers in CSS layers. Use `color=None` on custom-colored navigation/tab buttons rather than fighting `text-primary` with unlayered CSS. Explicitly remove drawer default padding when the inner shell owns spacing.
- `ui.run_javascript` needs a top-level `return` when Python awaits a value. Guard a missing result when the browser disconnects during initialization.
- Rebuilding content deletes the original event slot. Send notifications before clearing that slot or use an explicitly live client/container context afterwards.

## 4. Validation and Error Matrix

| Input/event | Result |
|---|---|
| Foreign-project device | ValueError; UI warns, no result replacement |
| Reversed dates or range over 31 days | ValueError; UI warns |
| Empty/invalid date or parameter | UI warns and retains prior result |
| Unsupported interval/out-of-range threshold | ValueError |
| Project switch with unapplied changes | Confirmation; cancel restores previous project selection |
| Filter changes before update | Stale message and disabled export |
| Invalid persisted theme | Resolve to system mode |
| localStorage unavailable | Catch storage failure; current-session theme still works |

## 5. Good / Base / Bad Cases

- Base: load synthetic topic, adjust device/date, update, inspect and export.
- Good: switch theme while zoomed; preserve result and zoom, update plot labels/tooltips to readable colors.
- Bad: select a device from a different project, label old data with new filters, or portray a placeholder schedule as running.

## 6. Required Verification

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/axiomforge
uv run pytest
```

`tests/test_demo.py` verifies reproducibility/range counts, project-device isolation, invalid ranges and CSV provenance. Browser checks cover theme reload, project switching/cancel, parameters, record isolation, export, code/empty modules and 1440x1024 / 1280x800 layouts. Save visual evidence and report concrete gaps.

## 7. Wrong vs Correct

Wrong: assign `current_project` at module scope or mutate a global chart palette. One client's interaction then leaks to others.

Correct: create a Workbench per `@ui.page` request, capture all state there, and return fresh chart options using its resolved theme.

Wrong: export currently visible samples using newly edited device/date metadata.

Correct: block export while filters are stale; only export after validated recomputation.
