# GenAI Semantic Conventions - Makefile
# Requires: docker (or podman aliased as docker)
# The weaver version is pinned in versions.env (WEAVER_VERSION) and run via
# the otel/weaver container image -- contributors do not need to install weaver locally.

# Shared external version pins. Override on the command line when needed, e.g.
# `make check-policies SEMCONV_VERSION=v1.40.0`.
VERSION_PINS_FILE := versions.env
include $(VERSION_PINS_FILE)

# Templates from the main semconv repo for doc generation
TEMPLATE_REPO := https://github.com/open-telemetry/semantic-conventions.git@$(SEMCONV_VERSION)[templates]

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

# Local cache of policies fetched from upstream (gitignored)
LOCAL_POLICIES := .build/weaver-policies
LOCAL_POLICY_STAMP := $(LOCAL_POLICIES)/.$(POLICY_REPO_REF)

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

.PHONY: check-policies schema-snapshot generate-docs package regenerate clean filter-upstream fix-external-links

# Pinned upstream GitHub URL base, used by fix-external-links to rewrite doc
# links that point at files living only in open-telemetry/semantic-conventions.
UPSTREAM_DOCS_BASE := https://github.com/open-telemetry/semantic-conventions/blob/$(SEMCONV_VERSION)

# Shared attribute namespaces imported from upstream that our generated docs
# reference. Any /docs/attributes/<ns>.md link for a namespace in this list is
# rewritten by fix-external-links to a pinned upstream GitHub URL so GitHub
# resolves it (Hugo renders it via registry_base_url). Expand when upstream
# shared namespaces start appearing in new generated snippets. Local namespaces
# (gen-ai, mcp, openai, aws via aws-bedrock) are listed as files under
# docs/attributes/ and stay relative.
UPSTREAM_ATTR_NAMESPACES := azure client error exception jsonrpc network rpc server

# Work around a Weaver 0.22.1 panic when `registry check` fetches a pinned remote
# policy pack by commit SHA. Keep the policy source pinned, but materialize it as
# a local checkout before running validation.
$(LOCAL_POLICY_STAMP): $(VERSION_PINS_FILE)
	@mkdir -p .build
	rm -rf $(LOCAL_POLICIES)
	git init -q $(LOCAL_POLICIES)
	cd $(LOCAL_POLICIES) && git remote add origin $(POLICY_REPO_URL)
	cd $(LOCAL_POLICIES) && git fetch --depth 1 origin $(POLICY_REPO_REF)
	cd $(LOCAL_POLICIES) && git checkout --detach FETCH_HEAD
	touch $(LOCAL_POLICY_STAMP)

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

# Validate the model and run shared policies
check-policies: $(LOCAL_POLICY_STAMP) $(SC_UPSTREAM_STAMP)
	$(WEAVER) registry check \
		-r ./model \
		--policy $(LOCAL_POLICIES)/policies/check

# Regenerate everything Weaver owns under docs/:
#   1. The attribute registry pages under docs/registry/ (full directory of
#      generated markdown).
#   2. The semconv snippet tables embedded in hand-written signal docs under
#      docs/gen-ai/ (refreshed in place between <!-- semconv ... --> markers).
# Depends on $(SC_UPSTREAM_STAMP) so the upstream dependency Weaver sees has
# the groups we redefine locally stripped out -- see the long comment on
# SC_UPSTREAM_FILTERED above.
generate-docs: $(SC_UPSTREAM_STAMP)
	$(WEAVER) registry generate \
		-r ./model \
		--templates $(TEMPLATE_REPO) \
		markdown \
		docs/registry
	$(WEAVER) registry update-markdown \
		-r ./model \
		--templates $(TEMPLATE_REPO) \
		--target markdown \
		--param registry_base_url=/docs/registry/ \
		docs
	$(MAKE) --no-print-directory fix-external-links

