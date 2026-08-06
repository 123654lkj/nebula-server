FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p data vault/notes \
  && chmod +x install.sh start.sh scripts/*.sh scripts/*.py 2>/dev/null || true

ENV NEBULA_HOST=0.0.0.0 \
    NEBULA_PORT=26670 \
    NEBULA_DB_PATH=/app/data/memory_vectors.db \
    VAULT_ROOT=/app/vault/notes \
    PYTHONUNBUFFERED=1

EXPOSE 26670
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf http://127.0.0.1:26670/v5/health || exit 1

CMD ["python", "vector_memory_server.py"]
