set positional-arguments

# List available commands.
[default]
[private]
list:
    @just --list

# Install the project and development dependencies from the lockfile.
[group('build')]
sync:
    uv sync --locked

# Build a source distribution and wheel.
[group('build')]
build:
    uv build

# Run the video workbench CLI.
[group('run')]
run *args:
    uv run --locked vw "$@"

# Run unit tests and small real-media smoke tests.
[group('test')]
test *args:
    uv run --locked pytest "$@"

# Run all tests, including comprehensive media integration cases.
[group('test')]
test-all *args:
    uv run --locked pytest -m '' "$@"

# Sort imports and format Python files.
[group('format')]
ruff-format:
    ruff check --select I --fix .
    ruff format .

# Format all Nix files outside generated directories.
[group('format')]
nixfmt:
    fd --hidden --exclude .git --type f --extension nix . --exec-batch nixfmt

# Format the justfile.
[group('format')]
just-format:
    just --format

# Run all formatters.
[group('format')]
[parallel]
format: ruff-format nixfmt just-format

[group('lint')]
ruff:
    ruff check .

[group('lint')]
ruff-format-check:
    ruff format --check .

[group('lint')]
nixfmt-check:
    fd --hidden --exclude .git --type f --extension nix . --exec-batch nixfmt --check

[group('lint')]
just-format-check:
    just --fmt --check

[group('lint')]
statix:
    statix check .

[group('lint')]
deadnix:
    deadnix --fail .

# Check Python, Nix, and justfile style without changing files.
[group('lint')]
lint: ruff ruff-format-check nixfmt-check just-format-check statix deadnix

# Run lint and the rapid iteration suite.
[group('test')]
check: lint test

# Run lint and every media integration test before merging rendering changes.
[group('test')]
check-all: lint test-all

# Create and render a complete demo using generated footage and audio.
[group('run')]
demo directory:
    uv run --locked python examples/make_demo.py "{{ directory }}"
