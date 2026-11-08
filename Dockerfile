FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/gateway
COPY gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home tollgate
COPY --chown=tollgate:tollgate gateway/ /srv/gateway/
RUN mkdir -p /srv/gateway/data && chown tollgate:tollgate /srv/gateway/data
USER 10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--limit-concurrency", "100", "--timeout-graceful-shutdown", "200"]
