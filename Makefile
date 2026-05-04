# GenAI Semantic Conventions - Makefile
# Requires: docker (or podman aliased as docker)
# The weaver version is pinned in versions.env (WEAVER_VERSION) and run via
# the otel/weaver container image -- contributors do not need to install weaver locally.

# Shared external version pins. Override on the command line when needed, e.g.
# `make check-policies SEMCONV_VERSION=v1.40.0`.
VERSION_PINS_FILE := versions.env
include $(VERSION_PINS_FILE)

# Run weaver via the pinned container image. The repo is bind-mounted at
# /workspace and that is the working directory, so every relative path the
# targets below pass to weaver (./model, .build/..., docs/, docs/registry)
# resolves the same way it would for a host-installed weaver.
WEAVER_IMAGE := otel/weaver:$(WEAVER_VERSION)
WEAVER := docker run --rm \
	-u $(shell id -u):$(shell id -g) \
	-v "$(CURDIR):/workspace" \
	-w /workspace \
	-e HOME=/tmp \
	$(WEAVER_IMAGE)

# Shared policy pack from open-telemetry/opentelemetry-weaver-packages. The URL
# and ref are pinned in versions.env (POLICY_REPO_URL, POLICY_REPO_REF). We
# track a named ref rather than a commit SHA because Weaver panics in
# `registry check` when fetching a remote policy pack by commit (reproduced
# on 0.23.0). See versions.env for the pin to switch back once that is fixed.
WEAVER_POLICY_PACK := $(POLICY_REPO_URL)@$(POLICY_REPO_REF)

# Filtered copy of the upstream semantic-conventions model. We clone the
# pinned upstream registry and delete the subdirectories that have been
# migrated into this repo.
#
# This repo is the new home for GenAI (and MCP) semantic conventions. The
# definitions here are the canonical ones going forward; the matching
# definitions still living in open-telemetry/semantic-conventions will be
# removed once the migration completes. Until then, the pinned upstream
# registry we depend on for shared attributes (server.*, error.type, etc.)
# also still contains GenAI/MCP/OpenAI groups that overlap with ours.
# Feeding both copies to Weaver would mean resolving the same id twice, so
# we strip the now-local subdirectories out of the upstream copy before
# Weaver sees it. When upstream finishes removing these definitions this
# filter becomes a no-op and can be deleted.
SC_UPSTREAM_CHECKOUT := .build/sc-upstream-$(SEMCONV_VERSION)
SC_UPSTREAM_FILTERED := .build/sc-upstream-filtered
SC_UPSTREAM_STAMP := $(SC_UPSTREAM_FILTERED)/.stamp-$(SEMCONV_VERSION)

# Upstream directories whose contents now live in this repo. Delete these
# from the filtered copy so their group ids do not collide with ours.
SC_UPSTREAM_MIGRATED_DIRS := gen-ai mcp openai

# Group-level migrations: upstream namespaces where we own only a subset of
# the groups inside a shared registry file. Each entry is `<file>:<group_id>`,
# relative to upstream `model/`. The filtered upstream copy has each listed
# group stripped from its file so Weaver does not see two definitions of the
# same group id. Keep in sync with `GROUP_IMPORTS` in
# `internal/tools/overwrite_model_from_upstream.py`.
SC_UPSTREAM_MIGRATED_GROUPS := aws/registry.yaml:registry.aws.bedrock

.PHONY: check-policies schema-snapshot generate-registry generate-docs package generate-all clean filter-upstream

# Pinned upstream GitHub URL base. Templates use this to render attribute and
# prose links into the upstream registry for namespaces we don't host locally
# (server, error, network, ...). Local-vs-upstream is decided per-attribute by
# the template, using the v2 `provenance` field on each attribute object.
UPSTREAM_DOCS_BASE := https://github.com/open-telemetry/semantic-conventions/blob/$(SEMCONV_VERSION)

