FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 INCIDENT_SERVICE_DIR=/state
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
COPY eval ./eval
RUN pip install --no-cache-dir -e . && useradd -m svc && mkdir /state && chown svc /state
USER svc
VOLUME /state
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"
ENTRYPOINT ["python", "-m", "incident_agent.service", "serve", "--host", "0.0.0.0", "--allow-remote"]
