FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip3 install --no-cache-dir \
    faster-whisper==1.2.1 \
    fastapi==0.136.1 \
    uvicorn[standard]==0.47.0 \
    python-multipart==0.0.29 \
    requests==2.34.2

COPY main.py .
EXPOSE 8003
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
