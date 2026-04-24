#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["ruamel.yaml>=0.18"]
# ///
"""Convert Weaver semconv `definition/1` source files to `definition/2`.

The OTEP that introduced schema v2 (open-telemetry/opentelemetry-specification#4815)
and the Weaver PR that locked in `manifest/2.0`/`resolved/2.0`/`definition/2`
file formats (open-telemetry/weaver#1333) reshape the source layout:

  v1 (`definition/1`, implicit when `file_format` is absent)
    groups:
      - id: ...
        type: attribute_group | span | event | metric
        ...

  v2 (`definition/2`)
    file_format: definition/2
    attributes:        # inline attribute defs (key/type/brief/...)
    attribute_groups:  # public/internal bundles of refs
    spans:
    events:
    metrics:
    entities:
    *_refinements:

Weaver v0.23.0 still parses v1, but the upstream semantic-conventions repo will
flip to v2 at the cutover. This script lets us re-run the migration each time
upstream re-syncs.

Usage
-----
    uv run internal/tools/convert_model_to_v2.py

Idempotent: files already declaring `file_format: definition/2` are left alone.

Scope (matches what's currently used in this repo)
--------------------------------------------------
- `attribute_group`, `span`, `event`, `metric` group types.
- Inline attribute definitions inside `attribute_group`s are lifted to the
  top-level `attributes:` list and replaced with `- ref: <key>` inside the
  group.
- `extends: <id>` becomes `- ref_group: <id>` prepended to `attributes:`.
- `id:` on signals is mapped to `type:` (spans), `name:` (events/metrics) per
  Weaver's v2 definitions, dropping the conventional `span.` / `event.` /
  `metric.` prefix when present so the resulting v1-equivalent id is stable.
- Required-but-missing v2 fields are filled with sensible defaults:
  `stability: development`, span `name.note: <brief>`. A best-effort warning
  is emitted for fields v2 doesn't model (`body`, `display_name`, `prefix`).

Anything outside this scope (entities, *_refinements, exotic v1 shapes) is
flagged on stderr and left for manual follow-up.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

V2_FILE_FORMAT = "definition/2"

MODEL_DIR = Path(__file__).resolve().parents[2] / "model"

# Files we never touch even when they live under a converted tree (e.g.
# Weaver's manifest, which is not a semconv source file).
SKIP_FILENAMES = frozenset({"manifest.yaml"})

# Fields v1 puts directly on a signal group that v2 carries inside
# `CommonFields`. Stability is required in v2; we default it to `development`
# when the v1 source omits it.
COMMON_FIELDS = frozenset({"brief", "note", "stability", "deprecated", "annotations"})
# Allowed v1 keys per signal type, precomputed as the full union including
# structural keys (`id`, `type`, `attributes`, `extends`) and `entity_associations`.
# Anything outside its kind's set is reported and dropped.
_STRUCTURAL = frozenset({"id", "type", "attributes", "extends", "entity_associations"})
ALLOWED_FIELDS = {
    "attribute_group": COMMON_FIELDS | _STRUCTURAL | frozenset({"display_name", "prefix"}),
    "span":            COMMON_FIELDS | _STRUCTURAL | frozenset({"span_kind", "name"}),
    "event":           COMMON_FIELDS | _STRUCTURAL | frozenset({"name", "body"}),
    "metric":          COMMON_FIELDS | _STRUCTURAL | frozenset({"metric_name", "instrument", "unit"}),
}


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 1_000_000  # don't reflow long lines
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _warn(path: Path, msg: str) -> None:
    print(f"warning: {path}: {msg}", file=sys.stderr)


def _normalize_deprecated(value: Any) -> Any:
    """v2 JSON schema validation requires `note:` on every `deprecated` variant.
    Weaver's runtime deserializer tolerates a missing note for `renamed` (it
    fills in `Replaced by ...`), but the JSON schema check in `weaver registry
    check` does not. Backfill a sensible default so the source files round-trip
    through both."""
    if not isinstance(value, dict):
        return value
    if "note" in value and value["note"] not in (None, ""):
        return value
    reason = value.get("reason")
    if reason == "renamed" and value.get("renamed_to"):
        value["note"] = f"Replaced by `{value['renamed_to']}`."
    elif reason in ("obsoleted", "uncategorized", "unspecified"):
        value["note"] = value.get("note") or "No replacement at this time."
    return value


def _backfill_deprecated_notes(node: Any) -> None:
    """Walk a converted document and ensure every `deprecated:` mapping has a
    non-empty `note:` field (v2 JSON schema validation requires it)."""
    if isinstance(node, dict):
        if "deprecated" in node:
            _normalize_deprecated(node["deprecated"])
        for v in node.values():
            _backfill_deprecated_notes(v)
    elif isinstance(node, list):
        for item in node:
            _backfill_deprecated_notes(item)


def _is_inline_attr_def(entry: Any) -> bool:
    """v1 attribute entries are either `{ref: ...}` (refinement) or `{id: ..., type: ...}`
    (definition). v2 lifts the latter to the top level."""
    return isinstance(entry, dict) and "id" in entry and "ref" not in entry


def _convert_inline_attr_to_v2(entry: dict, path: Path) -> dict:
    """Translate a v1 inline attribute definition (`id: ...`, `type: ...`, ...)
    into a v2 `AttributeDef` (`key: ...`, `type: ...`, ...)."""
    out = CommentedMap()
    out["key"] = entry["id"]
    if "type" in entry:
        out["type"] = entry["type"]
    # Required: brief. Optional: examples, note, stability, deprecated, annotations.
    out["brief"] = entry.get("brief", "")
    for opt in ("note", "examples", "stability", "deprecated", "annotations"):
        if opt in entry:
            out[opt] = entry[opt]
    if "stability" not in out:
        out["stability"] = "development"
    # Drop unknown-to-v2 fields with a warning.
    for k in entry:
        if k not in {"id", "type", "brief", "note", "examples", "stability", "deprecated", "annotations"}:
            _warn(path, f"attribute {entry['id']!r}: dropping field {k!r} not modeled in v2")
    return out


def _convert_attr_refs(
    attrs: list,
    path: Path,
    owner_id: str,
    lifted: list,
    lifted_keys: set,
    *,
    allow_sampling_relevant: bool,
) -> list:
    """Walk a v1 `attributes:` list. Inline defs get lifted into `lifted` (top-level
    `attributes:`) and replaced with `- ref: <key>`. Refs pass through untouched.

    `sampling_relevant` is a span-only field in v2. When `allow_sampling_relevant`
    is False (attribute_groups, events, metrics) we strip it with a warning."""
    out = CommentedSeq()
    for entry in attrs:
        if not isinstance(entry, dict):
            _warn(path, f"{owner_id}: skipping non-mapping attribute entry: {entry!r}")
            out.append(entry)
            continue
        if _is_inline_attr_def(entry):
            key = entry["id"]
            if key not in lifted_keys:
                lifted.append(_convert_inline_attr_to_v2(entry, path))
                lifted_keys.add(key)
            else:
                _warn(path, f"{owner_id}: attribute {key!r} defined more than once; keeping first")
            ref = CommentedMap()
            ref["ref"] = key
            out.append(ref)
        else:
            if not allow_sampling_relevant and "sampling_relevant" in entry:
                _warn(
                    path,
                    f"{owner_id}: dropping `sampling_relevant` on ref {entry.get('ref')!r}; "
                    "v2 only models it on span attribute refs",
                )
                entry = {k: v for k, v in entry.items() if k != "sampling_relevant"}
            out.append(entry)
    return out


def _copy_common(out: CommentedMap, group: dict) -> None:
    """Copy v1 fields that v2 carries directly on a signal/group entry.\n
    `brief` and `stability` are required in v2; the rest are optional.\n
    `entity_associations` is signal-level (span/event/metric) but cheap to\n    handle here since attribute_groups never carry it in our sources."""
    out["brief"] = group.get("brief", "")
    if "note" in group:
        out["note"] = group["note"]
    out["stability"] = group.get("stability", "development")
    if "deprecated" in group:
        out["deprecated"] = group["deprecated"]
    if "annotations" in group:
        out["annotations"] = group["annotations"]
    if "entity_associations" in group:
        out["entity_associations"] = group["entity_associations"]


def _warn_unknown_fields(path: Path, group: dict, kind: str, gid: str) -> None:
    """Warn about v1 fields with no v2 home for a given signal `kind`."""
    for k in group:
        if k not in ALLOWED_FIELDS[kind]:
            _warn(path, f"{kind} {gid!r}: dropping field {k!r} not modeled in v2")


def _build_attributes_list(
    group: dict,
    path: Path,
    owner_id: str,
    lifted: list,
    lifted_keys: set,
    *,
    allow_sampling_relevant: bool,
) -> CommentedSeq:
    """Build the v2 `attributes:` list for a signal/group, prepending
    `- ref_group: <extends>` when a v1 `extends:` is present. Returns an
    empty `CommentedSeq` when there is nothing to emit; callers gate on
    truthiness."""
    out: CommentedSeq = CommentedSeq()
    extends = group.get("extends")
    if extends:
        rg = CommentedMap()
        rg["ref_group"] = extends
        out.append(rg)
    if "attributes" in group:
        out.extend(
            _convert_attr_refs(
                group["attributes"],
                path,
                owner_id,
                lifted,
                lifted_keys,
                allow_sampling_relevant=allow_sampling_relevant,
            )
        )
    return out


def _convert_attribute_group(group: dict, path: Path, lifted: list, lifted_keys: set) -> CommentedMap | None:
    """Convert a v1 `type: attribute_group` group to a v2 attribute_group entry,
    or `None` if the group is a pure registry container (no refs, no extends).

    v2 `attribute_groups` only carry refs and group references -- inline
    attribute definitions live at the top level. A v1 `attribute_group` whose
    body is exclusively inline defs serves no purpose after lifting (Weaver
    will synthesize a `registry.{file}` group for the lifted defs anyway), so
    we drop it. Wrappers that hold refs or `extends:` survive as proper v2
    attribute_groups."""
    gid = group.get("id", "<no-id>")
    has_refs = any(
        isinstance(e, dict) and "ref" in e for e in group.get("attributes", []) or []
    )
    extends = group.get("extends")

    converted_attrs = _build_attributes_list(
        group, path, gid, lifted, lifted_keys, allow_sampling_relevant=False
    )

    if not has_refs and not extends:
        # Pure inline-defs container -- defs already lifted, drop the wrapper.
        return None

    out = CommentedMap()
    out["id"] = gid
    out["visibility"] = "public"
    out["brief"] = group.get("brief") or group.get("display_name") or gid
    if "note" in group:
        out["note"] = group["note"]
    out["stability"] = group.get("stability", "development")
    if "deprecated" in group:
        out["deprecated"] = group["deprecated"]
    if "annotations" in group:
        out["annotations"] = group["annotations"]
    if converted_attrs:
        out["attributes"] = converted_attrs

    _warn_unknown_fields(path, group, "attribute_group", gid)
    return out


def _convert_span(group: dict, path: Path, lifted: list, lifted_keys: set) -> CommentedMap:
    """v1 `type: span` -> v2 `Span`. v1 `id: span.foo` becomes v2 `type: foo`."""
    gid = group.get("id", "<no-id>")
    v2_type = gid.removeprefix("span.")
    out = CommentedMap()
    out["type"] = v2_type
    if "span_kind" in group:
        out["kind"] = group["span_kind"]
    else:
        _warn(path, f"span {gid!r}: missing span_kind")
        out["kind"] = "internal"

    name_block = CommentedMap()
    # v2 SpanName.note is required and documents the span name pattern. We
    # don't have a separate field in v1, so seed it from brief and let
    # maintainers refine.
    name_block["note"] = group.get("brief", v2_type) or v2_type
    out["name"] = name_block

    _copy_common(out, group)

    attrs = _build_attributes_list(
        group, path, gid, lifted, lifted_keys, allow_sampling_relevant=True
    )
    if attrs:
        out["attributes"] = attrs

    _warn_unknown_fields(path, group, "span", gid)
    return out


def _convert_event(group: dict, path: Path, lifted: list, lifted_keys: set) -> CommentedMap:
    """v1 `type: event` -> v2 `Event`. The v1 `name:` field becomes v2 `name:`."""
    gid = group.get("id", "<no-id>")
    out = CommentedMap()
    out["name"] = group.get("name") or gid.removeprefix("event.")
    _copy_common(out, group)

    attrs = _build_attributes_list(
        group, path, gid, lifted, lifted_keys, allow_sampling_relevant=False
    )
    if attrs:
        out["attributes"] = attrs

    if "body" in group:
        _warn(path, f"event {gid!r}: dropping `body` field; v2 Event has no body schema")

    _warn_unknown_fields(path, group, "event", gid)
    return out


def _convert_metric(group: dict, path: Path, lifted: list, lifted_keys: set) -> CommentedMap:
    """v1 `type: metric` -> v2 `Metric`. `metric_name:` becomes `name:`."""
    gid = group.get("id", "<no-id>")
    out = CommentedMap()
    out["name"] = group.get("metric_name") or gid.removeprefix("metric.")
    if "instrument" in group:
        out["instrument"] = group["instrument"]
    if "unit" in group:
        out["unit"] = group["unit"]
    _copy_common(out, group)

    attrs = _build_attributes_list(
        group, path, gid, lifted, lifted_keys, allow_sampling_relevant=False
    )
    if attrs:
        out["attributes"] = attrs

    _warn_unknown_fields(path, group, "metric", gid)
    return out


def convert_doc(doc: Any, path: Path) -> CommentedMap | None:
    """Translate a parsed v1 semconv YAML doc to v2. Returns `None` when the
    doc is already v2 or doesn't look like a semconv source file."""
    if not isinstance(doc, dict):
        return None
    if doc.get("file_format") == V2_FILE_FORMAT:
        return None
    if "groups" not in doc:
        # Not a semconv source file (e.g. manifest.yaml).
        return None

    new_doc = CommentedMap()
    new_doc["file_format"] = V2_FILE_FORMAT

    lifted_attrs: list = []
    lifted_keys: set = set()
    attribute_groups: list = []
    spans: list = []
    events: list = []
    metrics: list = []
    entities: list = []
    unknown: list = []

    for group in doc["groups"] or []:
        if not isinstance(group, dict):
            _warn(path, f"skipping non-mapping group: {group!r}")
            continue
        gtype = group.get("type")
        if gtype == "attribute_group":
            ag = _convert_attribute_group(group, path, lifted_attrs, lifted_keys)
            if ag is not None:
                attribute_groups.append(ag)
        elif gtype == "span":
            spans.append(_convert_span(group, path, lifted_attrs, lifted_keys))
        elif gtype == "event":
            events.append(_convert_event(group, path, lifted_attrs, lifted_keys))
        elif gtype == "metric":
            metrics.append(_convert_metric(group, path, lifted_attrs, lifted_keys))
        elif gtype == "entity":
            _warn(path, f"entity group {group.get('id')!r}: pass-through, manual review needed")
            entities.append(group)
        else:
            _warn(path, f"unknown group type {gtype!r} on {group.get('id')!r}; passing through")
            unknown.append(group)

    if lifted_attrs:
        new_doc["attributes"] = CommentedSeq(lifted_attrs)
    if attribute_groups:
        new_doc["attribute_groups"] = CommentedSeq(attribute_groups)
    if entities:
        new_doc["entities"] = CommentedSeq(entities)
    if events:
        new_doc["events"] = CommentedSeq(events)
    if metrics:
        new_doc["metrics"] = CommentedSeq(metrics)
    if spans:
        new_doc["spans"] = CommentedSeq(spans)
    if unknown:
        new_doc["_unknown_groups"] = CommentedSeq(unknown)

    # Pass through top-level `imports:` if present (v2 supports it as-is).
    if "imports" in doc:
        new_doc["imports"] = doc["imports"]

    _backfill_deprecated_notes(new_doc)
    return new_doc


def convert_file(path: Path) -> bool:
    """Convert a single YAML file in place. Returns True if it changed."""
    yaml = _yaml()
    doc = yaml.load(path.read_text(encoding="utf-8"))
    new_doc = convert_doc(doc, path)
    if new_doc is None:
        return False
    buf = io.StringIO()
    yaml.dump(new_doc, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return True


def main() -> int:
    for path in sorted(MODEL_DIR.rglob("*.yaml")):
        if path.name in SKIP_FILENAMES:
            continue
        try:
            if convert_file(path):
                print(f"converted: {path}")
        except Exception as e:  # noqa: BLE001 - surface any parse/convert error per-file
            print(f"error: {path}: {e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
