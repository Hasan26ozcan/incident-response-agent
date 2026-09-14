FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir ruff bandit mypy pytest

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
RUN ruff check data/ eval/ tests/ --exclude .venv && \
    mypy data/ eval/ tests/ --ignore-missing-imports && \
    bandit -r data/ eval/ tests/ --configfile bandit.yml && \
    pytest tests/ -v

CMD ["python", "-m", "pytest", "tests/", "-v"]