# Clone upstream semantic-conventions at the pinned version and drop the
# subdirectories that have been migrated into this repo. See the long
# comment on SC_UPSTREAM_FILTERED above.
$(SC_UPSTREAM_STAMP): $(VERSION_PINS_FILE)
	@mkdir -p .build
	rm -rf $(SC_UPSTREAM_CHECKOUT) $(SC_UPSTREAM_FILTERED)
	git clone --depth 1 --branch $(SEMCONV_VERSION) \
		https://github.com/open-telemetry/semantic-conventions.git \
		$(SC_UPSTREAM_CHECKOUT)
	cp -r $(SC_UPSTREAM_CHECKOUT)/model $(SC_UPSTREAM_FILTERED)
	cd $(SC_UPSTREAM_FILTERED) && rm -rf $(SC_UPSTREAM_MIGRATED_DIRS)
	@# Strip group-level migrated entries (file:group_id) from the filtered
	@# upstream copy. Awk slices out each `  - id: <group_id>` block up to the
	@# next sibling group at the same indent (or EOF), matching the textual
	@# extraction that internal/tools/overwrite_model_from_upstream.py performs
	@# in the opposite direction.
	@for entry in $(SC_UPSTREAM_MIGRATED_GROUPS); do \
		file=$${entry%%:*}; gid=$${entry##*:}; \
		target=$(SC_UPSTREAM_FILTERED)/$$file; \
		if [ -f "$$target" ]; then \
			awk -v gid="$$gid" 'BEGIN{skip=0} \
				/^  - id: / { skip = ($$0 == "  - id: " gid) } \
				!skip { print }' "$$target" > "$$target.tmp" && \
			mv "$$target.tmp" "$$target"; \
		fi; \
	done
	touch $(SC_UPSTREAM_STAMP)

filter-upstream: $(SC_UPSTREAM_STAMP)

# Validate the model and run shared policies. Each `--policy` selects one
# subdirectory of the weaver-packages repo via Weaver's `<url>@<ref>[<subpath>]`
# fetch syntax, so we never materialize a local copy.
check-policies: $(SC_UPSTREAM_STAMP)
	$(WEAVER) registry check \
		-r ./model \
		--v2 \
		--policy '$(WEAVER_POLICY_PACK)[policies/check/naming_conventions]' \
		--policy '$(WEAVER_POLICY_PACK)[policies/check/backwards-compatibility]' \
		--policy '$(WEAVER_POLICY_PACK)[policies/check/stability]'

# Generate the attribute registry pages under docs/registry/ (full directory
# of generated markdown). Templates live under templates/registry/
# markdown/ and consume the v2 resolved registry directly
# (`.registry.attributes` / `.registry.spans` / `.refinements.spans` etc.).
# Local-vs-upstream attribute links are decided per-attribute via the
# `provenance` field; non-local refs render as pinned upstream URLs using
# `--param upstream_docs_base`.
generate-registry: $(SC_UPSTREAM_STAMP)
	$(WEAVER) registry generate \
		-r ./model \
		--v2 \
		-t ./templates/registry \
		--param upstream_docs_base=$(UPSTREAM_DOCS_BASE) \
		markdown \
		./docs/registry

# Refresh the weaver snippet tables embedded in hand-written signal docs under
# docs/gen-ai/ (rewritten in place between <!-- weaver ... --> markers).
generate-docs: $(SC_UPSTREAM_STAMP)
	$(WEAVER) registry update-markdown \
		-r ./model \
		--v2 \
		-t ./templates \
		--target markdown \
		--param registry_base_url=/docs/registry/ \
		--param upstream_docs_base=$(UPSTREAM_DOCS_BASE) \
		docs

# Output the resolved schema for local debugging. Writes to ./resolved (gitignored).
# For the committed snapshot used by reviewers and downstream consumers, run
# `make schema-snapshot` instead -- that target writes to ./schema-snapshot and is checked in.
package: $(SC_UPSTREAM_STAMP)
	$(WEAVER) registry package \
		-r ./model \
		--skip-policies \
		--v2 \
		--resolved-schema-uri https://todo/v1.42.0-dev/resolved.yaml \
		-o ./resolved

# Run every weaver-driven regeneration the repo owns: build the resolved
# schema (./resolved, gitignored), refresh the committed schema snapshot
# (./schema-snapshot), and rebuild docs under ./docs. CI runs this and
# fails if any committed output is out of sync.
generate-all: package schema-snapshot generate-registry generate-docs

# Render the resolved registry as a single YAML file under ./schema-snapshot.
# The output is committed; CI runs `make schema-snapshot` and fails if the working
# tree differs, so reviewers can see schema-level changes in PR diffs.
schema-snapshot: $(SC_UPSTREAM_STAMP)
	$(WEAVER) registry generate \
		-r ./model \
		--v2 \
		-t ./templates/registry \
		yaml \
		./schema-snapshot

# Remove generated docs, the local .build/ tree (Weaver-fetched templates/policies
# plus any hand-created weaver-min-repro* dirs), reference-project caches, and
# Python bytecode trees under the entire repo.
#
# `clean` does NOT touch `reference/.venv`; rebuilding it requires a fresh
# `uv sync` which re-downloads every tooling dependency. Remove it manually
# (`rm -rf reference/.venv`) for a full reset.
clean:
	rm -rf docs/registry
	rm -rf resolved
	rm -rf .build
	rm -rf reference/.cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
	find . -type d -name .ruff_cache -prune -exec rm -rf {} +
