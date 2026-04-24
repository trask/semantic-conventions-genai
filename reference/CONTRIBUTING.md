# Contributing to the Reference Implementations

This directory contains the runnable reference implementations and the tooling
used to validate them against the GenAI semantic conventions.

If you are changing the semantic conventions themselves under `model/` or
`docs/`, use the repository-level guide in [../CONTRIBUTING.md](../CONTRIBUTING.md).

## Structure

```text
pyproject.toml           # Reference tooling project metadata
src/
  semconv_genai/         # Shared framework, CLI modules, and mock server
scenarios/
  <library>/             # Curated Python reference implementations
```

Within each library directory:

- `scenario.py` — Reference scenario (SDK invocation + manual OTel spans)
- `pyproject.toml` — Dependencies
- `uv.lock` — Locked transitive dependency graph (committed)
- `data.json` — Committed results

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (uv will fetch the Python 3.12 interpreter declared in `pyproject.toml` on first run).

`Weaver` is installed automatically by the reference tooling on first run.

Run the commands below from this `reference/` directory.

See [README.md](README.md#quick-start) for the minimum `uv sync` / `uv run run-scenario` commands.
`uv run run-scenario` starts [src/semconv_genai/mock_server/](src/semconv_genai/mock_server/) as a subprocess when
one isn't already listening on its port. `uv sync --locked` fails if [uv.lock](./uv.lock) has drifted from
[pyproject.toml](./pyproject.toml); after changing tooling dependencies, run `uv lock` and commit the
refreshed lockfile.

## Typical workflow

The reference workflow uses a local mock LLM server plus Weaver `registry
live-check`: `uv run run-scenario <library>` starts
[src/semconv_genai/mock_server/](src/semconv_genai/mock_server/), launches Weaver
live-check, runs the selected scenario under [scenarios/](scenarios/), and writes the
validated results that feed the checked-in reports.

Run all Python reference implementations serially:

```bash
uv run run-scenario --all
```

Continue through failures and report them at the end:

```bash
uv run run-scenario --all --keep-going
```

Lint and format the Python code under `src/semconv_genai/` and `scenarios/`:

```bash
uv tool run --from ruff ruff check --fix src/semconv_genai scenarios
uv tool run --from ruff ruff format src/semconv_genai scenarios
```

Regenerate the checked-in status section in `README.md` after updating committed
`data.json` files:

```bash
uv run update-reports
```

## Contribution expectations

- Keep reference coverage honest. Only emit spans and attributes that the
  library or reference code can actually produce.
- Prefer focused updates to the affected library under `scenarios/<library>/`.
- Commit regenerated `scenarios/*/data.json` files when validation output changes.
- Commit updated `README.md` when the checked-in status section changes.
- If a library emits unrelated native telemetry that obscures the intended
  validation surface, suppress that library-owned telemetry in the reference
  test rather than changing the semantic conventions to match it.

## Adding or updating a library

When adding a new Python reference implementation:

1. Create `scenarios/<library>/scenario.py`.
2. Create `scenarios/<library>/pyproject.toml` declaring the SDK dependencies plus
   `genai-reference-shared` (sourced from the shared project at `shared/`).
   The OTel SDK pin is provided transitively by `genai-reference-shared`; do
   not re-declare it here unless the library needs a non-default version
   (see below):

   ```toml
   [project]
   name = "<library>-reference-test"
   version = "0"
   requires-python = ">=3.12"
   dependencies = [
       "<sdk>==<pinned-version>",
       "genai-reference-shared",
   ]

   [tool.uv.sources]
   genai-reference-shared = { path = "../../shared", editable = true }

   [tool.uv]
   package = false
   ```

   If the SDK under test requires a specific OTel version that differs
   from the default pin in [shared/pyproject.toml](./shared/pyproject.toml),
   override it with `[tool.uv] override-dependencies`:

   ```toml
   [tool.uv]
   package = false
   override-dependencies = [
       "opentelemetry-api==<required>",
       "opentelemetry-sdk==<required>",
       "opentelemetry-exporter-otlp-proto-grpc==<required>",
   ]
   ```

3. Run `uv lock` inside `scenarios/<library>/` to generate the committed `uv.lock`.
4. Run the test to generate `scenarios/<library>/data.json`.
5. Regenerate `README.md` with `uv run update-reports`.

When changing dependencies, edit the library's `pyproject.toml` and re-run
`uv lock` in that directory. `run-scenario` uses `uv sync --frozen`, so the lock
file must be committed alongside any dependency change.

Keep new reference implementations minimal and readable. These files are both
validation inputs and examples for instrumentation authors.
