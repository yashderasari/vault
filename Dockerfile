FROM python:3.11-slim

RUN useradd -m -u 1000 vault

WORKDIR /app

COPY pyproject.toml ./
COPY server.py ./

RUN pip install --no-cache-dir .

USER vault

ENV VAULT_LOG_LEVEL=INFO
ENV VAULT_RATE_LIMIT_PER_MINUTE=60

EXPOSE 8000

CMD ["python", "server.py", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
