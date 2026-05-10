FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip3 install --no-cache-dir \
    faster-whisper==1.0.3 \
    fastapi==0.111.0 \
    uvicorn[standard]==0.30.1 \
    python-multipart==0.0.9 \
    requests==2.31.0 \
    opentelemetry-sdk==1.25.0 \
    opentelemetry-exporter-otlp-proto-http==1.25.0 \
    opentelemetry-instrumentation-fastapi==0.46b0 \
    prometheus-fastapi-instrumentator==7.0.0

COPY telemetry.py .
COPY main.py .
EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
