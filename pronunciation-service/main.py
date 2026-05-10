"""
Pronunciation Scoring Service
Uses faster-whisper word-level timestamps + phoneme analysis to score
Icelandic pronunciation. Returns per-word confidence, phoneme-level
feedback, and an overall score.
"""
import os
import io
import re
import tempfile
import logging
import unicodedata
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_SIZE   = os.getenv("MODEL_SIZE",   "large-v3")
DEVICE       = os.getenv("DEVICE",       "cuda")
COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "float16")

logger.info(f"Loading Whisper {MODEL_SIZE} for pronunciation scoring…")
model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
logger.info("Pronunciation service ready.")

app = FastAPI(title="Pronunciation Scoring Service")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Icelandic phoneme rules ────────────────────────────────────────────────────
# Maps Icelandic graphemes → IPA-like English hints
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

def normalize(text: str) -> str:
    return text.lower().strip().rstrip(".,!?;:")

def tokenize(text: str) -> list[str]:
    return [normalize(w) for w in text.split() if w.strip()]

def levenshtein(a: str, b: str) -> int:
    if not a: return len(b)
    if not b: return len(a)
    dp = list(range(len(b)+1))
    for i, ca in enumerate(a):
        ndp = [i+1]
        for j, cb in enumerate(b):
            ndp.append(min(dp[j]+(0 if ca==cb else 1), dp[j+1]+1, ndp[j]+1))
        dp = ndp
    return dp[-1]

def word_similarity(a: str, b: str) -> float:
    if not a or not b: return 0.0
    dist = levenshtein(a, b)
    return max(0.0, 1.0 - dist / max(len(a), len(b)))

def find_phoneme_issues(expected: str, spoken: str) -> list[dict]:
    """Check for known Icelandic phoneme problem spots in the expected word."""
    issues = []
    exp_lower = expected.lower()
    for grapheme, (_, hint) in PHONEME_HINTS.items():
        if grapheme in exp_lower:
            sim = word_similarity(normalize(expected), normalize(spoken))
            if sim < 0.75 and grapheme in COMMON_ERRORS:
                issues.append({
                    "grapheme": grapheme,
                    "hint": hint,
                    "tip": COMMON_ERRORS[grapheme],
                })
    return issues

def score_word(expected: str, spoken: str, whisper_prob: float) -> dict:
    sim   = word_similarity(normalize(expected), normalize(spoken))
    # Blend similarity with whisper confidence
    score = round((sim * 0.6 + whisper_prob * 0.4) * 100)
    issues = find_phoneme_issues(expected, spoken) if sim < 0.85 else []
    return {
        "expected": expected,
        "spoken":   spoken,
        "score":    score,
        "similarity": round(sim, 3),
        "whisper_confidence": round(whisper_prob, 3),
        "issues": issues,
        "status": "good" if score >= 80 else "fair" if score >= 55 else "needs_work",
    }

# ── API ───────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_SIZE}

@app.post("/score")
async def score_pronunciation(
    audio: UploadFile = File(...),
    expected_text: str = "",       # the text the student was trying to say
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
        # Transcribe with word-level timestamps and log-probabilities
        segments, info = model.transcribe(
            tmp_path,
            language="is",
            word_timestamps=True,
            beam_size=5,
            vad_filter=True,
        )

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

        # Score each expected word against what was spoken
        expected_tokens = tokenize(expected_text) if expected_text else []
        spoken_tokens   = [normalize(w["word"]) for w in spoken_words]

        word_scores = []
        if expected_tokens:
            # Align expected vs spoken with simple greedy matching
            s_idx = 0
            for e_tok in expected_tokens:
                if s_idx < len(spoken_words):
                    sw = spoken_words[s_idx]
                    ws = score_word(e_tok, normalize(sw["word"]), sw["probability"])
                    ws["timing"] = {"start": sw["start"], "end": sw["end"]}
                    word_scores.append(ws)
                    s_idx += 1
                else:
                    word_scores.append({
                        "expected": e_tok, "spoken": "", "score": 0,
                        "similarity": 0, "whisper_confidence": 0,
                        "issues": [], "status": "missing",
                    })

        # Overall score
        if word_scores:
            overall = round(sum(w["score"] for w in word_scores) / len(word_scores))
        elif spoken_words:
            overall = round(sum(w["probability"] for w in spoken_words) / len(spoken_words) * 100)
        else:
            overall = 0

        # Summary feedback
        needs_work = [w for w in word_scores if w["status"] == "needs_work"]
        all_issues = []
        for w in needs_work:
            all_issues.extend(w.get("issues", []))
        # Deduplicate tips
        seen_tips = set()
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
            "overall_score":    overall,
            "grade":            grade,
            "spoken_text":      spoken_text,
            "expected_text":    expected_text,
            "word_scores":      word_scores,
            "phoneme_tips":     unique_tips[:3],
            "detected_language": info.language,
            "language_confidence": round(info.language_probability, 3),
        }

    finally:
        os.unlink(tmp_path)
