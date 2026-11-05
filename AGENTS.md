# Repository Guidelines

## Project Structure & Module Organization

`vw` is a Python 3.13+ video workbench driven by TOML, CSV, and ffmpeg.
Application code lives in `src/vw/`: `cli.py` exposes commands, `project.py`
handles configuration and safe file operations, and the remaining modules cover
asset ingestion, audio analysis, timeline validation, shot preparation, rendering,
and inspection. `catalogue.py` manages project-independent clip libraries.
Keep CLI and Python callers on the same implementation.
`tests/test_workbench.py` contains integration tests; `tests/conftest.py` generates
media fixtures. `examples/make_demo.py` builds a complete demonstration project.

## Build, Test, and Development Commands

- `nix develop`: enter the shell with Python, ffmpeg/ffprobe, uv, and lint tools.
- `just sync`: install development dependencies from `uv.lock`.
- `just run --help`: explore the CLI; pass other arguments through `just run`.
- `just test`: run the rapid unit/smoke suite; use `just test -k cache` to select tests.
- `just test-all`: include the complete media integration suite.
- `just format`: format Python, Nix, and the justfile and sort Python imports.
- `just check`: run lint, formatting checks, and the rapid test suite.
- `just check-all`: include every media integration test before merging rendering changes.
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
require ffmpeg/ffprobe and skip without them; run `just test-all` and verify those
tests actually run. The default suite keeps two small real-media smoke tests.
Tests using `media` (including through `project`) are automatically marked
`integration` unless marked `smoke`; mark other expensive media cases explicitly.
Test observable results such as decoded pixels, frame counts, cache reuse, CLI
diagnostics, and preservation of existing files. No coverage percentage is configured.

CI runs `just check-all` in the locked Nix development shell on pull requests and
pushes to `master`. The standalone performance benchmark is separate from pytest:

```sh
uv run python examples/benchmark_frames.py /tmp/vw-frame-benchmark
```

Use a new directory. See the script's docstring and `--help` for measurement
phases and larger-source options. Compare decoded work and cache behavior as well
as elapsed time; OS page caches are not cleared.

## Pipeline Invariants

Source-frame indices address decoded presentation order. Use actual timestamps
and `Fraction` for duration/cadence conversion, rounding only at output boundaries.
Keep `auto` verified seeking and `exact` decode-from-start equivalent in decoded
frame selection. Keyframe flags are seek hints; preserve checksum verification
and the exact fallback. Cache keys must account for every input that changes pixels.

`Project.path` confines ordinary paths to the project root. Only explicit asset
`source_mode=reference` rows may resolve external media; do not generalize that
exception to animations, arbitrary command paths or generated outputs. Preserve
original media, CSV annotations and the previous successful export on failure.

Static preparation, animation and composition are separate stages. Preserve alpha
through prepared shots, apply coverage once, and keep preview placement consistent
with final-canvas geometry. Details belong with their implementations:
`timeline.py` (timing), `frame_index.py` (seeking), `normalize.py` (shot preparation),
`animation.py` (keyframes), `presentation.py` (fit), `geometry.py` (rasterization),
and `render.py` (composition). Diagnostic event contracts live in `metrics.py`.

## Commit & Pull Request Guidelines

Existing history uses short, plain subjects (`initial setup`, `first cut`), with
no established prefix convention. Write focused, descriptive commit subjects.
PRs should explain the behavior changed, link relevant issues, and report checks
run. Include representative frames or diagnostic output for rendering changes.

## Documentation Policy

`README.md` is a brief overview of what vw is and what it is for, with pointers
to `USAGE.md` for setup and usage. Keep it short and welcoming. Requirements,
installation steps, commands, demos and technical examples belong in
`USAGE.md`, not the README.

The README is not an exhaustive command reference, project-file schema, pipeline
specification, troubleshooting log or implementation history. Put operational
recipes, file-authoring rules and troubleshooting in `USAGE.md`; put
contributor workflow and repository rules in this file. Keep algorithm details,
cache contracts and design rationale beside the implementation in Python module
docstrings or local comments.

When behavior changes, update the owning reference and keep its examples
working. Prefer links to existing examples and code over duplicated tables of
implementation constants. Do not accumulate design artifacts or historical
implementation narratives in `docs/`.
