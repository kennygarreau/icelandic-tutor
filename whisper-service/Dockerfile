FROM nvidia/cuda:12.9.2-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app
RUN pip install --no-cache-dir \
    faster-whisper==1.2.1 \
    fastapi==0.136.1 \
    uvicorn[standard]==0.47.0 \
    python-multipart==0.0.29 \
    requests==2.34.2 \
    opentelemetry-sdk==1.42.1 \
    opentelemetry-exporter-otlp-proto-http==1.42.1 \
    opentelemetry-instrumentation-fastapi==0.63b1 \
    prometheus-fastapi-instrumentator==7.1.0

COPY telemetry.py .
COPY main.py .
EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
