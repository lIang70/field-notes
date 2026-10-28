# Markdown notes: formatting/convention checks + the published site.
# The check targets are a thin wrapper over scripts/format.py (stdlib);
# the site targets are a thin wrapper over web/build.py (Zensical), and all
# web code/config lives under web/.

PYTHON ?= python3
FORMAT := scripts/format.py

.DEFAULT_GOAL := help
.PHONY: help format format-check format-strict check stats site site-check serve clean

help: ## list targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

format: ## fix whitespace, line endings, blank lines, fence tags in place
	@$(PYTHON) $(FORMAT) --quiet

format-check: ## report only; exit 1 on errors (fences, broken links)
	@$(PYTHON) $(FORMAT) --check

format-strict: ## report only; warnings (emoji, `---`, heading jumps) also fail
	@$(PYTHON) $(FORMAT) --check --strict

check: format-check ## alias for format-check

stats: ## note count per area, to keep the README header honest
	@$(PYTHON) web/build.py --stats

# ── published site (all web code lives in web/) ──

site-src: ## install/refresh the site toolchain into web/.venv
	@test -d web/.venv || $(PYTHON) -m venv web/.venv
	@web/.venv/bin/pip install -q -r web/requirements.txt

site: site-src ## build the static site into web/.build/site
	@web/.venv/bin/python web/build.py

site-check: site-src ## build with --strict: fails on broken links / bad anchors
	@web/.venv/bin/python web/build.py --strict

serve: site-src ## live preview on http://127.0.0.1:8000
	@web/.venv/bin/python web/build.py --serve

clean: ## drop build output
	@rm -rf web/.build