# Rewrite absolute /docs/... links in generated docs so they resolve on GitHub.
#
# The upstream markdown templates were designed for the monorepo case where
# every linked page lives under the same repo. Downstream registries like this
# one import attributes from open-telemetry/semantic-conventions but host local
# pages only for the namespaces they own (gen-ai, mcp, openai), so links to
# shared namespaces (server, error, network, ...) and to upstream-only prose
# pages (/docs/general/recording-errors.md, /docs/general/metric-requirement-level.md)
# 404 on GitHub as generated. Hugo-side rendering is unaffected.
#
# This is a short-lived bridge. The real fix lives in the new shared markdown
# template package being designed in open-telemetry/opentelemetry-weaver-packages
# (see PR #29 and successors), which will expose parameters for cross-registry
# link bases and handle empty registries (no entities, no metrics, etc.) by
# construction. Remove this target, its callers, UPSTREAM_ATTR_NAMESPACES, and
# UPSTREAM_DOCS_BASE once TEMPLATE_REPO is migrated to the packages repo and
# exposes the new params.
fix-external-links:
	@# 1. Non-local attribute namespaces -> pinned upstream registry pages.
	@#    Paths on both sides are the same (docs/registry/attributes/<ns>.md),
	@#    so only the host prefix is prepended.
	@ns_alt=$$(echo "$(UPSTREAM_ATTR_NAMESPACES)" | tr ' ' '|'); \
	find docs -type f -name '*.md' -print0 | xargs -0 sed -Ei \
		-e "s#\]\(/docs/registry/attributes/($$ns_alt)\.md#](%$(UPSTREAM_DOCS_BASE)/docs/registry/attributes/\1.md#g"
	@# 2. Upstream-only prose pages under /docs/general/ (inline + ref-style links).
	@find docs -type f -name '*.md' -print0 | xargs -0 sed -Ei \
		-e 's#\]\(/docs/general/#](%$(UPSTREAM_DOCS_BASE)/docs/general/#g' \
		-e 's#\]:[[:space:]]*/docs/general/#]: $(UPSTREAM_DOCS_BASE)/docs/general/#g'
	@# 3. Hand-authored cross-repo links in docs/gen-ai/mcp.md pointing at upstream RPC docs.
	@find docs -type f -name '*.md' -print0 | xargs -0 sed -Ei \
		-e 's#\]\(/docs/rpc/#](%$(UPSTREAM_DOCS_BASE)/docs/rpc/#g'
	@# 4. Collapse the `](%URL` sentinel introduced above into a clean `](URL`
	@#    (the `%` keeps intermediate passes idempotent so re-running this target
	@#    does not double-rewrite already-external URLs).
	@find docs -type f -name '*.md' -print0 | xargs -0 sed -Ei \
		-e 's#\]\(%https://#](https://#g'
	@# 5. Strip the dangling entities link from docs/registry/README.md (no
	@#    type: entity groups in this registry, so Weaver emits no entities/
	@#    directory under docs/registry/).
	@#    Becomes unnecessary once TEMPLATE_REPO migrates to the packages repo,
	@#    whose v2 registry README template is expected to omit the link when
	@#    no entities are defined.
	@if [ ! -d docs/registry/entities ] && [ -f docs/registry/README.md ]; then \
		awk '!/^- \[Entities\]\(entities\/README\.md\)$$/' docs/registry/README.md > docs/registry/README.md.tmp && \
		mv docs/registry/README.md.tmp docs/registry/README.md; \
	fi

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
regenerate: package schema-snapshot generate-docs

# Render the resolved registry as a single YAML file under ./schema-snapshot.
# The output is committed; CI runs `make schema-snapshot` and fails if the working
# tree differs, so reviewers can see schema-level changes in PR diffs.
schema-snapshot: $(SC_UPSTREAM_STAMP)
	$(WEAVER) registry generate \
		-r ./model \
		--v2 \
		-t ./internal/templates \
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
