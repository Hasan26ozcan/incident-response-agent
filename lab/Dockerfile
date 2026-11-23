FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY src/incident_agent/lab/app.py /app/lab_app.py
# stdlib only: no pip install needed. Non-root user.
RUN useradd -m lab && mkdir /logs && chown lab /logs
USER lab
ENTRYPOINT ["python", "/app/lab_app.py"]
