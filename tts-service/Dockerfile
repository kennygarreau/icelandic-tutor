FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl wget espeak-ng libespeak-ng1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir \
    piper-tts==1.2.0 \
    fastapi==0.136.1 \
    uvicorn[standard]==0.47.0 \
    opentelemetry-sdk==1.42.1 \
    opentelemetry-exporter-otlp-proto-http==1.42.1 \
    opentelemetry-instrumentation-fastapi==0.63b1 \
    prometheus-fastapi-instrumentator==7.1.0

# Download Icelandic Piper voice model at build time
RUN mkdir -p /app/models && \
    wget -q -O /app/models/is_IS-bui-medium.onnx \
      "https://huggingface.co/rhasspy/piper-voices/resolve/main/is/is_IS/bui/medium/is_IS-bui-medium.onnx" && \
    wget -q -O /app/models/is_IS-bui-medium.onnx.json \
      "https://huggingface.co/rhasspy/piper-voices/resolve/main/is/is_IS/bui/medium/is_IS-bui-medium.onnx.json"

COPY telemetry.py .
COPY main.py .
EXPOSE 8002
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
