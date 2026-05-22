FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir \
    fastapi==0.136.1 \
    uvicorn[standard]==0.47.0 \
    httpx==0.28.1 \
    pydantic==2.13.4 \
    python-multipart==0.0.29 \
    opentelemetry-sdk==1.42.1 \
    opentelemetry-exporter-otlp-proto-http==1.42.1 \
    opentelemetry-instrumentation-fastapi==0.63b1 \
    opentelemetry-instrumentation-httpx==0.63b1 \
    prometheus-fastapi-instrumentator==7.1.0

RUN mkdir -p /data
COPY telemetry.py .
COPY main.py .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
