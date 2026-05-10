"""
Whisper STT + Pronunciation Scoring Service
Accepts audio uploads, returns Icelandic transcription (/transcribe)
or per-word pronunciation scores (/score).
Uses faster-whisper for GPU-accelerated inference — one model load for both.
"""
import os
import time
import tempfile
import logging
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from faster_whisper import WhisperModel
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Histogram
from telemetry import setup_tracing

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

tracer = setup_tracing("icelandic-tutor-whisper")

TRANSCRIBE_DURATION = Histogram(
    "whisper_transcribe_duration_seconds", "Whisper STT inference duration",
    buckets=[.1, .25, .5, 1, 2, 5, 10])
SCORE_DURATION = Histogram(
    "whisper_score_duration_seconds", "Pronunciation scoring duration",
    buckets=[.05, .1, .25, .5, 1, 2])

MODEL_SIZE   = os.getenv("MODEL_SIZE",   "large-v3")
DEVICE       = os.getenv("DEVICE",       "cuda")
COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "float16")

logger.info(f"Loading Whisper {MODEL_SIZE} on {DEVICE} ({COMPUTE_TYPE})…")
model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
logger.info("Whisper model ready (STT + pronunciation scoring).")

app = FastAPI(title="Whisper STT + Pronunciation Service")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)

# ═══════════════════════════════════════════════════════════════════════════════
# STT
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_SIZE, "device": DEVICE}


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str = "is",
    task: str = "transcribe",
):
    """
    POST /transcribe
    - audio: audio file (webm, wav, mp3, ogg, …)
    - language: BCP-47 code (default "is" for Icelandic)
    - task: "transcribe" keeps original language, "translate" → English
    """
    if not audio.content_type.startswith("audio/"):
        raise HTTPException(400, "File must be an audio type")

    data = await audio.read()
    if len(data) < 1000:
        raise HTTPException(400, "Audio too short or empty")

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        with tracer.start_as_current_span("whisper.transcribe") as span:
            span.set_attribute("whisper.language", language)
            span.set_attribute("whisper.task", task)
            t0 = time.perf_counter()
            try:
                segments, info = model.transcribe(
                    tmp_path,
                    language=language,
                    task=task,
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )
            except Exception as e:
                if "End of file" in str(e) or "EOFError" in type(e).__name__:
                    raise HTTPException(400, "Audio too short to transcribe — hold the button longer")
                raise
            TRANSCRIBE_DURATION.observe(time.perf_counter() - t0)
            raw_segments = [s.text.strip() for s in segments]
        # Deduplicate consecutive repeated segments
        deduped = []
        for seg in raw_segments:
            if not deduped or seg.lower() != deduped[-1].lower():
                deduped.append(seg)
        text = " ".join(deduped)
        return {
            "text": text,
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
        }
    finally:
        os.unlink(tmp_path)


# ═══════════════════════════════════════════════════════════════════════════════
# PRONUNCIATION SCORING
# ═══════════════════════════════════════════════════════════════════════════════

# Icelandic graphemes → IPA-like English hints
PHONEME_HINTS = {
    "þ":  ("th", "th in 'think'"),
    "ð":  ("dh", "th in 'this'"),
    "æ":  ("ae", "like 'eye'"),
    "ö":  ("oe", "u in 'burn'"),
    "á":  ("ow", "ow in 'cow'"),
    "é":  ("ye", "like 'ye'"),
    "í":  ("ee", "ee in 'see'"),
    "ý":  ("ee", "ee in 'see'"),
    "ú":  ("oo", "oo in 'moon'"),
    "ó":  ("oh", "oh in 'oh'"),
    "ll": ("tl", "like 'tl' (Icelandic lateral)"),
    "rl": ("rtl","like 'rdl'"),
    "hv": ("kv", "like 'kv'"),
    "fn": ("bn", "like 'bn'"),
    "gi": ("ji", "like 'yi'"),
    "gg": ("kk", "like double 'k'"),
    "nn": ("dn", "like 'dn' before i/j"),
}

COMMON_ERRORS = {
    "þ": "Often mispronounced as 't' or 's'. Tip: place tongue between teeth like 'th' in 'think'.",
    "ð": "Often mispronounced as 'd'. Tip: voiced 'th' as in 'this'.",
    "æ": "Often mispronounced as 'ay'. It's closer to the 'i' in 'bike'.",
    "ll": "Icelandic 'll' is a lateral fricative — sounds like 'tl' with a hiss.",
    "hv": "Pronounced 'kv' in modern Icelandic, not 'wh'.",
    "r":  "Icelandic 'r' is rolled/trilled, not the English 'r'.",
}


def _normalize(text: str) -> str:
    return text.lower().strip().rstrip(".,!?;:")


def _tokenize(text: str) -> list[str]:
    return [_normalize(w) for w in text.split() if w.strip()]


