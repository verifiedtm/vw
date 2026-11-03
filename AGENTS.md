# Repository Guidelines

## Project Structure & Module Organization

`vw` is a Python 3.13+ video workbench driven by TOML, CSV, and ffmpeg.
Application code lives in `src/vw/`: `cli.py` exposes commands, `project.py`
handles configuration and safe file operations, and the remaining modules cover
asset ingestion, audio analysis, timeline validation, shot preparation, rendering,
and inspection. Keep CLI and Python callers on the same implementation.
`tests/test_workbench.py` contains integration tests; `tests/conftest.py` generates
media fixtures. `examples/make_demo.py` builds a complete demonstration project.

## Build, Test, and Development Commands

- `nix develop`: enter the shell with Python, ffmpeg/ffprobe, uv, and lint tools.
- `just sync`: install development dependencies from `uv.lock`.
- `just run --help`: explore the CLI; pass other arguments through `just run`.
- `just test`: run pytest; use `just test -k cache` to select tests.
- `just format`: format Python, Nix, and the justfile and sort Python imports.
- `just check`: run all lint, formatting checks, and tests.
- `just build`: produce a wheel and source distribution.
- `just demo /tmp/vw-demo`: generate and render sample media; use a new or empty destination.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions and modules, and
`PascalCase` classes. Follow Ruff formatting with an 88-character line limit;
Ruff also checks import ordering and common correctness issues. Use
`WorkbenchError` for actionable project/media errors. Preserve project-relative
path validation and exact rational frame calculations when changing the pipeline.

## Testing Guidelines

Use pytest with `test_*.py` files and `test_*` functions. Reuse generated fixtures
and temporary directories instead of committing media. Media integration tests
require ffmpeg/ffprobe and skip without them; verify those tests actually run.
Test observable results such as decoded pixels, frame counts, cache reuse, CLI
diagnostics, and preservation of existing files. No coverage percentage is configured.

## Commit & Pull Request Guidelines

Existing history uses short, plain subjects (`initial setup`, `first cut`), with
no established prefix convention. Write focused, descriptive commit subjects.
PRs should explain the behavior changed, link relevant issues, and report checks
run. Include representative frames or diagnostic output for rendering changes.

## Documentation Policy

Keep documentation useful to current users and developers. Do not accumulate
design artifacts or historical implementation narratives in `docs/`; put relevant
design rationale in code comments and Git history. Update `README.md` when CLI
behavior or project-file formats change.
