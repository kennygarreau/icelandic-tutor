# Sigríður — Icelandic Language Tutor

A fully self-hosted, voice-enabled Icelandic learning assistant.  
Powered by **Whisper STT**, **Piper TTS**, **ChromaDB RAG**, and your choice of **Ollama (local LLM)** or **Claude API**.

```
You speak Icelandic → Whisper (GPU) → Backend → LLM + RAG context → Piper TTS → You hear Icelandic
                                           ↓
                            English corrections · pronunciation score
                            Flashcards · CEFR assessment · heatmap
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for a full service/routing diagram.

---

## Hardware Targets

| Service     | Recommended device         | Notes                              |
|-------------|----------------------------|------------------------------------|
| Whisper STT | RTX 5080                   | `large-v3-turbo`, near real-time   |
| LLM         | DGX Spark × 2 (via Ollama) | qwen3:32b or larger                |
| Piper TTS   | CPU or any GPU             | Icelandic `is_IS-bui-medium` voice |
| Frontend    | Any                        | Served via Nginx                   |

---

## Quick Start

### 1. Prerequisites

- Docker & Docker Compose v2
- NVIDIA Container Toolkit (for GPU access)
- NVIDIA drivers ≥ 550

```bash
# Verify GPU access in Docker
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — set LLM_PROVIDER and either ANTHROPIC_API_KEY or OLLAMA_BASE_URL
```

### 3. Launch

```bash
# Build and start all services
docker compose up --build -d

# Watch logs
docker compose logs -f

# Access the app (HTTPS)
open https://localhost:8843
# or HTTP
open http://localhost:8888
```

---

## LLM Backends

### Option A: Ollama (recommended — fully offline)

Install Ollama on your inference machine:
```bash
curl -fsSL https://ollama.ai/install.sh | sh

# Pull a model (qwen3:32b fits comfortably on a single DGX Spark)
ollama pull qwen3:32b

# Larger options (requires 2× DGX Spark or similar)
ollama pull qwen2.5:72b
ollama pull llama3.3:70b
```

Then in `.env`:
```
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://192.168.1.50:11434
OLLAMA_MODEL=qwen3:32b
SPARK_IP=192.168.1.50
```

### Option B: Anthropic Claude API

```
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Services & Ports

| Service   | Internal port | External port      | Description                        |
|-----------|---------------|--------------------|------------------------------------|
| nginx     | 80 / 443      | **8888** / **8843**| Reverse proxy — use this           |
| frontend  | 80            | —                  | React UI (served through nginx)    |
| backend   | 8000          | 8000               | FastAPI orchestrator               |
| whisper   | 8001          | 8001               | STT + pronunciation scoring        |
| tts       | 8002          | 8002               | TTS (Piper Icelandic)              |
| rag       | 8004          | 8004               | RAG service (ChromaDB)             |

---

## Features

### Chat
- Converse with Sigríður in Icelandic at Beginner / Intermediate / Advanced level
- Type or hold the **mic button** to speak (WebM audio → Whisper STT)
- Responses auto-played via Piper TTS with adjustable speed
- Per-message **pronunciation score** with word-level breakdown
- Toggle **English translation** on any assistant message
- **Word of the Day** banner with etymology and example sentence

### Scenarios
Role-play real-life situations (travel, food, shopping, health, emergencies, etc.) selected from a curated library.

### Lessons
Structured curriculum with beginner → intermediate → advanced tracks. Progress is gated — complete each lesson to unlock the next.

### Heatmap
Visual breakdown of every grammar mistake made across all sessions, grouped by error category with heat intensity. AI-generated pattern analysis and recommended focus areas.

### Progress
Daily practice chart, session counts, active-day streak, and flashcard due/total summary.

### Flashcards
Spaced-repetition card review (SM-2 scheduling). Browse, add manually, or **generate cards with AI** by topic and level. Each card is pronounceable via TTS.

### CEFR Assessment
Estimates your current level (A1–C2) from practice history, or take a formal 20-question exam covering vocabulary, grammar, reading, and speaking. Full per-skill score breakdown and targeted recommendations.

---

## RAG (Retrieval-Augmented Generation)

The `rag-service` embeds Icelandic grammar PDFs into a ChromaDB vector store and injects relevant context into every chat turn. This keeps grammar explanations grounded in source material rather than relying on LLM parametric knowledge alone.

**To add documents:**
```bash
# Place PDFs in rag-service/pdfs/
cp my-grammar-book.pdf rag-service/pdfs/

# Rebuild the RAG container to re-ingest
docker compose up --build -d rag
```

The service uses `intfloat/multilingual-e5-small` for embeddings (runs on CPU, no GPU needed).

---

## Upgrading the TTS Voice

The default voice is `is_IS-bui-medium`. To try other Icelandic voices:

```bash
# Edit .env
TTS_VOICE=is_IS-salka-medium

docker compose up --build tts -d
```

---

## Running Whisper on CPU (no GPU)

Edit `.env`:
```
WHISPER_DEVICE=cpu
WHISPER_COMPUTE=int8
WHISPER_MODEL=medium
```

Remove the `deploy.resources` GPU block from `docker-compose.yml` for the whisper service.

---

## Project Structure

```
icelandic-tutor/
├── docker-compose.yml
├── .env.example
├── ARCHITECTURE.md          # Mermaid service/routing diagram
├── backend/
│   ├── Dockerfile
│   └── main.py              # FastAPI orchestrator
├── whisper-service/
│   ├── Dockerfile
│   └── main.py              # STT + pronunciation scoring
├── tts-service/
│   ├── Dockerfile
│   └── main.py              # Piper TTS
├── rag-service/
│   ├── Dockerfile
│   ├── main.py              # ChromaDB RAG API
│   └── pdfs/                # Grammar PDFs ingested at startup
├── frontend/
│   ├── Dockerfile
│   └── src/
│       ├── App.js           # All views (Chat, Scenarios, Lessons, etc.)
│       └── App.css          # Norse/aurora aesthetic, mobile-responsive
└── nginx/
    └── nginx.conf           # Reverse proxy + TLS
```

---

## Troubleshooting

**502 Bad Gateway after rebuilding a service**
- Nginx caches container IPs at startup. After any `docker compose up --build <service>`, reload nginx:
  ```bash
  docker exec icelandic_nginx nginx -s reload
  ```

**Whisper takes too long**
- Switch to `WHISPER_MODEL=medium` for faster (slightly less accurate) results
- Confirm GPU is active: `docker compose logs whisper | grep device`

**No Icelandic voice / TTS silent**
- Check TTS logs: `docker compose logs tts`
- Voice model downloads at build time — rebuild if it failed: `docker compose build tts`

**LLM responses in English only**
- Ensure your Ollama model supports Icelandic (qwen3, Qwen2.5, and Llama3.3 all do)
- Try the Anthropic backend as a comparison

**Mic not working**
- Browsers require HTTPS for mic access except on `localhost`
- For remote access use the HTTPS port (8843) with a valid or self-signed cert, or tunnel via Tailscale

**RAG returning irrelevant context**
- Check what's been ingested: `curl http://localhost:8004/sources`
- Delete and re-ingest a source: `curl -X DELETE http://localhost:8004/source/<filename>`