def _levenshtein(a: str, b: str) -> int:
    if not a: return len(b)
    if not b: return len(a)
    dp = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        ndp = [i + 1]
        for j, cb in enumerate(b):
            ndp.append(min(dp[j] + (0 if ca == cb else 1), dp[j+1] + 1, ndp[j] + 1))
        dp = ndp
    return dp[-1]


def _word_similarity(a: str, b: str) -> float:
    if not a or not b: return 0.0
    dist = _levenshtein(a, b)
    return max(0.0, 1.0 - dist / max(len(a), len(b)))


def _find_phoneme_issues(expected: str, spoken: str) -> list[dict]:
    issues = []
    exp_lower = expected.lower()
    for grapheme, (_, hint) in PHONEME_HINTS.items():
        if grapheme in exp_lower:
            sim = _word_similarity(_normalize(expected), _normalize(spoken))
            if sim < 0.75 and grapheme in COMMON_ERRORS:
                issues.append({
                    "grapheme": grapheme,
                    "hint": hint,
                    "tip": COMMON_ERRORS[grapheme],
                })
    return issues


def _score_word(expected: str, spoken: str, whisper_prob: float) -> dict:
    sim = _word_similarity(_normalize(expected), _normalize(spoken))
    score = round((sim * 0.6 + whisper_prob * 0.4) * 100)
    issues = _find_phoneme_issues(expected, spoken) if sim < 0.85 else []
    return {
        "expected": expected,
        "spoken":   spoken,
        "score":    score,
        "similarity": round(sim, 3),
        "whisper_confidence": round(whisper_prob, 3),
        "issues": issues,
        "status": "good" if score >= 80 else "fair" if score >= 55 else "needs_work",
    }


@app.post("/score")
async def score_pronunciation(
    audio: UploadFile = File(...),
    expected_text: str = Form(default=""),
):
    """
    POST /score
    - audio: webm/wav audio of the student speaking
    - expected_text: the Icelandic text they were trying to pronounce

    Returns per-word scores, phoneme issues, and an overall score.
    """
    if not audio.content_type.startswith("audio/"):
        raise HTTPException(400, "File must be audio")

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name

    try:
        with tracer.start_as_current_span("whisper.score") as span:
            span.set_attribute("whisper.expected_text", expected_text[:200])
            t0 = time.perf_counter()
            segments, info = model.transcribe(
                tmp_path,
                language="is",
                word_timestamps=True,
                beam_size=5,
                vad_filter=True,
            )
            SCORE_DURATION.observe(time.perf_counter() - t0)

        spoken_words = []
        for seg in segments:
            if seg.words:
                for w in seg.words:
                    spoken_words.append({
                        "word":        w.word.strip(),
                        "start":       round(w.start, 2),
                        "end":         round(w.end, 2),
                        "probability": round(w.probability, 3),
                    })

        spoken_text = " ".join(w["word"] for w in spoken_words)

        expected_tokens = _tokenize(expected_text) if expected_text else []

        word_scores = []
        if expected_tokens:
            s_idx = 0
            for e_tok in expected_tokens:
                if s_idx < len(spoken_words):
                    sw = spoken_words[s_idx]
                    ws = _score_word(e_tok, _normalize(sw["word"]), sw["probability"])
                    ws["timing"] = {"start": sw["start"], "end": sw["end"]}
                    word_scores.append(ws)
                    s_idx += 1
                else:
                    word_scores.append({
                        "expected": e_tok, "spoken": "", "score": 0,
                        "similarity": 0, "whisper_confidence": 0,
                        "issues": [], "status": "missing",
                    })

        if word_scores:
            overall = round(sum(w["score"] for w in word_scores) / len(word_scores))
        elif spoken_words:
            overall = round(sum(w["probability"] for w in spoken_words) / len(spoken_words) * 100)
        else:
            overall = 0

        needs_work = [w for w in word_scores if w["status"] == "needs_work"]
        all_issues = []
        for w in needs_work:
            all_issues.extend(w.get("issues", []))
        seen_tips: set[str] = set()
        unique_tips = []
        for iss in all_issues:
            if iss["tip"] not in seen_tips:
                seen_tips.add(iss["tip"])
                unique_tips.append(iss)

        grade = "Excellent! 🌟" if overall >= 90 else \
                "Good! 👍"      if overall >= 75 else \
                "Getting there 💪" if overall >= 55 else \
                "Keep practicing 🎯"

        return {
            "overall_score":       overall,
            "grade":               grade,
            "spoken_text":         spoken_text,
            "expected_text":       expected_text,
            "word_scores":         word_scores,
            "phoneme_tips":        unique_tips[:3],
            "detected_language":   info.language,
            "language_confidence": round(info.language_probability, 3),
        }

    finally:
        os.unlink(tmp_path)
