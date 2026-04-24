# GenAI Semantic Conventions - Makefile
# Requires: weaver >= 0.23.0
# Install: https://github.com/open-telemetry/weaver/releases

# Shared external version pins. Override on the command line when needed, e.g.
# `make check SEMCONV_VERSION=v1.40.0`.
VERSION_PINS_FILE := versions.env
include $(VERSION_PINS_FILE)

# Templates from the main semconv repo for doc generation
TEMPLATE_REPO := https://github.com/open-telemetry/semantic-conventions.git@$(SEMCONV_VERSION)[templates]

# Clear Weaver's global vdir cache before each run so local results match CI.
# Weaver 0.22.1 stores fetched registries under dirs::home_dir(), which stays global
# across repos and invocations on Windows. When that cache is stale, `registry
# update-markdown` can resolve shared attributes against the wrong source group and
# rewrite links such as `gen_ai.operation.name`, `error.type`, `server.address`, and
# `server.port` to `/docs/attributes/attributes/event.md` instead of the correct
# `gen-ai.md`, `error.md`, or `server.md` targets. Clearing the cache removes that
# stale-link failure mode, but it does not fix the separate resolve instability below.
WEAVER_VDIR_CACHE := $(HOME)/.weaver/vdir_cache

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

.PHONY: check generate-docs update-markdown check-docs resolve clean clear-weaver-vdir-cache filter-upstream fix-external-links

# Pinned upstream GitHub URL base, used by fix-external-links to rewrite doc
# links that point at files living only in open-telemetry/semantic-conventions.
UPSTREAM_DOCS_BASE := https://github.com/open-telemetry/semantic-conventions/blob/$(SEMCONV_VERSION)

# Shared attribute namespaces imported from upstream that our generated docs
# reference. Any /docs/attributes/<ns>.md link for a namespace in this list is
# rewritten by fix-external-links to a pinned upstream GitHub URL so GitHub
# resolves it (Hugo renders it via registry_base_url). Expand when upstream
# shared namespaces start appearing in new generated snippets. Local namespaces
# (gen-ai, mcp, openai) are listed as files under docs/attributes/ and stay
# relative.
UPSTREAM_ATTR_NAMESPACES := aws azure client error exception jsonrpc network rpc server

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
	touch $(SC_UPSTREAM_STAMP)

filter-upstream: $(SC_UPSTREAM_STAMP)

clear-weaver-vdir-cache:
	rm -rf "$(WEAVER_VDIR_CACHE)"

# Validate the model and run shared policies
check: clear-weaver-vdir-cache $(LOCAL_POLICY_STAMP) $(SC_UPSTREAM_STAMP)
	weaver registry check \
		-r ./model \
		--policy $(LOCAL_POLICIES)/policies/check

# Generate registry docs (attributes, entities) under docs/registry/, matching
# the layout used by open-telemetry/semantic-conventions.
generate-docs: clear-weaver-vdir-cache $(SC_UPSTREAM_STAMP)
	weaver registry generate \
		-r ./model \
		--templates $(TEMPLATE_REPO) \
		markdown \
		docs/registry
	$(MAKE) --no-print-directory fix-external-links

# Update semconv snippet tables in hand-written signal docs (spans, metrics, events).
# Depends on $(SC_UPSTREAM_STAMP) so the upstream dependency Weaver sees has the
# groups we redefine locally stripped out -- see the long comment on
# SC_UPSTREAM_FILTERED above.
update-markdown: clear-weaver-vdir-cache $(SC_UPSTREAM_STAMP)
	weaver registry update-markdown \
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

# Verify deterministic generated docs are in sync with model.
check-docs: generate-docs
	@if ! git diff --exit-code -- ./docs/registry; then \
		echo "FAIL: Generated registry documentation is out of sync!"; \
		echo "Run 'make generate-docs' and commit the changes."; \
		exit 1; \
	fi
	@echo "OK: Deterministic generated documentation is up to date"

# Output the resolved schema (useful for debugging)
resolve: clear-weaver-vdir-cache $(SC_UPSTREAM_STAMP)
	weaver registry generate \
		-r ./model \
		--skip-policies \
		-f yaml

# Remove generated docs, the local .build/ tree (Weaver-fetched templates/policies
# plus any hand-created weaver-min-repro* dirs), reference-project caches, and
# Python bytecode trees under the entire repo.
#
# `clean` does NOT touch `reference/.venv`; rebuilding it requires a fresh
# `uv sync` which re-downloads every tooling dependency. Remove it manually
# (`rm -rf reference/.venv`) for a full reset.
clean:
	rm -rf docs/registry
	rm -rf .build
	rm -rf reference/.cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
	find . -type d -name .ruff_cache -prune -exec rm -rf {} +
