# internal/tools

Maintainer-only scripts. Not part of the public contributor workflow — none
of these are wired into `make` targets or `CONTRIBUTING.md`. Run them
directly with `uv` from the repo root.

> **Temporary migration tooling.** Both scripts exist only while the GenAI
> namespaces still live in both this repo and
> [open-telemetry/semantic-conventions](https://github.com/open-telemetry/semantic-conventions).
> The cutover lands before upstream itself moves to Weaver's `definition/2`
> source layout, so every re-import lands as `definition/1` and needs to be
> converted up to the `definition/2` layout this repo uses
> ([OTEP 4815](https://github.com/open-telemetry/opentelemetry-specification/pull/4815),
> [weaver#1333](https://github.com/open-telemetry/weaver/pull/1333)).
> Once upstream deletes the migrated namespaces, the re-import workflow
> ends and these scripts have nothing left to do — delete this directory
> at that point.

## `overwrite_model_from_upstream.py`

Re-imports the locally-owned namespaces (`gen-ai`, `mcp`, `openai`) from
[open-telemetry/semantic-conventions](https://github.com/open-telemetry/semantic-conventions)
into `model/`. Used while this repo coexists with upstream to pick up
community fixes that landed there.

```sh
# Pull upstream main into model/:
uv run internal/tools/overwrite_model_from_upstream.py

# Pin to a tag/SHA:
uv run internal/tools/overwrite_model_from_upstream.py --ref v1.41.0
```

The imported files are left in whatever schema upstream uses
(currently `definition/1`). Run `convert_model_to_v2.py` next if you
need to convert them.

## `convert_model_to_v2.py`

Converts Weaver semconv source files from the legacy `definition/1` layout
(implicit `groups:` list) to `definition/2` (explicit `attributes:`,
`attribute_groups:`, `spans:`, `events:`, `metrics:`). See
[OTEP 4815](https://github.com/open-telemetry/opentelemetry-specification/pull/4815)
and [weaver#1333](https://github.com/open-telemetry/weaver/pull/1333) for the
underlying schema change.

```sh
uv run internal/tools/convert_model_to_v2.py
```

Idempotent: files already declaring `file_format: definition/2` are
skipped. Anything the converter cannot translate cleanly (event `body:`
schemas, missing `span_kind`, exotic v1 shapes outside the
`attribute_group` / `span` / `event` / `metric` set) is reported on
stderr and left for manual follow-up.

## Typical re-sync workflow

```sh
uv run internal/tools/overwrite_model_from_upstream.py --ref main
uv run internal/tools/convert_model_to_v2.py
make check-policies
make generate-docs
```

Then diff against `HEAD`, re-apply this repo's local edits where they got
overwritten, and open a PR.
