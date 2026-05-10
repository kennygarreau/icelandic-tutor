FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn[standard]==0.30.1 \
    httpx==0.27.0 \
    pydantic==2.7.1 \
    opentelemetry-sdk==1.25.0 \
    opentelemetry-exporter-otlp-proto-http==1.25.0 \
    opentelemetry-instrumentation-fastapi==0.46b0 \
    opentelemetry-instrumentation-httpx==0.46b0 \
    prometheus-fastapi-instrumentator==7.0.0

RUN mkdir -p /data
COPY telemetry.py .
COPY main.py .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
