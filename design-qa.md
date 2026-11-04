# Research Canvas Style Prototype QA

final result: passed

Scope: visual acceptance of the authorized local NiceGUI style prototype. This does not certify later live-data, control, training or scheduling features.

## Source and Browser Evidence

- Source truth: `docs/architecture/assets/research-canvas-light.png` and `research-canvas-dark.png`.
- Actual UI: `http://127.0.0.1:8080/`, NiceGUI 3.19.0.
- Final screenshots: [light](docs/architecture/previews/light-reviewed.jpg), [dark](docs/architecture/previews/dark-reviewed.jpg).
- Responsive screenshots: [light 1280](docs/architecture/previews/light-1280-reviewed.jpg), [dark 1280](docs/architecture/previews/dark-1280-reviewed.jpg).
- Full comparisons: [light](docs/architecture/previews/comparison-light.png), [dark](docs/architecture/previews/comparison-dark.png).
- Focused comparisons: [light navigation/type](docs/architecture/previews/comparison-light-navigation.png), [dark navigation/type](docs/architecture/previews/comparison-dark-navigation.png).

Source dimensions: 1487 × 1058 raster pixels. Comparison copies normalize to 1440 × 1024. Actual browser screenshots are 1440 × 1024 pixels at a 1440 × 1024 CSS viewport, effectively 1:1 density. Separate responsive checks use 1280 × 800. Sources are unchanged; Pillow only prepares internal comparison copies.

State: selected power/temperature topic for 大润发东环店, synthetic data, default date/device and applicable theme. Dark final includes three genuinely executed demo runs; light final shows the honest initial no-run state. Dates/values are deterministic synthetic fixtures, while run timestamps reflect actual clicks. Source mock values and historical fake versions are not reproduced as operational facts. Browser JPEG compression differs from the source PNG and limits fine glyph comparison.

## Comparison History

1. First render: [light-first](docs/architecture/previews/light-first.jpg), [dark-second](docs/architecture/previews/dark-second.jpg), [combined before](docs/architecture/previews/comparison-before.png).
   - P2: Quasar primary helper overrode muted navigation/tab colors, causing low dark-theme contrast. Fixed by `color=None` and semantic CSS colors.
   - P2: Drawer default padding compounded shell padding; the selected topic wrapped and the bottom label was cramped. Fixed by zero outer drawer padding, explicit non-wrapping content and one icon gap.
   - P2: Chart legends/axis labels were crowded and comparison surfaces were shorter than the source. Fixed axis-name placement, clear dual scales, vertical grid and chart height.
   - Interaction blocker: notification after content replacement accessed a deleted slot. Moved notification before render; repeated run actions now work without console errors.
   - Theme initialization: added the JavaScript top-level return and disconnected-client guard. Explicit dark selection now survives reload.
2. Final render: reviewed full dark and focused light comparisons at normalized equal size; final light/dark and 1280 screenshots verify the fixes. No actionable P0/P1/P2 visual findings remain.

## Required Fidelity Surfaces

| Surface | Result |
|---|---|
| Fonts / typography | System sans-serif with Chinese fallbacks, 30px primary heading, 15px navigation and compact secondary data text. Hierarchy and wrapping are clear. Slightly denser than the generated mock; acceptable for the working local tool. |
| Spacing / layout | Shared header/sidebar, four horizontal tabs, filters, dominant time series, supporting scatter and lower history preserve the chosen layout. Additional provenance copy and real table fields are intentional. |
| Colors / tokens | Light neutral surfaces and indigo accent; dark slate surfaces and readable labels/controls/charts. Success uses icon and text as well as green. |
| Assets / sharpness | Real ECharts plots and standard Material UI icons, no rasterized UI or handmade SVG stand-ins. Generic cube icon is a provisional library mark, not a finalized brand asset. JPEG screenshot softness is capture-related. |
| Copy / content | Chinese navigation and tabs follow the selected concept. Synthetic provenance is explicit. Empty integration pages do not imply real connections or running schedules. |

## Interaction Checks

- All six module entries, settings and return-to-topic navigation.
- All three project options; airport/library device sets and empty records differ from the original project. Returning restores session records.
- Dirty-filter switching: cancel restores original project; confirm resets incompatible filters/parameters.
- Parameter edits: 30-minute sampling yields 336 points versus 168 at one hour; disabling temperature hides the dependent scatter.
- Source inspection shows the actual known Python generator; no browser code submission.
- Changed filters disable export and label the old results until recomputed.
- Light/dark switching preserves displayed run rows. Explicit dark preference survives browser reload.
- Keyboard Tab can reach export; visible focus is present.
- Page width equals viewport width at 1280 × 800; persistent controls remain available. Long content scrolls vertically.
- Final browser warning/error logs: none for checked interactions.
- Ruff lint/format, mypy and 5 pytest tests: passed.

## Gaps and Follow-up Polish

- Browser download testing was declined by the browser permission gate. No repeat or workaround was attempted. CSV generation, project scope and provenance are unit-tested; actual browser file saving remains unverified.
- OS preference changes, chart zoom preservation, separate browser profiles and exhaustive keyboard/screen-reader behavior were not fully exercised.
- P3: Replace the provisional library brand icon if a real brand asset is supplied; refine outlined icons and exact mock typography if desired.
- P3: The source's decorative timeline and fictional model versions were replaced with a simpler real-session table. Future durable history can add richer presentation.

## Implementation Checklist

- [x] Resolve P0/P1/P2 visual/interaction issues found in the style pass.
- [x] Capture and compare both themes and responsive states.
- [x] Keep the loopback preview available with reproducible startup instructions.
- [x] Report the download permission limitation and pending integrations explicitly.
