FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir ruff bandit mypy pytest pytest-cov

# Install trivy for image vulnerability scanning
RUN curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin

# Copy project files
COPY pyproject.toml bandit.yml ./
COPY data/ ./data/
COPY eval/ ./eval/
COPY tests/ ./tests/

# Verify the dataset generator and validator work
RUN python data/generate_dataset.py && \
    python eval/validate_dataset.py && \
    python eval/generate_gold_set_md.py

# Run quality checks
RUN ruff check . && \
    ruff format --check . && \
    mypy . --ignore-missing-imports && \
    bandit -c bandit.yml -r src && \
    pytest tests/ -v

CMD ["python", "-m", "pytest", "tests/", "-v"]
