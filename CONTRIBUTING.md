# Contributing

Welcome to the OpenTelemetry GenAI Semantic Conventions repository!

Before you start — see the OpenTelemetry general
[contributing](https://github.com/open-telemetry/community/blob/main/guides/contributor/README.md)
requirements and recommendations.

## Sign the CLA

Before you can contribute, you will need to sign the
[Contributor License Agreement](https://identity.linuxfoundation.org/projects/cncf).

## How to contribute

- All attributes, metrics, etc. are formally defined in YAML files under
  the `model/` directory.
- All descriptions and normative language are defined in the `docs/` directory.
- In the PR description, include links to the relevant instrumentation and any
  applicable prototypes.

If you are working on the runnable reference project under `reference/`, see
[reference/CONTRIBUTING.md](reference/CONTRIBUTING.md).

Changes under `model/` or `docs/` can also require regenerated reference
scenario outputs and reference docs; the reference guide covers that workflow.

### Prerequisites

Install [Weaver](https://github.com/open-telemetry/weaver/releases)
(>= 0.22.1) and ensure it is on your `PATH`.

### 1. Modify the YAML model

Refer to the
[Semantic Convention YAML Language](https://github.com/open-telemetry/weaver/blob/main/schemas/semconv-syntax.md)
to learn how to make changes to the YAML files.

#### Code structure

```
├── docs
│   ├── attributes/        # auto-generated attribute registry pages
│   ├── gen-ai/            # hand-written signal docs (spans, metrics, events)
│   ├── mcp/               # hand-written MCP signal docs
├── model
│   ├── manifest.yaml      # dependency on core semantic conventions
│   ├── gen-ai/
│   │   ├── registry.yaml  # attribute definitions
│   │   ├── spans.yaml     # span conventions
│   │   ├── metrics.yaml   # metric conventions
│   │   ├── events.yaml    # event conventions
│   │   └── deprecated/    # deprecated conventions
│   ├── mcp/
│   ├── openai/
```

All attributes must be defined in `registry.yaml` files under the matching
namespace folder in `model/`.

### 2. Update the markdown files

After updating the YAML file(s), regenerate the documentation:

```bash
make generate-docs update-markdown
```

When defining new telemetry signals (spans, metrics, events), add a new
markdown section with semconv markers:

```markdown
<!-- semconv new-group-id -->
<!-- endsemconv -->
```

Then re-run the generation commands above.

### 3. Validate

Run the full validation suite:

```bash
make check
```

This validates the model against shared policies from
[opentelemetry-weaver-packages](https://github.com/open-telemetry/opentelemetry-weaver-packages).

To verify that docs are in sync:

```bash
make check-docs
```

### 4. Getting your PR merged

A PR is considered **ready to merge** when:

- It has received the required approvals
- There are no open discussions
- It has been at least two working days since the last modification
  (except for trivial updates like typos, cosmetic changes, rebases)

## Makefile targets

| Target             | Description                                              |
| ------------------ | -------------------------------------------------------- |
| `check`            | Validate the model and run shared policies               |
| `generate-docs`    | Generate attribute registry pages                        |
| `update-markdown`  | Update semconv tables in hand-written signal docs        |
| `check-docs`       | Verify generated docs are in sync with the model         |
| `resolve`          | Output the resolved schema (for debugging)               |
| `clean`            | Remove generated and cached artifacts                    |
