.PHONY: install lint format format-check typecheck security test validate-data \
        regen-data ci sandbox-build sandbox-shell clean

# Mirrors .github/workflows/ci.yml exactly — `make ci` should be the same
# pass/fail signal you'll get from CI, so failures are caught locally
# before a push, not after.

install:  ## Install the project with dev dependencies into the active venv
	pip install -e ".[dev]"

lint:  ## Lint (ruff check)
	ruff check .

format:  ## Auto-format (ruff format)
	ruff format .

format-check:  ## Check formatting without modifying files
	ruff format --check .

typecheck:  ## Static type check (mypy)
	mypy

security:  ## Security scan (bandit)
	bandit -c bandit.yml -r src

trivy-scan: sandbox-build  ## Scan sandbox image for vulnerabilities (trivy)
	trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed incident-agent-sandbox:local

trivy-fs:  ## Scan project filesystem for vulnerabilities (trivy)
	trivy fs --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed docker/

test:  ## Run tests with coverage
	pytest --cov=incident_agent --cov-report=term-missing

regen-data:  ## Regenerate the Stage 1 dataset and its human-readable summary
	cd data && python3 generate_dataset.py
	python3 eval/generate_gold_set_md.py

validate-data:  ## Validate dataset integrity (Stage 1 regression check)
	python3 eval/validate_dataset.py

ci: lint format-check security typecheck test validate-data trivy-scan  ## Run the full local CI suite

sandbox-build:  ## Build the Stage 2 sandbox image
	docker build -t incident-agent-sandbox:local -f docker/sandbox/Dockerfile docker/sandbox

sandbox-shell: sandbox-build  ## Interactive shell in the isolated, resource-limited sandbox
	docker run --rm -it \
		--network none \
		--read-only \
		--memory 256m \
		--cpus 0.5 \
		--user sandbox \
		incident-agent-sandbox:local

clean:  ## Remove build/test artifacts
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
