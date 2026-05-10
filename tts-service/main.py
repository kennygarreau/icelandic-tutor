"""
Piper TTS Service
Accepts Icelandic text, returns synthesized WAV audio.
"""
import io
import os
import time
import wave
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from piper.voice import PiperVoice
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Histogram
from telemetry import setup_tracing

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

tracer = setup_tracing("icelandic-tutor-tts")

TTS_DURATION = Histogram(
    "tts_synthesize_duration_seconds", "Piper TTS synthesis duration",
    buckets=[.05, .1, .25, .5, 1, 2, 5])
TTS_CHARS = Histogram(
    "tts_text_length_chars", "Characters synthesized per request",
    buckets=[10, 25, 50, 100, 200, 400, 800])

VOICE_MODEL = os.getenv("VOICE_MODEL", "is_IS-bui-medium")
MODEL_PATH  = f"/app/models/{VOICE_MODEL}.onnx"
CONFIG_PATH = f"/app/models/{VOICE_MODEL}.onnx.json"

logger.info(f"Loading Piper voice: {VOICE_MODEL}")
voice = PiperVoice.load(MODEL_PATH, config_path=CONFIG_PATH, use_cuda=False)
logger.info("Piper TTS ready.")

app = FastAPI(title="Piper TTS Service")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)


class TTSRequest(BaseModel):
    text: str
    speed: float = 1.0     # 0.5 – 2.0


@app.get("/health")
def health():
    return {"status": "ok", "voice": VOICE_MODEL}


@app.post("/synthesize")
def synthesize(req: TTSRequest):
    """
    POST /synthesize
    { "text": "Halló, hvernig hefur þú það?", "speed": 1.0 }
    Returns: audio/wav stream
    """
    if not req.text.strip():
        raise HTTPException(400, "text must not be empty")

    with tracer.start_as_current_span("tts.synthesize") as span:
        span.set_attribute("tts.char_count", len(req.text))
        span.set_attribute("tts.speed", req.speed)
        TTS_CHARS.observe(len(req.text))
        t0 = time.perf_counter()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize(req.text, wav_file, length_scale=1.0 / req.speed)
        TTS_DURATION.observe(time.perf_counter() - t0)

    buf.seek(0)
    return StreamingResponse(buf, media_type="audio/wav",
                              headers={"Content-Disposition": "inline; filename=speech.wav"})
