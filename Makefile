.DEFAULT_GOAL := help

# Every target below runs through .venv explicitly, so `make test`, `make api`, etc.
# behave the same regardless of what's active on your shell's PATH (conda, system
# Python, nothing). Run `make install` once to create .venv and populate it.
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

$(PYTHON):
	python3 -m venv $(VENV)

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: $(PYTHON) ## Create .venv if it doesn't exist yet

install: venv ## Create .venv (if needed) and install the package with dev and ui extras
	$(PIP) install -e ".[dev,ui]"

databases: ## Download the PHREEQC thermodynamic databases into ops/phreeqc-databases
	./ops/fetch_databases.sh

test: venv ## Run the test suite
	HGC_ENV=test $(VENV)/bin/pytest

lint: venv ## Ruff + mypy
	$(VENV)/bin/ruff check src tests && $(VENV)/bin/ruff format --check src tests && $(VENV)/bin/mypy src

api: venv ## Run the API locally
	HGC_ENV=local $(VENV)/bin/uvicorn hgc.api.main:app --reload --app-dir src

worker: venv ## Run a Celery worker locally
	$(VENV)/bin/celery -A hgc.worker.celery_app.celery_app worker -Q runs,batches -c 2 --loglevel=info

ui: venv ## Run the Streamlit UI locally
	$(VENV)/bin/streamlit run src/hgc/ui/app.py

up: ## Start the full stack
	docker compose up --build

migrate: venv ## Apply database migrations
	$(VENV)/bin/alembic upgrade head

.PHONY: help venv install databases test lint api worker ui up migrate
