---
name: define-v2
description: 'Use when writing or editing weaver v2 definition schema (file_format: definition/2*) for spans, metrics, events, attribute groups, or signal refinements in any semantic-conventions registry. Enforces minimal attribute groups, internal-by-default visibility, flat structure, and refinement of generic signals over re-declaration. Always validates by running the repo''s package target (typically `make package`).'
argument-hint: 'Describe the new or modified span/metric/event/entity and which generic signal it specializes (if any).'
---

# Define v2

Use this skill when adding or changing a weaver v2 definition file
(`file_format: definition/2` or `file_format: definition/2.*`) — spans, metrics, events, entities, attribute
groups, or signal refinements — in any semantic-conventions registry.

## Goal

Produce a v2 definition that is minimal, flat, and resolvable by weaver
without errors.

The result should reuse existing signals and attribute groups wherever
possible, declare a new attribute group only when two or more signals
genuinely share the same attribute set, and express
"signal X is an implementation of generic signal Y" as an explicit
refinement rather than a copy of Y's attributes.

## Non-Goals

This skill is not for deciding *whether* a new convention should be added,
or for arguing about attribute names, requirement levels, or stability —
those are upstream design questions. Once that decision is made, this skill
turns it into well-shaped v2 yaml.

This skill is also not a generator. It does not write attribute names,
descriptions, examples, or notes for you. Bring those, and the skill will
shape the structure around them.

## Core Stance

- Reuse before redeclare. If a signal or attribute group with the right
  shape already exists in the registry, reference it.
- Refinement before re-declaration. If the new signal is an
  implementation-specific variant of a signal already defined in this
  registry, declare it as a refinement (`span_refinements`,
  `metric_refinements`, `event_refinements`, `entity_refinements`) and add
  only the delta. Do not copy attributes from the parent.
- Fewest groups that explain the structure. Prefer one shared base group
  plus inlined per-signal attributes over a hierarchy of groups that each
  add one attribute.
- Internal by default. Authoring groups are infrastructure; only public
  attribute groups should be `visibility: public` (if group is intended for public consumption,
  and intent is demonstrated by using the group in markdown snippets).
- Validate every change by running the host repo's package target
  (typically `make package`) before considering the work done.

## Schema Reference

This skill encodes opinions, not the v2 grammar. For top-level sections,
required keys per signal, valid `kind` / `instrument` / `stability` /
`requirement_level` values, override rules, and everything else about
what is or isn't legal yaml — read the doc directly. Always.

Fetch the relevant one before any non-trivial change; do not guess at v2
syntax from memory or copy-paste:

- Lifecycle and overview (definition vs. resolved vs. materialized):
  `https://raw.githubusercontent.com/open-telemetry/weaver/main/schemas/semconv-schemas.md`
- Authoring syntax reference (every key, type, and validation rule for a
  `definition/2` file):
  `https://raw.githubusercontent.com/open-telemetry/weaver/main/schemas/semconv-syntax.v2.md`

If anything in this skill contradicts the doc, the doc wins — this file
intentionally does not mirror the grammar so it cannot rot when upstream
changes.

## Authoring Rules

### 1. Minimal attribute groups

Only create an `attribute_groups` entry when the *same exact set* of
attributes is reused by two or more signals. A group used by exactly one
signal is dead weight — inline the attributes on the signal directly.

Avoid creating attribute_group with one attribute.

### 2. Internal by default

Every authoring `attribute_groups` entry MUST have `visibility: internal`
unless the group is genuinely a public registry entity. Public groups
carry stricter required-key obligations (see the syntax doc); if you
don't need those, you don't want a public group.

Concretely, human-authored docs render attribute tables by referencing
groups in `<!-- semconv <group_id> -->` markers, which `weaver registry
update-markdown` rewrites in place (typically wrapped by a `make
generate-docs` target or equivalent). Being snippet-rendered is a signal
that a group is user-facing — but it is not by itself sufficient: an
`internal` group can also be embedded in a snippet, and visibility does
not gate rendering. The sharper test: if you would naturally write a
`brief` and pick a `stability` for the group *as a thing in its own
right* (something users link to or reference outside the snippet that
embeds it), it is public. If it exists only to compose attributes for
one or two signals — even if those signals' docs render its table —
keep it internal.

### 3. Flat structure

A `ref_group` chain deeper than two levels (`base → mid → leaf`) is a
refactor signal. Two levels is the maximum:

```yaml
attribute_groups:
  - id: attributes.<domain>.common         # level 1
    visibility: internal
    attributes:
      - ref: <attr.a>

  - id: attributes.<domain>.client         # level 2
    visibility: internal
    attributes:
      - ref_group: attributes.<domain>.common
      - ref: <attr.b>
```

If you are reaching for a third level, the right move is almost always to
flatten groups, or to split the leaves so they each ref the base group
directly and inline their delta.

### 4. Refinement over redefinition

If the new signal is an implementation-specific variant of a generic
signal that already exists in this registry, declare it in the matching
`*_refinements` section (`span_refinements`, `metric_refinements`,
`event_refinements`, `entity_refinements`) and add only the delta.

Refinements inherit the parent's attributes — do **not** re-list them.
List an inherited attribute only to override its presentation (note,
brief, requirement_level, examples, sampling_relevant — see the syntax doc
for which keys are overridable on which signals).

If you are tempted to declare the variant as a brand-new signal in `spans:`
and `ref_group:` the parent's attribute group(s), stop and use a refinement
instead. The attribute-group form loses the explicit "this implements
that" relationship and produces noisier resolved output.

### 5. Override on the signal, not on the attribute

