# Markdown notes: formatting and convention checks.
# Everything here is a thin wrapper over scripts/format.py (Python 3 stdlib).

PYTHON ?= python3
FORMAT := scripts/format.py
AREAS  := go rust os ai

.DEFAULT_GOAL := help
.PHONY: help format format-check format-strict check stats

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
	@for d in $(AREAS); do printf '  %-4s %s\n' $$d "$$(find $$d -name '*.md' | wc -l | tr -d ' ')"; done
	@printf '  %-4s %s\n' total "$$(find $(AREAS) -name '*.md' | wc -l | tr -d ' ')"
