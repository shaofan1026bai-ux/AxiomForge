# AxiomForge
A personal AI-powered workbench for building rules, training models, and orchestrating algorithms.

## Run Locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run axiomforge
```

Open http://127.0.0.1:8080 in your browser. To use another port:

```bash
uv run axiomforge --port 8081
```

The service listens only on loopback. Stop it with Ctrl+C. Theme preferences persist in browser local storage; demo filters, parameters and run records are per client and reset on page reload. No credential is required.

## Current Scope

The first NiceGUI style prototype implements the selected Research Canvas layout, light/dark/system themes, three project contexts, module navigation and an interactive synthetic power/temperature analysis. Date/device filters, parameter adjustments, analysis records, source inspection and CSV export work locally.

All equipment names and measurements shown in the application are examples. Model training, real query/control connections and application-owned schedules are not implemented; their pages explicitly show pending integration. Codex remains the place to create and change algorithms.

## Development Checks

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/axiomforge
uv run pytest
```

## Design References

- [Architecture and execution boundaries](docs/architecture/workbench.md)
- [Modules and screen design](docs/architecture/workbench-ui.md)
- [Interface documentation guide](docs/interfaces/README.md)
- [Project documentation](docs/interfaces/projects/README.md)
- [Planning task and acceptance criteria](.trellis/tasks/11-04-nicegui-workbench-design/prd.md)