When a signal needs a different `requirement_level`, `note`, `brief`, or
similar for an inherited attribute, override it at the reference site —
do not edit the underlying attribute definition. The syntax doc lists
which keys are overridable per signal type.

Do not duplicate the underlying attribute's `brief` / `note` verbatim —
leave them out so the inherited values show through. Use an empty string
(`note: ""`) only when you genuinely want to suppress an inherited note.

## Refinement Decision Tree

For every new signal, walk this tree before writing any yaml:

1. Is this an implementation-specific variant of a signal already defined
   in this registry (same operation, just a specific provider, framework,
   or transport)?
   - **Yes** → declare it in the matching `*_refinements` section with
     `ref:` pointing at the parent's `type` or `name`. Add only the delta
     attributes. Use per-attribute overrides for any inherited attribute
     whose framing changes. Stop.
   - **No** → continue.
2. Does this signal share its attribute set with at least one other signal
   already in the registry?
   - **Yes** → reuse the existing attribute group via `ref_group`. Do not
     create a new one. Add only the delta inline on the signal.
   - **No** → continue.
3. Does this signal share its attribute set with at least one other signal
   you are introducing in the same change?
   - **Yes** → create one shared internal attribute group, used by both.
     One level deep. Do not nest.
   - **No** → inline the attributes on the signal. Do not create an
     attribute group.

## Procedure

1. Read the user's intent. Identify whether the change touches
   spans, metrics, events, entities, attribute groups, refinements, or
   several of those.
2. **Capture a pre-change baseline.** Before editing any model file,
   run the repo's `make package` and (if it exists) the committed-snapshot
   target — `make schema-snapshot` in this registry, `make generate` in
   others — so the resolved-schema and committed-snapshot artifacts
   reflect the current model. After your edits you will re-run them
   and read the diff to confirm only the intended signals/attributes
   moved. Skip this step only if the user has already mentioned that a
   baseline was captured, or if the working tree shows the artifacts
   are already in sync with HEAD.
3. If the syntax for any of those sections is not already obvious from the
   surrounding files, fetch the v2 syntax reference (see Schema Reference
   above) before editing.
4. Walk the Refinement Decision Tree for every new signal in the change.
5. For every new attribute group, confirm `visibility: internal` unless
   the change is explicitly adding to a public group.
6. Write the yaml with the fewest groups and shallowest hierarchy that
   satisfies the rules.
7. Run validation (next section). Fix every error before stopping.
8. Re-read the diff for groups used exactly once, `ref_group` chains
   deeper than two levels, public groups that should be internal, and
   re-listed parent attributes on refinements. These are the four most
   common drifts and the easiest to fix in a final pass.
9. Regenerate every committed artifact the repo produces from the model
   — `make package`, the snapshot target (`make schema-snapshot` here,
   `make generate` in other registries), and `make generate-docs` if all
   three exist — and compare against the baseline from step 2. See the
   Output Format section for what each does and what diffs to expect.
   Do not stop the skill until each target has been run and the
   resulting diffs are linked.

## Validation

The non-negotiable gate: the resolved registry must build cleanly with
zero errors after the change.

Prefer the host repo's Makefile (or equivalent) wrapper — it already
passes the right `--registry`, dependencies, `--v2`, and policies. Look
for a target that produces a resolved schema; common names are `package`,
`resolve`, `check`, `check-policies`, `validate`. A typical invocation
looks like:

```
make package
```

If the repo also exposes a `check` / `check-policies` target that runs
the upstream policy pack, run that too — it catches id collisions and
naming-convention violations that the package step alone does not.

Only fall back to invoking weaver directly when the repo has no wrapper
(rare). In that case use `weaver registry package -r <registry-root>
--v2`, or the pinned container image (`otel/weaver:<pinned-version>`)
when weaver is not installed locally. Note: `weaver registry resolve` is
the older name for this command and is deprecated — use `package` even
if you see `resolve` in older docs.

A change is not done until validation is green.

## Output Format

Do not write a prose summary of what changed. Run the repo's wrappers
and link the artifacts — those are the change.

Run each of these targets that the host repo exposes (most repos have
all three; some only have a subset). Run them in order — the snapshot
and docs targets both consume the resolved model that package produces:

1. **`make package`** — builds the resolved schema and runs validation.
   The output directory is wrapper-specific (e.g. `./resolved`,
   `./output`); this file is typically gitignored. Link the resolved
   schema file it wrote.
2. **The committed-snapshot target** — `make schema-snapshot` in this
   registry (output at `./schema-snapshot/registry.yaml`); `make generate`
   in other registries (output typically at `./generated/registry.yaml`).
   Produces a committed, single-file snapshot of the resolved registry.
   This is not docs; it is a reviewable schema diff. Link the file. If
   CI has a "snapshot is in sync" check, this is the target it runs.
3. **`make generate-docs`** (or `make docs`) — regenerates per-namespace
   attribute pages under `docs/registry/` and refreshes the
   `<!-- semconv ... -->` snippet tables embedded in hand-authored docs.
   This is the actual docs regeneration step; the snapshot target does
   not do this.

After running all three, do `git status` (or `git diff --name-only`)
and link every file the regeneration changed, grouped as:

- the resolved-registry snapshot (e.g. `./schema-snapshot/registry.yaml`
  or `./generated/registry.yaml`), if the repo has a snapshot target
- regenerated registry pages under `docs/registry/`
- snippet refreshes elsewhere under `docs/`

The resolved schema and the docs diff already say which signals,
refinements, and attribute groups moved and how. A prose recap would
only restate them, and tends to drift from the artifacts over time.
