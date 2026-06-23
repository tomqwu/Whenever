# Whenever — developer task runner.
# Run `make` (or `make help`) to list targets.

.DEFAULT_GOAL := help

VENV   := .venv
PYTHON := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

# Coverage scope — keep in sync with .github/workflows/ci.yml
COV  := --cov=app --cov=watch --cov=scheduler --cov=export
GATE := $(COV) --cov-report=term-missing --cov-fail-under=99

.PHONY: help install test test-unit test-e2e cov ci run scheduler clean

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(VENV): ## Create the virtualenv
	python3 -m venv $(VENV)

install: $(VENV) ## Install runtime + dev deps and the Playwright browser
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt
	$(PYTHON) -m playwright install --with-deps chromium

test: ## Run the full suite with the 99% coverage gate (same as CI)
	$(PYTEST) $(GATE)

test-unit: ## Run unit tests only
	$(PYTEST) tests/unit -v

test-e2e: ## Run the Playwright e2e tests only
	$(PYTEST) tests/e2e -v

cov: ## Full suite + HTML coverage report in htmlcov/
	$(PYTEST) $(COV) --cov-report=html --cov-report=term-missing --cov-fail-under=99

ci: test ## Alias for the CI gate (what GitHub Actions runs)

run: ## Run the Flask web app (http://localhost:5000)
	$(PYTHON) app.py

scheduler: ## Run the price-watch scheduler once (re-prices saved watches)
	$(PYTHON) scheduler.py

clean: ## Remove caches, coverage artifacts, and the local watch DB
	rm -rf .pytest_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -f whenever_watches.db whenever_watches.db-wal whenever_watches.db-shm
