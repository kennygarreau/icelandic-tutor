# Sigríður — Icelandic Language Tutor

A fully self-hosted, voice-enabled Icelandic learning assistant.  
Powered by **Whisper STT**, **Piper TTS**, **ChromaDB RAG**, and your choice of **Ollama (local LLM)** or **Claude API**.

```
You speak Icelandic → Whisper (GPU) → Backend → LLM + RAG context → Piper TTS → You hear Icelandic
                                           ↓
                            English corrections · pronunciation score
                            Flashcards · CEFR assessment · heatmap
                                           ↓
                           Visual flashcard → SD API (SDXL) → image
```

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for a full service/routing diagram.

---

## Hardware Targets

The ideal setup will provide a near-realtime experience for the user, and therefore the better the GPU hardware provided, the better the experience. Since there are multiple models in use, VRAM can be a constraint depending on the context length/LLM used.

An RTX 5080 was used for development.

---

## Quick Start

### 1. Prerequisites

- Docker & Docker Compose v2
- NVIDIA Container Toolkit (for GPU access)
- NVIDIA drivers ≥ 570

```bash
# Verify GPU access in Docker
docker run --rm --gpus all nvidia/cuda:12.9.2-base-ubuntu24.04 nvidia-smi
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

Install Ollama on the host running your GPU:
```bash
curl -fsSL https://ollama.ai/install.sh | sh

# mistral-nemo:12b fits comfortably in 16 GB VRAM (RTX 5080)
ollama pull mistral-nemo:12b

# Larger models if you have more VRAM (24 GB+)
ollama pull qwen3:32b
ollama pull qwen2.5:72b
```

Then in `.env`:
```
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=mistral-nemo:12b
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

Three card types:
- **Vocabulary** — word + translation + part of speech
- **Sentences** — phrase-level cards for situational practice
- **Visual** — concrete nouns paired with AI-generated images; review by looking at the image and recalling the Icelandic word

### CEFR Assessment
Estimates your current level (A1–C2) from practice history, or take a formal 20-question exam covering vocabulary, grammar, reading, and speaking. Full per-skill score breakdown and targeted recommendations.

---

## Visual Flashcards — Image Generation

Visual cards pair Icelandic nouns with photorealistic images. After the LLM generates a batch of visually concrete nouns, the backend calls an image provider to generate or fetch an image for each card. Images are stored in the `tutor_data` Docker volume and served from `/api/images/{card_id}`.

Two providers are supported, selected by `IMAGE_PROVIDER` in `.env`:

### Option A: Stable Diffusion (default)

Requires an [AUTOMATIC1111-compatible](https://github.com/AUTOMATIC1111/stable-diffusion-webui) API or the custom `diffusers`-based API included in the project. Recommended model: `stabilityai/stable-diffusion-xl-base-1.0`.

```
IMAGE_PROVIDER=sd
SD_URL=http://your-sd-host:7860
```

Tuning parameters (defaults shown):
```
SD_STEPS=25                 # inference steps — 25 for SDXL base; use 2-4 for SDXL Turbo
SD_CFG=7                    # guidance scale — 7 for SDXL base; use 1.5 for Turbo
SD_WIDTH=1024               # image width in pixels
SD_HEIGHT=1024              # image height
SD_PROMPT_TEMPLATE=...      # {word} is substituted with the English noun
SD_NEGATIVE_PROMPT=...      # negative prompt passed to the model
```

> **Note:** SDXL Turbo requires `SD_CFG=1.5` and `SD_STEPS=4`. Using the standard defaults with Turbo produces broken images because Turbo's distillation is incompatible with high guidance scales.

If you use the bundled `sd-api` service (under `~/sd-api` on the SD host), change the model by editing `SD_MODEL_ID` in its `docker-compose.yml` and restarting:
```bash
# On the SD host
cd ~/sd-api
# Edit docker-compose.yml: SD_MODEL_ID=stabilityai/stable-diffusion-xl-base-1.0
docker compose up -d --build
```

### Option B: Unsplash

Uses the [Unsplash API](https://unsplash.com/developers) for royalty-free photographs. No GPU required.

```
IMAGE_PROVIDER=unsplash
UNSPLASH_ACCESS_KEY=your_key_here
```

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
├── docs/
│   └── ARCHITECTURE.md      # Mermaid service/routing diagram
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
│       ├── App.jsx          # All views (Chat, Scenarios, Lessons, etc.)
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

**Visual flashcard images are wrong / all landscapes**
- SDXL Turbo ignores prompts at `SD_CFG=7`; set `SD_STEPS=4` and `SD_CFG=1.5` in `.env`
- If `SD_PROMPT_TEMPLATE` is set to an empty string in the environment, images are generated unconditionally — leave it unset to use the built-in default
- Use the refresh button on any card to regenerate its image after fixing config

**Visual flashcard images fail with 502**
- Confirm the SD service is running: `curl http://<SD_URL>/health`
- Check backend logs: `docker compose logs backend | grep image`
