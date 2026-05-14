"""
Icelandic Tutor — Backend v3
New: lesson curriculum, scenario/topic mode, mistake heatmap,
     pronunciation score proxying, error pattern analysis.
"""
import asyncio, os, json, re, sqlite3, logging, httpx, uuid, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Histogram, Counter
from telemetry import setup_tracing
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

tracer = setup_tracing("icelandic-tutor-backend")
HTTPXClientInstrumentor().instrument()

# ── Custom Prometheus metrics ─────────────────────────────────────────────────
CHAT_TTFT = Histogram(
    "chat_ttft_seconds", "Time from request start to first streamed token",
    ["provider"], buckets=[.1, .25, .5, 1, 2, 5, 10, 20, 30])
LLM_DURATION = Histogram(
    "llm_duration_seconds", "Full LLM streaming duration",
    ["provider", "model"], buckets=[1, 2.5, 5, 10, 20, 30, 60, 120])
RAG_DURATION = Histogram(
    "rag_query_duration_seconds", "RAG retrieval round-trip (backend view)",
    buckets=[.05, .1, .25, .5, 1, 2, 5])
RAG_RELEVANCE = Histogram(
    "rag_chunk_relevance", "Relevance scores of chunks returned by RAG",
    buckets=[.1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0])
GRAMMAR_ERRORS = Counter(
    "grammar_errors_total", "Grammar errors by category",
    ["category"])
PRON_SCORE = Histogram(
    "pronunciation_score", "Per-assessment pronunciation scores",
    buckets=[10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
FLASHCARDS_GEN = Counter(
    "flashcards_generated_total", "Flashcards generated via AI",
    ["level"])

LLM_PROVIDER    = os.getenv("LLM_PROVIDER",    "anthropic")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "qwen2.5:72b")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
WHISPER_URL     = os.getenv("WHISPER_URL",     "http://whisper:8001")
TTS_URL         = os.getenv("TTS_URL",         "http://tts:8002")
PRONUN_URL      = os.getenv("PRONUN_URL",      "http://pronunciation:8003")
RAG_URL         = os.getenv("RAG_URL",         "http://rag:8004")
DB_PATH         = os.getenv("DB_PATH",         "/data/tutor.db")
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with get_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, title TEXT, level TEXT,
            mode TEXT DEFAULT 'free',
            scenario_id TEXT, lesson_id TEXT,
            created_at TEXT, updated_at TEXT, turn_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, icelandic TEXT, correction TEXT,
            created_at TEXT, FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, date TEXT NOT NULL,
            turns INTEGER DEFAULT 0, errors_made INTEGER DEFAULT 0,
            errors_corrected INTEGER DEFAULT 0, level TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS flashcards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icelandic TEXT NOT NULL, english TEXT NOT NULL,
            notes TEXT, category TEXT DEFAULT 'vocabulary',
            ease_factor REAL DEFAULT 2.5, interval_days INTEGER DEFAULT 1,
            due_date TEXT, times_seen INTEGER DEFAULT 0,
            times_correct INTEGER DEFAULT 0, created_at TEXT, source_session TEXT
        );
        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, date TEXT NOT NULL,
            error_type TEXT NOT NULL,
            original TEXT, correction TEXT, explanation TEXT,
            grammar_category TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS lesson_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_id TEXT NOT NULL, completed INTEGER DEFAULT 0,
            score INTEGER DEFAULT 0, completed_at TEXT,
            session_id TEXT
        );
        CREATE TABLE IF NOT EXISTS word_of_day (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            word TEXT NOT NULL,
            english TEXT NOT NULL,
            part_of_speech TEXT,
            example_is TEXT,
            example_en TEXT,
            etymology TEXT,
            difficulty TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS cefr_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            level TEXT NOT NULL,
            score_overall INTEGER DEFAULT 0,
            score_grammar INTEGER DEFAULT 0,
            score_vocabulary INTEGER DEFAULT 0,
            score_comprehension INTEGER DEFAULT 0,
            score_speaking INTEGER DEFAULT 0,
            evidence TEXT,
            recommendations TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS cefr_exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT DEFAULT 'in_progress',
            level_target TEXT,
            questions TEXT,
            answers TEXT,
            result TEXT,
            created_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pronunciation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, date TEXT NOT NULL,
            expected_text TEXT, spoken_text TEXT,
            overall_score INTEGER, word_scores TEXT,
            phoneme_tips TEXT
        );
        """)
        # Migrations
        try:
            c.execute("ALTER TABLE flashcards ADD COLUMN part_of_speech TEXT DEFAULT ''")
        except Exception:
            pass
        # Deduplicate: keep the oldest card per icelandic word, then enforce uniqueness
        c.execute("""
            DELETE FROM flashcards WHERE id NOT IN (
                SELECT MIN(id) FROM flashcards GROUP BY lower(trim(icelandic))
            )
        """)
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_flashcards_icelandic ON flashcards(lower(trim(icelandic)))")
        c.commit()
    logger.info("DB ready.")

init_db()

# ═══════════════════════════════════════════════════════════════════════════════
# LESSON CURRICULUM DATA
# ═══════════════════════════════════════════════════════════════════════════════
LESSONS = [
    # ── Beginner track ────────────────────────────────────────────────────────
    {"id":"L01","track":"beginner","order":1,"title":"Greetings & Introductions",
     "description":"Learn to say hello, introduce yourself, and ask someone's name.",
     "grammar_focus":"Basic sentence structure, verb 'að vera' (to be)",
     "vocabulary":["Halló","Góðan daginn","Ég heiti…","Hvað heitir þú?","Gaman að hitta þig"],
     "goal":"Have a basic introduction conversation with Sigríður.",
     "system_addon":"Focus this lesson on greetings and introductions. Introduce yourself, ask the student's name, where they're from. Teach: Halló, Góðan daginn, Ég heiti, Hvað heitir þú, Ég er frá."},
    {"id":"L02","track":"beginner","order":2,"title":"Numbers & Counting",
     "description":"Count to 100, tell your age, discuss quantities.",
     "grammar_focus":"Cardinal numbers, the verb 'að vera' with age",
     "vocabulary":["einn","tveir","þrír","fjórir","fimm","tíu","tuttugu","hundrað"],
     "goal":"Count objects and tell Sigríður your age and phone number.",
     "system_addon":"Focus on Icelandic numbers. Count things together, ask the student's age, practice phone numbers. Numbers: einn/ein, tveir/tvær, þrír/þrjár..."},
    {"id":"L03","track":"beginner","order":3,"title":"Colors & Descriptions",
     "description":"Describe objects using colors and basic adjectives.",
     "grammar_focus":"Adjective agreement with noun gender",
     "vocabulary":["rauður","blár","grænn","gulur","svartur","hvítur","stór","lítill"],
     "goal":"Describe 5 objects in the room using colors and adjectives.",
     "system_addon":"Teach colors and basic adjectives. Point to things (describe them), practice adjective agreement. Teach that adjectives change form by gender: rauður/rauð/rautt."},
    {"id":"L04","track":"beginner","order":4,"title":"Family Members",
     "description":"Talk about your family — parents, siblings, children.",
     "grammar_focus":"Possessive pronouns, noun plurals",
     "vocabulary":["móðir","faðir","systir","bróðir","barn","afi","amma","eiginmaður","eiginkona"],
     "goal":"Describe your family to Sigríður.",
     "system_addon":"Focus on family vocabulary. Ask about the student's family. Teach possessive forms: mamma mín, pabbi minn. Plural nouns."},
    {"id":"L05","track":"beginner","order":5,"title":"Food & Drink",
     "description":"Order food, express preferences, talk about meals.",
     "grammar_focus":"The accusative case with 'að vilja' (to want)",
     "vocabulary":["matur","drykkur","brauð","mjólk","vatn","kaffi","fiskur","kjöt","grænmeti"],
     "goal":"Order a meal and describe what you like to eat.",
     "system_addon":"Role-play a café or restaurant. Teach food vocabulary. Practice ordering: Ég vil fá... Má ég fá...? Introduce accusative case."},
    {"id":"L06","track":"beginner","order":6,"title":"Days, Months & Time",
     "description":"Tell the time, say what day it is, discuss your schedule.",
     "grammar_focus":"Dative case with time expressions",
     "vocabulary":["mánudagur","þriðjudagur","miðvikudagur","janúar","febrúar","klukkan","í dag","á morgun"],
     "goal":"Describe your weekly schedule to Sigríður.",
     "system_addon":"Teach days of week, months, telling time. Klukkan... Ask what day it is, what the student does each day."},
    {"id":"L07","track":"beginner","order":7,"title":"The Weather",
     "description":"Talk about Icelandic weather — sun, rain, wind, snow, and temperature.",
     "grammar_focus":"Impersonal weather expressions, adjective predicates",
     "vocabulary":["veður","rigning","snjór","vindur","kalt","hlýtt","sól","þoka","frost","stormur"],
     "goal":"Describe today's weather and ask about tomorrow's forecast.",
     "system_addon":"Focus on Icelandic weather vocabulary — extremely relevant in Iceland! Teach impersonal expressions: Það er kalt, Það rignir, Það snjóar. Ask the student about today's weather, compare seasons. Introduce: hlýtt/kalt/vindasamt/þokið/sólríkt."},
    {"id":"L08","track":"beginner","order":8,"title":"Getting Around",
     "description":"Use public transport, ask for directions, navigate a city.",
     "grammar_focus":"Imperative mood for directions, accusative with motion verbs",
     "vocabulary":["strætó","stoppistöð","leiga","gangstétt","beyga","beint áfram","til vinstri","til hægri","nálægt","langt"],
     "goal":"Ask Sigríður for directions to three Reykjavík landmarks and understand the answers.",
     "system_addon":"Teach transport and navigation vocabulary. Role-play giving/receiving directions in Reykjavík. Teach imperative for directions: Farðu beint áfram, Beygðu til vinstri. Prepositions of movement with accusative."},
    {"id":"L09","track":"beginner","order":9,"title":"At the Hotel",
     "description":"Check in and out, request amenities, handle common hotel situations.",
     "grammar_focus":"Polite requests with 'mætti ég', modal verbs",
     "vocabulary":["herbergi","lykill","morgunmatur","brottför","koma","bókun","baðherbergi","þjónusta"],
     "goal":"Check in to a hotel, ask for a wake-up call, and request extra towels.",
     "system_addon":"Role-play as hotel receptionist. Student checks in, asks for things, handles issues. Teach polite request forms: Mætti ég fá...? Gæti þú...? Natural hotel service Icelandic."},
    {"id":"L10","track":"beginner","order":10,"title":"Feelings & Health",
     "description":"Express how you feel physically and emotionally, describe symptoms.",
     "grammar_focus":"Dative with feeling expressions ('mér líður vel'), body vocabulary",
     "vocabulary":["veikur","þreyttur","sárt","höfuðverkur","kuldafloginn","gleður","reiður","hræddur","líða","heilsa"],
     "goal":"Tell Sigríður how you feel today and describe a recent illness.",
     "system_addon":"Teach feeling and health vocabulary. Drill dative with feelings: Mér líður vel/illa, Mig verkjar, Ég er veikur. Ask the student how they feel, teach body parts, practice describing symptoms."},
    {"id":"L11","track":"beginner","order":11,"title":"Shopping & Money",
     "description":"Buy things, ask about prices, handle money and payments.",
     "grammar_focus":"Accusative for quantities and prices, question words",
     "vocabulary":["verð","króna","dýrt","ódýrt","kaupa","selja","greiða","afsláttur","kvittun","búð"],
     "goal":"Buy three items, negotiate a price, and ask for a receipt.",
     "system_addon":"Role-play as shopkeeper. Teach price vocabulary: Hvað kostar þetta? Það er of dýrt. Practice numbers in context of prices. Accusative with quantities. Krónur (ISK currency)."},
    {"id":"L12","track":"beginner","order":12,"title":"Telling Stories — Simple Past",
     "description":"Recount events in the past, describe what happened.",
     "grammar_focus":"Simple past tense introduction, time adverbs",
     "vocabulary":["í gær","í síðustu viku","áður","síðan","fyrst","þá","loks"],
     "goal":"Tell Sigríður about what you did yesterday using at least 5 past tense verbs.",
     "system_addon":"Focus on past tense narrative. Ask the student what they did yesterday, last weekend. Teach common weak verb past forms first: fór, kom, gerði, sagði, sá. Time adverbs: í gær, í morges, í síðustu viku."},
    # ── Intermediate track ────────────────────────────────────────────────────
    {"id":"L13","track":"intermediate","order":1,"title":"The Four Cases — Nominative & Accusative",
     "description":"Understand when to use nominative vs accusative case.",
     "grammar_focus":"Nominative (subject) vs Accusative (direct object)",
     "vocabulary":["hús","hundur","köttur","stóll","borð","bók","penni"],
     "goal":"Correctly use nouns in both nominative and accusative in 10 sentences.",
     "system_addon":"Drill nominative vs accusative case. Give sentences to complete, correct errors explicitly. Use concrete examples: Hundurinn bítur manninn (acc). Explain the -inn/-inn pattern."},
    {"id":"L14","track":"intermediate","order":2,"title":"The Dative Case",
     "description":"Master the dative case — used with many prepositions and indirect objects.",
     "grammar_focus":"Dative case endings, prepositions that take dative",
     "vocabulary":["með","á","í","frá","til","hjá","eftir"],
     "goal":"Use dative correctly with 5 prepositions.",
     "system_addon":"Focus on dative case. Drill prepositions that take dative: í, á, með, frá, hjá. Correct dative errors specifically."},
    {"id":"L15","track":"intermediate","order":3,"title":"The Genitive Case",
     "description":"Express possession and relationships using genitive.",
     "grammar_focus":"Genitive case endings, possessive constructions",
     "vocabulary":["eigandi","hluti","nafn","heimilisfang","kennitala"],
     "goal":"Describe ownership of 8 things using genitive.",
     "system_addon":"Teach genitive case for possession. Bók Sigríðar (Sigríður's book). Practice possession structures."},
    {"id":"L16","track":"intermediate","order":4,"title":"Verb Conjugation — Present Tense",
     "description":"Conjugate strong and weak verbs across all persons.",
     "grammar_focus":"Present tense conjugation patterns, strong vs weak verbs",
     "vocabulary":["að fara","að koma","að gera","að segja","að sjá","að vita","að vilja"],
     "goal":"Conjugate 7 common verbs correctly in all persons.",
     "system_addon":"Drill present tense conjugation. Have student conjugate verbs: ég fer, þú ferð, hann/hún fer, við förum, þið farið, þeir/þær fara. Correct all conjugation errors."},
    {"id":"L17","track":"intermediate","order":5,"title":"Past Tense",
     "description":"Talk about what happened in the past.",
     "grammar_focus":"Past tense (þátíð) of strong and weak verbs",
     "vocabulary":["fór","kom","gerði","sagði","sá","vissi","vildi"],
     "goal":"Tell Sigríður about what you did yesterday, entirely in past tense.",
     "system_addon":"Focus on past tense. Ask the student what they did yesterday, last week. Correct past tense errors. Teach strong verb ablaut patterns."},
    {"id":"L18","track":"intermediate","order":6,"title":"Subjunctive & Conditionals",
     "description":"Express wishes, hypotheticals, and polite requests.",
     "grammar_focus":"Subjunctive mood (viðtengingarhátt), conditional sentences",
     "vocabulary":["myndi","væri","hefði","mætti","skyldi"],
     "goal":"Form 5 conditional sentences and 3 polite requests.",
     "system_addon":"Teach subjunctive/conditional. Hvað myndir þú gera ef...? Polite requests: Mætti ég fá...? Viltu hjálpa mér?"},
    {"id":"L19","track":"intermediate","order":7,"title":"Comparatives & Superlatives",
     "description":"Compare things — bigger, better, more expensive, the best.",
     "grammar_focus":"Comparative and superlative adjective forms, 'en' (than)",
     "vocabulary":["stærri","stærstur","betri","bestur","dýrari","dýrastur","fleiri","flestir","meira","mest"],
     "goal":"Make 8 comparison sentences describing people, places, and things.",
     "system_addon":"Teach comparative and superlative forms. Drill: þetta er stærra en... þetta er stærst. Irregular comparatives: góður/betri/bestur, lítill/minni/minstur, margur/fleiri/flestir. Have student compare things around them."},
    {"id":"L20","track":"intermediate","order":8,"title":"Reflexive Verbs & Pronouns",
     "description":"Master the reflexive pronoun 'sig' and reflexive verb constructions.",
     "grammar_focus":"Reflexive pronoun sig/sér/sín, reflexive verbs",
     "vocabulary":["sig","sér","sín","klæða sig","setja sig","líða","finna fyrir sér","hreyfa sig","þvo sér"],
     "goal":"Use sig/sér/sín correctly in 6 sentences and conjugate 3 reflexive verbs.",
     "system_addon":"Focus on the Icelandic reflexive — one of the trickiest features. Teach sig (acc), sér (dat), sín (gen). Reflexive verbs: klæða sig (to dress), setja sig (to sit down), þvo sér (to wash). Distinguish from English 'himself/herself'. Drill heavily with corrections."},
    {"id":"L21","track":"intermediate","order":9,"title":"Passive Voice",
     "description":"Express actions without naming the subject — 'it was done', 'it is said'.",
     "grammar_focus":"Passive construction with 'vera' + past participle, impersonal passive",
     "vocabulary":["gert","sagt","talið","séð","heyrt","fundið","búið til","opnað","lokað"],
     "goal":"Produce 5 passive sentences describing events or states.",
     "system_addon":"Teach Icelandic passive: vera + past participle. Þetta var gert (this was done). Also teach the -st passive/middle: Það er sagt að... Impersonal constructions. Compare with active equivalents."},
    {"id":"L22","track":"intermediate","order":10,"title":"Modal Verbs in Depth",
     "description":"Master must, can, need, may — and the cases they govern.",
     "grammar_focus":"Modal verbs: mega, verða, þurfa, geta, skylda + correct case",
     "vocabulary":["mega","verða","þurfa","geta","skylda","má","verð","þarf","get","á að"],
     "goal":"Use all 5 modal verbs correctly in conversation with proper case agreement.",
     "system_addon":"Deep drill on modal verbs. Each takes different construction: Ég get gert þetta (can), Ég verð að fara (must), Ég þarf að sofa (need to), Mér má/Ég má (I am allowed). Correct case errors after each modal. Practice in real scenarios."},
    {"id":"L23","track":"intermediate","order":11,"title":"Prepositions & Cases Deep Dive",
     "description":"Master which prepositions take which cases — and why.",
     "grammar_focus":"Case government of prepositions, motion vs location distinction",
     "vocabulary":["í","á","við","fyrir","eftir","um","til","frá","með","án","gegnum","meðfram"],
     "goal":"Use 8 prepositions correctly with the right case in context.",
     "system_addon":"Focus on the motion/location distinction: í bænum (dative, location) vs í bæinn (accusative, motion toward). Drill: Ég fer í skólann (acc) vs Ég er í skólanum (dat). Each preposition with its case rule. Heavily correct case errors."},
    {"id":"L24","track":"intermediate","order":12,"title":"Talking About the Future",
     "description":"Express plans, predictions, and intentions.",
     "grammar_focus":"Future with 'ætla að', 'mun', 'verður að', present for near future",
     "vocabulary":["ætla","mun","verður","vonast","búast við","líklega","kannski","ef til vill","á morgun","í næstu viku"],
     "goal":"Describe your plans for next week using at least 3 different future constructions.",
     "system_addon":"Teach Icelandic future expressions. Ætla að (intend to/going to): Ég ætla að fara. Mun (will, prediction): Það mun rigna. Verður að (will have to). Present tense for scheduled future. Ask student about their plans."},
    # ── Advanced track ────────────────────────────────────────────────────────
    {"id":"L25","track":"advanced","order":1,"title":"Noun Declension Mastery",
     "description":"Full command of all noun declension classes.",
     "grammar_focus":"All four declension classes, irregular nouns",
     "vocabulary":["maður","kona","barn","hestur","skip","borg"],
     "goal":"Decline 6 nouns correctly in all four cases, singular and plural.",
     "system_addon":"Advanced declension drill. Give tables to complete. Correct all case errors. Challenge with irregular nouns."},
    {"id":"L26","track":"advanced","order":2,"title":"Complex Sentences & Relative Clauses",
     "description":"Build sophisticated sentences with embedded clauses.",
     "grammar_focus":"Relative pronouns, subordinate clauses, word order",
     "vocabulary":["sem","þar sem","þegar","þótt","þar til","svo að"],
     "goal":"Produce 5 complex sentences with relative clauses.",
     "system_addon":"Teach complex sentence structures. Practice relative clauses with 'sem'. Subordinate clauses and their word order differences."},
    {"id":"L27","track":"advanced","order":3,"title":"Idiomatic Expressions",
     "description":"Sound natural with common Icelandic idioms and expressions.",
     "grammar_focus":"Idiomatic usage, fixed phrases",
     "vocabulary":["Mér líður vel","Þetta reddast","Hvernig gengur?","Gangi þér vel","Vertu sæll"],
     "goal":"Use 8 idiomatic expressions naturally in conversation.",
     "system_addon":"Teach and practice Icelandic idioms. Introduce: Þetta reddast (it'll work out), Mér líður vel, common fixed expressions. Use them in context."},
    {"id":"L28","track":"advanced","order":4,"title":"The Middle Voice (-st verbs)",
     "description":"Master the unique Icelandic middle voice — verbs ending in -st.",
     "grammar_focus":"Middle voice formation, reciprocal and reflexive -st verbs",
     "vocabulary":["kallast","finnast","líðast","hittast","kynnast","skiljanst","gleðjast","kvíðast","minnast","þykjast"],
     "goal":"Use 6 middle voice verbs correctly and explain the difference from active forms.",
     "system_addon":"Teach the Icelandic middle voice (-st suffix). Three main uses: reflexive (klæðast = to dress oneself), reciprocal (hittast = to meet each other), impersonal/passive (það finnst mér = it seems to me). This is unique to Icelandic/Norse. Drill each type with corrections. Compare: kenna (to teach) vs kennast við (to be acquainted with)."},
    {"id":"L29","track":"advanced","order":5,"title":"Noun Declension of Proper Names",
     "description":"Decline Icelandic personal names correctly across all four cases.",
     "grammar_focus":"Name declension patterns, -ar vs -s genitive, declined first names",
     "vocabulary":["Jón","Jóns","Jóni","Björk","Björku","Sigríður","Sigríðar","Gunnar","Gunnars"],
     "goal":"Correctly decline 5 Icelandic names in all four cases.",
     "system_addon":"Teach Icelandic name declension — tourists and learners always struggle with this. Names fully decline: Jón (nom), Jóns (gen), Jóni (dat), Jón (acc). Female names: Sigríður, Sigríðar, Sigríði, Sigríði. Practice using names in sentences. Also teach patronymic system: Jónsson/Jónsdóttir."},
    {"id":"L30","track":"advanced","order":6,"title":"Formal vs Informal Register",
     "description":"Navigate between casual speech and formal written Icelandic.",
     "grammar_focus":"Register differences, formal vocabulary, written vs spoken forms",
     "vocabulary":["kæri","virðulegur","þér","yður","hér með","meðfylgjandi","gjörðu svo vel","með vinsemd"],
     "goal":"Write a formal email and contrast it with how you'd say the same thing casually.",
     "system_addon":"Teach the difference between formal written and informal spoken Icelandic. Formal letters use: Kæri/Kæra, Með vinsemd og virðingu, Hér með. The formal second person þér/yður (now rare but seen in formal writing). Formal vs informal vocabulary pairs. Have student draft a formal email then rewrite casually."},
    {"id":"L31","track":"advanced","order":7,"title":"Icelandic Phonology Deep Dive",
     "description":"Master the sounds that make Icelandic distinctive — pre-aspiration, lateral fricative, vowel shifts.",
     "grammar_focus":"Pre-aspiration, lateral fricative ll, rl cluster, vowel quantity",
     "vocabulary":["köttur","vatn","epli","þorskur","fjall","völlur","herbergi","allt","fellt"],
     "goal":"Correctly pronounce 10 words featuring challenging Icelandic phonemes.",
     "system_addon":"Deep phonology lesson. Teach: pre-aspiration (köttur — the tt has a breath before it), the lateral fricative ll (like tl with a hiss), rl cluster (like rdl), vowel length distinctions. Use TTS to model pronunciation. Have student attempt words and give detailed phonemic feedback. Focus on what makes Icelandic sound unlike other languages."},
    {"id":"L32","track":"advanced","order":8,"title":"Reading Old Norse Cognates",
     "description":"Connect modern Icelandic to its Old Norse roots through cognates with English.",
     "grammar_focus":"Etymology, sound correspondences, Norse loan words in English",
     "vocabulary":["skip","gleyma","vindur","drykkur","systir","faðir","egg","hnífur","gluggi","húsbóndi"],
     "goal":"Identify 10 Old Norse cognates in English and explain their sound shifts.",
     "system_addon":"Teach the Norse roots of Icelandic and connections to English. Many English words come from Old Norse via Viking influence: egg, knife, window, sky, husband, ugly, wrong, anger, cake, die. Explain sound shifts: Norse sk became English sh (skip→ship, skinn→skin). This helps vocabulary retention enormously. Make it a discovery exercise."},
    {"id":"L33","track":"advanced","order":9,"title":"Newspaper & Media Language",
     "description":"Read Icelandic news headlines, understand formal broadcast language.",
     "grammar_focus":"Headline grammar (omitted verbs), nominalization, formal connectives",
     "vocabulary":["þingmaður","ríkisstjórn","hagvöxtur","verðlag","hlutfall","rannsókn","tilkynning","skýrsla","viðtal","greinargerð"],
     "goal":"Read and explain 3 Icelandic news headlines and summarize a short news item.",
     "system_addon":"Teach media and newspaper Icelandic. Headlines often omit verbs and use nominalizations. Formal connectives: þar sem, þrátt fyrir, vegna þess að. Political/economic vocabulary. Discuss a current Icelandic news story (make one up if needed). Have student practice summarizing in formal register."},
    # ── Cultural track ────────────────────────────────────────────────────────
    {"id":"C01","track":"cultural","order":1,"title":"Icelandic Names & Patronymics",
     "description":"Understand how Icelandic names work — patronymics, matronymics, and address customs.",
     "grammar_focus":"Patronymic formation, name declension in practice",
     "vocabulary":["Jónsson","Jónsdóttir","fornafn","eftirnafn","kenninafn","faðir","móðir","-son","-dóttir"],
     "goal":"Explain the Icelandic naming system and correctly form 4 patronymics.",
     "system_addon":"Teach the Icelandic patronymic system — unique in Europe. No family surnames: children take father's (or mother's) first name + son/dóttir. Jón Sigurðsson's son Pétur is Pétur Jónsson. His sister is Anna Jónsdóttir. Also: people are listed by first name in the phone book! Modern matronymics. How to address people (always by first name). Practice forming patronymics."},
    {"id":"C02","track":"cultural","order":2,"title":"The Sagas — Key Passages",
     "description":"Read simplified passages from the Icelandic Sagas with vocabulary support.",
     "grammar_focus":"Old/formal narrative style, saga vocabulary, past tense narrative",
     "vocabulary":["víkingur","goði","þing","útlægur","blót","frændi","hefnd","drengskapur","skáld","Ísland"],
     "goal":"Read and discuss a simplified saga passage, identifying key narrative elements.",
     "system_addon":"Introduce the Icelandic Sagas — world literature treasure written 1100-1400 AD. Start with a simplified Njáls saga or Egils saga passage. Teach saga vocabulary: goði (chieftain), þing (assembly), útlægur (outlaw), hefnd (revenge). Discuss the narrative style. Connect to modern Icelandic — the language has barely changed. Make it a cultural conversation."},
    {"id":"C03","track":"cultural","order":3,"title":"Icelandic Holidays & Traditions",
     "description":"Learn about þorrablót, Jónsmessa, Verslunarmannahelgi, and other Icelandic celebrations.",
     "grammar_focus":"Describing customs and traditions, temporal expressions",
     "vocabulary":["þorrablót","Jónsmessa","hákarl","svið","brennivín","jólasveinar","Verslunarmannahelgi","páskar","sumardagurinn fyrsti"],
     "goal":"Describe three Icelandic holidays and explain what happens at þorrablót.",
     "system_addon":"Teach Icelandic cultural celebrations. Þorrablót (midwinter feast in January): traditional foods including hákarl (fermented shark), svið (singed sheep head), brennivín (schnapps). Jónsmessa (midsummer). Verslunarmannahelgi (August bank holiday). 13 Jólasveinar instead of Santa. The first day of summer (April). Make it conversational and cultural."},
    {"id":"C04","track":"cultural","order":4,"title":"Music & Pop Culture",
     "description":"Discuss Icelandic music, art, and contemporary culture.",
     "grammar_focus":"Discussing preferences and opinions, comparative language",
     "vocabulary":["tónlist","lag","hljómsveit","söngvari","kvikmynd","listir","bók","höfundur","íslenska","menning"],
     "goal":"Discuss a favourite type of music and ask about Icelandic cultural life.",
     "system_addon":"Talk about Icelandic music and culture. Discuss the Icelandic music scene, Airwaves festival, the literary tradition (most books per capita globally), design and architecture, film. Ask the student what music/art they enjoy, make comparisons. Teach opinion vocabulary: Mér finnst, Að mínu mati, Ég held að."},
    {"id":"C05","track":"cultural","order":5,"title":"Vikings & The Settlement of Iceland",
     "description":"Discuss the Viking Age, the settlement of Iceland (874 AD), and early Icelandic history.",
     "grammar_focus":"Historical past tense, formal narrative vocabulary",
     "vocabulary":["landnám","Ingólfur Arnarson","Alþingi","goðorð","þræll","búnaður","landnámsmaður","víkingaöld","Norðmenn","Garðarshólmur"],
     "goal":"Recount the story of Iceland's settlement and explain what the Alþingi was.",
     "system_addon":"Teach Iceland's founding history. Ingólfur Arnarson first settler 874 AD. The landnám (settlement) period. Alþingi founded 930 AD — world's oldest parliament still in operation. Norse settlement from Norway and Celtic slaves from British Isles. Viking Age context. Use past tense narrative throughout. Ask student to retell the story."},
]

# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO DATA
# ═══════════════════════════════════════════════════════════════════════════════
SCENARIOS = [
    # ── Original 10 ───────────────────────────────────────────────────────────
    {"id":"S01","category":"travel","title":"At the Airport",
     "icon":"✈️","description":"Check in, find your gate, navigate the airport.",
     "sigridur_role":"airport staff member","level":"beginner",
     "vocabulary":["farþegi","farseðill","ferð","töskur","hlið","bíða","flug"],
     "system_addon":"You are an Icelandic airport staff member at Keflavík airport. Help the student check in, find their gate, handle baggage questions. Speak in natural airport Icelandic. Give corrections after each student turn."},
    {"id":"S02","category":"food","title":"Ordering at a Restaurant",
     "icon":"🍽️","description":"Read a menu, order food and drinks, pay the bill.",
     "sigridur_role":"restaurant server","level":"beginner",
     "vocabulary":["matseðill","réttir","forréttur","meginréttur","eftirréttir","reikningur","þjónn"],
     "system_addon":"You are a friendly server at an Icelandic restaurant. Take the student's order, answer questions about the menu, bring the bill. Give corrections after each student turn."},
    {"id":"S03","category":"shopping","title":"Shopping for Clothes",
     "icon":"👕","description":"Find your size, ask about prices, try things on.",
     "sigridur_role":"clothing store assistant","level":"beginner",
     "vocabulary":["stærð","verð","litur","efni","prufuklefar","afsláttur","greiðsla"],
     "system_addon":"You are a helpful assistant in an Icelandic clothing shop. Help the student find clothes, ask their size, discuss prices and colors. Natural retail Icelandic."},
    {"id":"S04","category":"social","title":"Meeting New People at a Party",
     "icon":"🎉","description":"Introduce yourself, make small talk, discuss interests.",
     "sigridur_role":"fellow party guest","level":"beginner",
     "vocabulary":["kynna","áhugi","vinur","vinna","gaman","tómstundir","tónlist","íþróttir"],
     "system_addon":"You are a friendly Icelander the student has just met at a party. Make small talk: ask where they're from, what they do, their interests. Natural social Icelandic."},
    {"id":"S05","category":"travel","title":"Asking for Directions",
     "icon":"🗺️","description":"Ask how to get somewhere, understand directions.",
     "sigridur_role":"local Icelander on the street","level":"beginner",
     "vocabulary":["beint áfram","til vinstri","til hægri","hornan","gatnamót","nálægt","langt","stutt"],
     "system_addon":"You are a local Icelander on the street in Reykjavík. Give the student directions to landmarks: Hallgrímskirkja, Harpa, the harbor. Use natural direction-giving Icelandic."},
    {"id":"S06","category":"medical","title":"At the Doctor",
     "icon":"🏥","description":"Describe symptoms, understand medical advice.",
     "sigridur_role":"doctor at a clinic","level":"intermediate",
     "vocabulary":["veikur","einkenni","verkur","lyf","hjartsláttur","blóðþrýstingur","lyfseðill","læknir"],
     "system_addon":"You are a doctor at an Icelandic clinic. Ask about the student's symptoms, give simple medical advice, prescribe medicine. Medical Icelandic, intermediate level."},
    {"id":"S07","category":"work","title":"A Job Interview",
     "icon":"💼","description":"Discuss your background, skills, and why you want the job.",
     "sigridur_role":"hiring manager","level":"intermediate",
     "vocabulary":["starfsferill","reynsla","hæfni","menntun","launakröfur","verkefni","teymi","áætlun"],
     "system_addon":"You are conducting a job interview in Icelandic. Ask about work history, skills, why they want the position. Professional register. Intermediate level."},
    {"id":"S08","category":"culture","title":"Discussing Icelandic History & Sagas",
     "icon":"📜","description":"Talk about the Settlement, the sagas, and Icelandic heritage.",
     "sigridur_role":"museum guide","level":"advanced",
     "vocabulary":["landnám","Íslendingasögur","þing","goði","víkingur","Alþingi","Eddur","skáld"],
     "system_addon":"You are a guide at the National Museum of Iceland. Discuss the Settlement (874 AD), the Sagas, Alþingi. Rich cultural Icelandic, advanced vocabulary."},
    {"id":"S09","category":"nature","title":"Talking About Icelandic Nature",
     "icon":"🌋","description":"Discuss volcanoes, geysers, northern lights, and Icelandic weather.",
     "sigridur_role":"nature guide","level":"intermediate",
     "vocabulary":["eldfjall","goshver","norðurljós","veður","loftslag","jökull","hraun","náttúra"],
     "system_addon":"You are an Icelandic nature guide. Discuss volcanoes, geysers, Northern Lights. Natural description vocabulary. Intermediate level."},
    {"id":"S10","category":"social","title":"Talking About Weekend Plans",
     "icon":"🏔️","description":"Make and discuss plans, suggest activities, agree or decline.",
     "sigridur_role":"Icelandic friend","level":"beginner",
     "vocabulary":["um helgina","ætla","fara","heimsækja","biðja","bjóða","dagskrá","tímasetning"],
     "system_addon":"You are the student's Icelandic friend making weekend plans. Suggest activities, ask their preferences, make plans together. Casual, friendly Icelandic."},

    # ── Travel & Navigation ───────────────────────────────────────────────────
    {"id":"S11","category":"travel","title":"Renting a Car",
     "icon":"🚗","description":"Pick up a rental car, ask about insurance, return it, handle damage.",
     "sigridur_role":"car rental agent","level":"beginner",
     "vocabulary":["bíll","leiga","trygging","skemmdir","skilyrði","keyrsla","eldsneyti","afhending"],
     "system_addon":"You are a car rental agent at an Icelandic rental desk. The student wants to rent a car. Discuss vehicle type, insurance options, fuel policy, return conditions. Answer questions about driving in Iceland (F-roads, ring road). Natural rental desk Icelandic."},
    {"id":"S12","category":"medical","title":"At the Pharmacy",
     "icon":"💊","description":"Describe symptoms, ask for medication, understand dosage instructions.",
     "sigridur_role":"pharmacist","level":"beginner",
     "vocabulary":["lyf","lyfseðill","skammtur","einkenni","höfuðverkur","kvef","magurverk","allergía","apótek"],
     "system_addon":"You are a pharmacist in an Icelandic pharmacy. The student comes in describing symptoms. Recommend over-the-counter medication, explain dosage, ask about allergies. Helpful, clear Icelandic. Not a substitute for a doctor."},
    {"id":"S13","category":"travel","title":"Taking a Domestic Flight",
     "icon":"🛫","description":"Fly from Reykjavík to Akureyri — small airport, check-in, boarding.",
     "sigridur_role":"airline staff at Reykjavík domestic airport","level":"beginner",
     "vocabulary":["innanlandsflugi","Reykjavík","Akureyri","farmiðinn","borðstigi","flugvöllur","farþegi","seinka"],
     "system_addon":"You are staff at Reykjavík domestic airport (Reykjavíkurflugvöllur). The student is taking Eagle Air or Air Iceland Connect to Akureyri. Help with check-in, gate, boarding. Small friendly airport atmosphere."},
    {"id":"S14","category":"travel","title":"At a Petrol Station",
     "icon":"⛽","description":"Fill up the tank, pay, ask for directions, buy snacks.",
     "sigridur_role":"petrol station attendant","level":"beginner",
     "vocabulary":["bensín","dísel","tankur","borga","kort","kvittun","krókur","kassa","pylsa","heitt"],
     "system_addon":"You are working at an Icelandic petrol station (bensínstöð). Student needs to fill up, pay, possibly ask about nearby attractions or road conditions. Also serve the classic Icelandic petrol station food — pylsur (hot dogs)! Casual helpful Icelandic."},
    {"id":"S15","category":"travel","title":"Lost & Found",
     "icon":"🔍","description":"Report a lost item, describe your belongings, visit a lost property office.",
     "sigridur_role":"lost property officer","level":"intermediate",
     "vocabulary":["glatað","fann","lýsing","litur","stærð","veski","sími","töska","passa","þekkja"],
     "system_addon":"You are a lost property officer at an Icelandic police station or tourist office. The student has lost something. Ask them to describe the item in detail — color, size, brand, contents. Check your records, explain the process. Intermediate vocabulary."},

    # ── Food & Social ─────────────────────────────────────────────────────────
    {"id":"S16","category":"food","title":"At a Coffee Shop",
     "icon":"☕","description":"Order coffee and pastries, customise your drink, make small talk.",
     "sigridur_role":"friendly barista","level":"beginner",
     "vocabulary":["kaffi","te","mjólk","sykur","kaka","samloka","stór","lítill","heitt","kalt"],
     "system_addon":"You are a friendly barista at a Reykjavík coffee shop. Take the student's order, ask about milk preferences, recommend pastries. Casual coffee shop Icelandic. Make small talk about the weather or their plans."},
    {"id":"S17","category":"food","title":"Grocery Shopping",
     "icon":"🛒","description":"Find items in a supermarket, ask staff for help, go through checkout.",
     "sigridur_role":"supermarket staff member","level":"beginner",
     "vocabulary":["verslun","gangur","hillu","afurð","grænmeti","kjöt","brauð","mjólkurvara","kassa","poki"],
     "system_addon":"You are a helpful staff member at a Bónus or Krónan supermarket in Iceland. Help the student find items, explain where things are in the store. At checkout, ask if they have a loyalty card, pack bags. Natural supermarket Icelandic."},
    {"id":"S18","category":"social","title":"Dinner at Someone's Home",
     "icon":"🏠","description":"Be a guest for dinner — compliments, dietary restrictions, toasts, gratitude.",
     "sigridur_role":"Icelandic host","level":"intermediate",
     "vocabulary":["gestur","þakka","skál","bragðgóður","mataræði","ofnæmi","grænmetisæta","uppskrift","kvöldmatur","hlý"],
     "system_addon":"You are hosting the student for dinner at your Icelandic home. Welcome them, offer drinks, serve dinner, make conversation. Student should compliment the food, navigate dietary questions, participate in skál (toast). Warm, hospitable Icelandic."},
    {"id":"S19","category":"social","title":"At a Bar",
     "icon":"🍺","description":"Order drinks, make conversation, experience Icelandic nightlife.",
     "sigridur_role":"bartender","level":"intermediate",
     "vocabulary":["bjór","vín","cocktail","gler","sæti","tónlist","hlærinn","reikningur","þakka","skál"],
     "system_addon":"You are a bartender at a Reykjavík bar on a Friday night. Take orders, make conversation, describe drinks on the menu. Casual evening Icelandic. Student practices ordering, socialising, understanding loud/casual speech."},
    {"id":"S20","category":"food","title":"Food Festival — Þorrablót",
     "icon":"🦈","description":"Navigate a þorrablót feast — traditional foods, polite declining, asking what things are.",
     "sigridur_role":"fellow guest at a þorrablót","level":"intermediate",
     "vocabulary":["hákarl","svið","hrútspungar","slátur","brennivín","þorri","smakka","óvenjulegt","hefð","bragð"],
     "system_addon":"You are a fellow guest at a traditional þorrablót feast in January. Help the student identify the unusual traditional foods: hákarl (fermented shark), svið (singed sheep head), hrútspungar (pickled ram testicles), slátur (blood pudding). Be encouraging, explain the tradition, suggest they try things bravely. Warm cultural Icelandic."},

    # ── Work & Formal ─────────────────────────────────────────────────────────
    {"id":"S21","category":"work","title":"Opening a Bank Account",
     "icon":"🏦","description":"Visit a bank, provide ID, choose an account type, set up online banking.",
     "sigridur_role":"bank teller","level":"intermediate",
     "vocabulary":["banki","reikningur","kennitala","skilríki","millifærsla","netbanki","sparnaður","greiðslukort","vextir","gjald"],
     "system_addon":"You are a bank teller at Landsbankinn or Íslandsbanki. The student wants to open an account. Ask for kennitala (ID number), passport, explain account types, set up netbanki (online banking). Formal but helpful bank Icelandic."},
    {"id":"S22","category":"work","title":"At the Post Office",
     "icon":"📮","description":"Send a package abroad, fill in customs forms, buy stamps.",
     "sigridur_role":"post office clerk","level":"beginner",
     "vocabulary":["póstur","pakki","frímerki","toll","þyngd","sendandi","viðtakandi","skráð","truflun","erlendis"],
     "system_addon":"You are a clerk at an Icelandic post office (Pósturinn). The student wants to send a package. Weigh it, ask destination, discuss customs forms for international packages, sell stamps. Efficient postal service Icelandic."},
    {"id":"S23","category":"work","title":"Visiting a Government Office",
     "icon":"🏛️","description":"Register your ID, ask about services, navigate formal bureaucratic language.",
     "sigridur_role":"government office worker","level":"intermediate",
     "vocabulary":["ríkisstjórnin","skráning","kennitala","lögheimili","umsókn","eyðublað","undirskrift","stimpill","dagsetning","skilyrði"],
     "system_addon":"You are a worker at Þjóðskrá (National Registry) or similar government office. The student needs to register, update records, or ask about services. Formal bureaucratic Icelandic, but helpful. Explain forms, requirements, processing times."},
    {"id":"S24","category":"work","title":"Calling in Sick",
     "icon":"📞","description":"Call your workplace to report illness — phone register, symptoms, duration.",
     "sigridur_role":"supervisor at work","level":"intermediate",
     "vocabulary":["veikur","veikindi","atvinnurekstur","yfirmaður","fjarvera","læknisvottorð","dagur","skilaboð","mótaðili","kveðja"],
     "system_addon":"You are the student's supervisor receiving a sick-call phone call. Ask what's wrong, how long they'll be out, if they need a doctor's note. Phone conversation register — more formal than texting. Student practices phone Icelandic and illness vocabulary."},
    {"id":"S25","category":"work","title":"Renting an Apartment",
     "icon":"🏢","description":"View a flat, ask about utilities and terms, discuss the lease.",
     "sigridur_role":"landlord showing an apartment","level":"intermediate",
     "vocabulary":["íbúð","leiga","rafmagn","hiti","samningur","trygging","gæðareyðublað","eigandi","leigjandi","uppsagnarfrestur"],
     "system_addon":"You are a landlord showing a Reykjavík apartment. Student is viewing it as a potential tenant. Show them around, answer questions about rent, utilities included (geothermal heating is cheap in Iceland!), deposit, notice period. Practical property Icelandic."},

    # ── Culture & Leisure ─────────────────────────────────────────────────────
    {"id":"S26","category":"culture","title":"At a Museum",
     "icon":"🏛️","description":"Ask about exhibits, audio guides, and Icelandic history.",
     "sigridur_role":"museum guide","level":"intermediate",
     "vocabulary":["sýning","þjóðmenning","grípur","fornleifar","lýsing","hljóðleiðsögn","miði","opnunartímar","söfnuður","saga"],
     "system_addon":"You are a guide at the National Museum of Iceland (Þjóðminjasafn). Welcome the student, tell them about current exhibits, offer audio guide, answer questions about Icelandic artefacts and history. Cultural, educational Icelandic."},
    {"id":"S27","category":"culture","title":"Booking a Tour",
     "icon":"🚌","description":"Book a Golden Circle, whale watching, or Northern Lights tour.",
     "sigridur_role":"tour booking agent","level":"beginner",
     "vocabulary":["ferð","túr","bókun","gullni hringurinn","hvalaskoðun","norðurljós","verð","tímasetning","mætingarstaður","afbókun"],
     "system_addon":"You are a tour booking agent in Reykjavík. The student wants to book a day trip. Offer Golden Circle, whale watching from Old Harbor, Northern Lights hunt. Discuss prices, pick-up times, what to wear. Friendly tourism Icelandic."},
    {"id":"S28","category":"culture","title":"At a Geothermal Pool",
     "icon":"♨️","description":"Navigate pool etiquette, ask about facilities, make conversation.",
     "sigridur_role":"pool attendant and fellow swimmer","level":"beginner",
     "vocabulary":["sundlaug","heit pot","kaldur pottur","búningsherbergi","handklæði","sápa","reglur","laugarvatn","hlýr","slaka á"],
     "system_addon":"First play a pool attendant explaining rules (must shower without swimsuit before entering, no outdoor shoes on pool deck). Then switch to being a friendly local in the hot pot making conversation. Icelanders love chatting in hot tubs! Relaxed social Icelandic."},
    {"id":"S29","category":"social","title":"Watching Football / Sport",
     "icon":"⚽","description":"Watch a match, discuss teams, celebrate or commiserate.",
     "sigridur_role":"fellow sports fan","level":"intermediate",
     "vocabulary":["fótbolti","lið","mark","leikur","sigur","tap","leikmaður","deild","bikar","knattspyrna"],
     "system_addon":"You are a passionate Icelandic football fan watching a match — maybe KR vs Breiðablik, or the national team. Discuss the game, players, tactics. React to goals together. Iceland's famous 2016 Euro run is always worth mentioning. Passionate but friendly sports Icelandic."},
    {"id":"S30","category":"nature","title":"Hiking & Nature Walk",
     "icon":"🥾","description":"Ask about trails, check weather safety, discuss gear and distances.",
     "sigridur_role":"hiking guide","level":"intermediate",
     "vocabulary":["ganga","stígur","fjall","hæð","veðurspá","búnaður","skór","vatnsflaska","kort","öryggi"],
     "system_addon":"You are a hiking guide at a visitor centre near Landmannalaugar or Þórsmörk. Advise the student on trail difficulty, current weather, what to bring, emergency procedures. Safety-focused but encouraging. Outdoor activity Icelandic."},

    # ── Emergency & Practical ─────────────────────────────────────────────────
    {"id":"S31","category":"emergency","title":"At the Hospital / Emergency Room",
     "icon":"🚑","description":"Check in, describe pain level and symptoms, understand instructions.",
     "sigridur_role":"emergency room nurse","level":"intermediate",
     "vocabulary":["bráðamóttaka","verkur","meiðsli","blóðþrýstingur","hiti","öndun","sjúkraliði","bið","lyf","meðferð"],
     "system_addon":"You are a nurse at Landspítali emergency room. Triage the student: ask about pain level (1-10), symptoms, duration, allergies. Explain wait times, procedures. Clear, calm medical Icelandic. Not for actual medical advice — this is language practice."},
    {"id":"S32","category":"emergency","title":"Car Breakdown",
     "icon":"🔧","description":"Call for roadside assistance, describe your location and problem.",
     "sigridur_role":"roadside assistance operator","level":"intermediate",
     "vocabulary":["bilun","dekk","flat dekk","vélin","bíllinn","staðsetning","vegur","hjálp","dráttarbíll","bíða"],
     "system_addon":"You are a roadside assistance operator at Félag íslenskra bifreiðaeigenda (FÍB). The student's car has broken down. Ask for their location (which road, km marker), what happened, what they can see. Send help, give safety instructions. Practical emergency Icelandic."},
    {"id":"S33","category":"emergency","title":"Reporting a Problem to a Landlord",
     "icon":"🔨","description":"Report broken heating, water leak, or other flat problems formally.",
     "sigridur_role":"landlord receiving a complaint","level":"intermediate",
     "vocabulary":["hitaveita","leki","kaldur","tjón","viðgerð","iðnaðarmaður","brýnt","tilkynna","samningur","réttur"],
     "system_addon":"You are a landlord receiving a call about a problem in your rental property. Student reports something broken — heating failure in winter, water leak, broken lock. Respond professionally, ask for details, arrange a repair. Formal complaint/response Icelandic."},
    {"id":"S34","category":"emergency","title":"At Customs / Immigration",
     "icon":"🛂","description":"Answer questions about your visit — purpose, duration, accommodation.",
     "sigridur_role":"border control officer","level":"beginner",
     "vocabulary":["vegabréf","dvöl","tilgangur","gisting","flugmiði","heimilisfang","ferðamaður","vinna","dvalarleyfi","Schengen"],
     "system_addon":"You are a border control officer at Keflavík airport. Ask the student standard entry questions: purpose of visit, how long they're staying, where they're staying, do they have a return ticket. Formal but not unfriendly. Routine customs Icelandic."},
    {"id":"S35","category":"emergency","title":"Weather Emergency",
     "icon":"🌨️","description":"Respond to storm warnings, road closures, and safety advisories.",
     "sigridur_role":"emergency broadcast and local neighbour","level":"intermediate",
     "vocabulary":["storm","vegalokanir","hlébarði","veðurviðvörun","almannavarnir","birgðir","skjól","vegurinn","hætta","öruggur"],
     "system_addon":"First play an emergency radio broadcast warning about a severe storm (veðurviðvörun), then switch to being a concerned neighbour checking if the student is prepared. Discuss what to do: stay indoors, stock water and food, check road.is for closures. Safety-focused Icelandic."},

    # ── Unique to Iceland ─────────────────────────────────────────────────────
    {"id":"S36","category":"nature","title":"Watching the Northern Lights",
     "icon":"🌌","description":"Join a Northern Lights tour — describe what you see, ask questions.",
     "sigridur_role":"Northern Lights guide","level":"beginner",
     "vocabulary":["norðurljós","loft","ljós","litir","grænur","fjólublár","hreyfing","mynd","ljósmyndun","náttúruvætti"],
     "system_addon":"You are a Northern Lights guide on a tour outside Reykjavík on a clear night. Describe what's appearing in the sky, explain the science simply, help the student describe the colors and movement. Magical, enthusiastic Icelandic. Teach color and nature description vocabulary."},
    {"id":"S37","category":"nature","title":"Visiting a Volcano / Lava Field",
     "icon":"🌋","description":"Guided walk on a lava field — geology vocabulary, safety briefing.",
     "sigridur_role":"volcanology guide","level":"intermediate",
     "vocabulary":["eldfjall","hraun","gígur","gossprungur","öskufall","jarðhiti","jarðfræði","kaldur hraun","hellir","eldgos"],
     "system_addon":"You are a volcanology guide at Reykjanes peninsula or near Fagradalsfjall. Brief the student on safety, explain the types of lava (pahoehoe vs a'a), volcanic activity. Iceland's recent eruptions (2021-2024) are great conversation. Geological Icelandic, intermediate level."},
    {"id":"S38","category":"culture","title":"The Midnight Sun",
     "icon":"☀️","description":"Discuss the midnight sun phenomenon — how it affects life and sleep.",
     "sigridur_role":"local Icelander in summer","level":"beginner",
     "vocabulary":["miðnæturssól","ljós","myrkur","svefn","gluggablindur","sumar","vetur","birta","sólsetur","sólris"],
     "system_addon":"You are a local Icelander chatting with the student during summer when the sun barely sets. Explain how Icelanders cope (blackout curtains, later schedules), how it feels, what activities people do at midnight. Conversational summer Icelandic. Fun cultural exchange."},
    {"id":"S39","category":"culture","title":"Viking History Tour at Þingvellir",
     "icon":"⚔️","description":"Tour the site of the world's first parliament — historical and cultural vocabulary.",
     "sigridur_role":"historical guide at Þingvellir","level":"intermediate",
     "vocabulary":["Þingvellir","Alþingi","þingmaður","lögberg","goði","lög","dómur","landnám","saga","þjóðgarður"],
     "system_addon":"You are a guide at Þingvellir National Park, site of the original Alþingi (930 AD). Explain the geography (the rift valley between North American and Eurasian plates!), the history of the parliament, famous events from the sagas that took place here. Rich historical and geological Icelandic."},
    {"id":"S40","category":"nature","title":"Icelandic Horse Riding",
     "icon":"🐴","description":"Book a riding tour, learn horse vocabulary, experience the tölt gait.",
     "sigridur_role":"riding instructor at a horse farm","level":"beginner",
     "vocabulary":["hestur","tölt","brokk","stigi","hnakkur","taumur","hjálmur","búsáhald","bú","kynþáttur"],
     "system_addon":"You are an instructor at an Icelandic horse farm. Fit the student with a helmet, introduce them to their horse, explain the unique five-gaited Icelandic horse and the famous tölt. Lead a short guided ride, teach commands for the horse. Enthusiastic equestrian Icelandic. The Icelandic horse is a point of national pride!"},
]

# Grammar category classifier for error heatmap
GRAMMAR_CATEGORIES = [
    "case_nominative","case_accusative","case_dative","case_genitive",
    "verb_conjugation","verb_tense","noun_gender","adjective_agreement",
    "word_order","pronunciation","vocabulary","spelling","other"
]

# ═══════════════════════════════════════════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════════════════════════════════════════
BASE_SYSTEM = """You are Sigríður, a warm and encouraging Icelandic language tutor.

YOUR ROLE:
- Converse naturally with the student IN ICELANDIC
- Keep responses appropriate to their level
- After EVERY response, provide an English feedback block
- Extract vocabulary worth saving as flashcards
- Always provide a natural English translation of your Icelandic response

RESPONSE FORMAT — always return valid JSON:
{{
  "icelandic": "Your Icelandic response (spoken aloud)",
  "english_translation": "Natural English translation of your Icelandic response",
  "english_correction": {{
    "errors": [
      {{"original":"what they said","correction":"correct form",
        "explanation":"why in English","grammar_category":"case_accusative|verb_conjugation|noun_gender|adjective_agreement|word_order|vocabulary|spelling|other"}}
    ],
    "positive": "One thing they did well",
    "tip": "One grammar/vocab tip (optional)"
  }},
  "difficulty_assessment": "beginner|intermediate|advanced",
  "new_vocabulary": [
    {{"icelandic":"word","english":"translation","notes":"usage note","category":"vocabulary|grammar|phrase","part_of_speech":"noun|verb|adjective|adverb|preposition|conjunction|pronoun|phrase|other"}}
  ],
  "lesson_progress": {{
    "goal_met": false,
    "goal_percent": 0,
    "goal_note": "one short sentence on how close to the lesson goal (only in lesson mode)"
  }}
}}

grammar_category must be one of: case_nominative, case_accusative, case_dative, case_genitive,
verb_conjugation, verb_tense, noun_gender, adjective_agreement, word_order, pronunciation,
vocabulary, spelling, other.

Extract 0-3 vocabulary items per turn.
Keep Icelandic responses concise (2-4 sentences).
"""

FLASHCARD_GEN_PROMPT = """Icelandic language expert. Generate {count} flashcards for a {level} learner on: {topic}
Return ONLY a JSON array, no markdown:
[{{"icelandic":"...","english":"...","notes":"...","category":"vocabulary|grammar|phrase","part_of_speech":"noun|verb|adjective|adverb|preposition|conjunction|pronoun|phrase|other"}}]
"""

HEATMAP_ANALYSIS_PROMPT = """You are an Icelandic language expert analyzing a student's error patterns.

Given these error records, identify:
1. Their top 3 weakest grammar areas
2. Specific recurring mistakes with examples
3. Targeted practice recommendations

Error data:
{errors}

Return JSON:
{{
  "weakest_areas": [
    {{"category":"case_accusative","count":5,"percentage":35,"display_name":"Accusative Case",
      "description":"Brief explanation of the pattern","example_errors":[{{"original":"...","correction":"..."}}]}}
  ],
  "recurring_mistakes": [
    {{"pattern":"description","frequency":3,"example_original":"...","example_correction":"...","fix":"how to fix"}}
  ],
  "recommendations": [
    {{"action":"specific practice exercise","priority":"high|medium|low"}}
  ],
  "overall_assessment": "2-3 sentence summary of the student's strengths and areas to improve"
}}
"""

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def now_iso(): return datetime.now(timezone.utc).isoformat()
def today_iso(): return datetime.now(timezone.utc).date().isoformat()

def sm2(ease, interval, correct):
    if correct: return min(2.5, max(1.3, ease+0.1)), max(1, round(interval*ease))
    return max(1.3, ease-0.2), 1

async def call_ollama(messages, system):
    payload = {"model":OLLAMA_MODEL,
               "messages":[{"role":"system","content":system}]+messages,
               "stream":False}
    async with httpx.AsyncClient(timeout=270) as c:
        r = await c.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]["content"]

async def call_anthropic(messages, system, max_tokens=1500):
    payload = {"model":ANTHROPIC_MODEL,"max_tokens":max_tokens,"system":system,"messages":messages}
    headers = {"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",json=payload,headers=headers)
        r.raise_for_status()
        return r.json()["content"][0]["text"]

async def call_llm(messages, system, max_tokens=1500):
    if LLM_PROVIDER=="ollama": return await call_ollama(messages,system)
    return await call_anthropic(messages,system,max_tokens)

def _unescaped_quote(s):
    """Return index of first unescaped double-quote in s, or -1."""
    i = 0
    while i < len(s):
        if s[i] == '\\':
            i += 2
            continue
        if s[i] == '"':
            return i
        i += 1
    return -1

async def stream_ollama(messages, system):
    payload = {"model":OLLAMA_MODEL,
               "messages":[{"role":"system","content":system}]+messages,
               "stream":True,"format":"json"}
    async with httpx.AsyncClient(timeout=270) as c:
        async with c.stream("POST", f"{OLLAMA_BASE_URL}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    token = json.loads(line).get("message",{}).get("content","")
                    if token:
                        yield token
                except json.JSONDecodeError:
                    continue

async def stream_anthropic(messages, system):
    payload = {"model":ANTHROPIC_MODEL,"max_tokens":1500,"system":system,
               "messages":messages,"stream":True}
    headers = {"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
               "content-type":"application/json"}
    async with httpx.AsyncClient(timeout=60) as c:
        async with c.stream("POST","https://api.anthropic.com/v1/messages",
                            json=payload,headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    evt = json.loads(line[6:])
                    if evt.get("type") == "content_block_delta":
                        token = evt.get("delta",{}).get("text","")
                        if token:
                            yield token
                except json.JSONDecodeError:
                    continue

async def stream_llm(messages, system):
    if LLM_PROVIDER == "ollama":
        async for chunk in stream_ollama(messages, system):
            yield chunk
    else:
        async for chunk in stream_anthropic(messages, system):
            yield chunk

def parse_json(raw):
    clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    clean = clean.lstrip("```json").lstrip("```").rstrip("```").strip()
    try: return json.loads(clean)
    except: return {"icelandic":raw,"english_correction":{"errors":[],"positive":"","tip":""},
                    "difficulty_assessment":"beginner","new_vocabulary":[],"lesson_progress":{}}

def build_system_prompt(mode, scenario_id, lesson_id, level):
    system = BASE_SYSTEM
    if mode=="scenario" and scenario_id:
        sc = next((s for s in SCENARIOS if s["id"]==scenario_id), None)
        if sc:
            system += f"\n\nSCENARIO MODE — {sc['title']}\n{sc['system_addon']}\n"
            system += f"\nVocabulary to introduce: {', '.join(sc['vocabulary'])}"
    elif mode=="lesson" and lesson_id:
        ls = next((l for l in LESSONS if l["id"]==lesson_id), None)
        if ls:
            system += f"\n\nLESSON MODE — {ls['title']}\nGrammar focus: {ls['grammar_focus']}\n"
            system += f"Lesson goal: {ls['goal']}\n{ls['system_addon']}\n"
            system += f"Key vocabulary: {', '.join(ls['vocabulary'])}\n"
            system += "\nTrack goal_percent (0-100) and set goal_met=true when the student has achieved the lesson goal."
    system += f"\n\n[Student level: {level}]"
    return system

# ═══════════════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════════════
async def _prefetch_wotd():
    """Pre-generate word of the day if not already cached."""
    today = today_iso()
    with get_db() as db:
        row = db.execute("SELECT 1 FROM word_of_day WHERE date=?", (today,)).fetchone()
        if row:
            return
    try:
        raw = await call_llm(
            [{"role": "user", "content": "Generate today's Icelandic word of the day."}],
            system=WOTD_PROMPT,
            max_tokens=400,
        )
        clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if not match:
            raise ValueError("no JSON object found")
        data = json.loads(match.group())
        with get_db() as db:
            db.execute(
                """INSERT OR REPLACE INTO word_of_day
                   (date, word, english, part_of_speech, example_is, example_en, etymology, difficulty, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (today, data.get("word", ""), data.get("english", ""),
                 data.get("part_of_speech", ""), data.get("example_is", ""),
                 data.get("example_en", ""), data.get("etymology", ""),
                 data.get("difficulty", "beginner"), now_iso()),
            )
            db.commit()
        logging.info("WOTD pre-fetched: %s", data.get("word", "?"))
    except Exception as exc:
        logging.error("WOTD prefetch failed: %s", exc)


async def _wotd_scheduler():
    """Background task: generate word of the day at 06:00 UTC each day."""
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await _prefetch_wotd()


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_wotd_scheduler())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Icelandic Tutor v3", lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)

# ── Models ────────────────────────────────────────────────────────────────────
class Msg(BaseModel):
    role: Literal["user","assistant"]
    content: str

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    messages: list[Msg]
    level: Literal["beginner","intermediate","advanced"] = "beginner"
    mode: Literal["free","scenario","lesson"] = "free"
    scenario_id: Optional[str] = None
    lesson_id:   Optional[str] = None

class FlashcardReview(BaseModel):
    card_id: int; correct: bool

class FlashcardCreate(BaseModel):
    icelandic: str; english: str
    notes: Optional[str] = ""
    category: str = "vocabulary"
    part_of_speech: Optional[str] = ""

class FlashcardGenReq(BaseModel):
    count: int = 10; level: str = "beginner"
    topic: str = "common greetings and everyday vocabulary"

class LessonProgressUpdate(BaseModel):
    lesson_id: str; completed: bool; score: int = 0; session_id: Optional[str]=None

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health(): return {"status":"ok","llm":LLM_PROVIDER}

# ═══════════════════════════════════════════════════════════════════════════════
# CHAT
# ═══════════════════════════════════════════════════════════════════════════════
@app.post("/chat")
async def chat(req: ChatRequest):
    sid = req.session_id or str(uuid.uuid4())

    # Fire RAG immediately so the HTTP request is in-flight while we do sync work below.
    last_user_text = next((m.content for m in reversed(req.messages) if m.role=="user"), "")
    rag_task = asyncio.create_task(retrieve_context(last_user_text, top_k=3)) if last_user_text else None
    await asyncio.sleep(0)  # yield so the task starts its HTTP request before we block

    with get_db() as db:
        if not db.execute("SELECT id FROM sessions WHERE id=?",(sid,)).fetchone():
            first = next((m.content for m in req.messages if m.role=="user"),"New session")
            title = first[:60]+("…" if len(first)>60 else "")
            db.execute("INSERT INTO sessions(id,title,level,mode,scenario_id,lesson_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (sid,title,req.level,req.mode,req.scenario_id,req.lesson_id,now_iso(),now_iso()))
            db.commit()

    system = build_system_prompt(req.mode, req.scenario_id, req.lesson_id, req.level)
    msgs = [{"role":m.role,"content":m.content} for m in req.messages[-6:]]

    # Collect RAG result — has been running concurrently during the sync work above.
    if rag_task:
        rag_context = await rag_task
        if rag_context:
            system += f"""

REFERENCE MATERIAL from student's Icelandic grammar books (use when relevant to correct or explain):
{rag_context}

When this material is relevant, naturally reference it in your tip or correction (e.g. "As your grammar book explains..."). Do not force it into every response."""

    try: raw = await call_llm(msgs, system)
    except Exception as e: raise HTTPException(502,f"LLM failed: {e}")

    data       = parse_json(raw)
    correction = data.get("english_correction",{})
    new_vocab  = data.get("new_vocabulary",[])
    lp         = data.get("lesson_progress",{})

    with get_db() as db:
        last_user = next((m for m in reversed(req.messages) if m.role=="user"),None)
        if last_user:
            db.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                       (sid,"user",last_user.content,now_iso()))
        db.execute("INSERT INTO messages(session_id,role,content,icelandic,correction,created_at) VALUES(?,?,?,?,?,?)",
                   (sid,"assistant",data.get("icelandic",""),data.get("icelandic",""),json.dumps(correction),now_iso()))
        db.execute("UPDATE sessions SET updated_at=?,level=?,turn_count=turn_count+1 WHERE id=?",
                   (now_iso(),req.level,sid))
        today = today_iso()
        errors_n = len(correction.get("errors",[]))
        if db.execute("SELECT id FROM progress WHERE session_id=? AND date=?",(sid,today)).fetchone():
            db.execute("UPDATE progress SET turns=turns+1,errors_made=errors_made+? WHERE session_id=? AND date=?",
                       (errors_n,sid,today))
        else:
            db.execute("INSERT INTO progress(session_id,date,turns,errors_made,level) VALUES(?,?,1,?,?)",
                       (sid,today,errors_n,req.level))
        # Log errors with grammar category for heatmap
        for err in correction.get("errors",[]):
            gc = err.get("grammar_category","other")
            if gc not in GRAMMAR_CATEGORIES: gc = "other"
            db.execute("INSERT INTO error_log(session_id,date,error_type,original,correction,explanation,grammar_category) VALUES(?,?,?,?,?,?,?)",
                       (sid,today,gc,err.get("original",""),err.get("correction",""),err.get("explanation",""),gc))
        # Save vocabulary (INSERT OR IGNORE deduplicates on icelandic text)
        due = today
        for v in new_vocab:
            if v.get("icelandic") and v.get("english"):
                db.execute("INSERT OR IGNORE INTO flashcards(icelandic,english,notes,category,part_of_speech,due_date,created_at,source_session) VALUES(?,?,?,?,?,?,?,?)",
                           (v["icelandic"],v["english"],v.get("notes",""),v.get("category","vocabulary"),v.get("part_of_speech",""),due,now_iso(),sid))
        # Auto-complete lesson when goal met
        lesson_just_completed = False
        if req.mode=="lesson" and req.lesson_id and lp.get("goal_met"):
            already = db.execute("SELECT id FROM lesson_progress WHERE lesson_id=? AND completed=1",(req.lesson_id,)).fetchone()
            if not already:
                db.execute("INSERT INTO lesson_progress(lesson_id,completed,score,completed_at,session_id) VALUES(?,1,100,?,?)",
                           (req.lesson_id,now_iso(),sid))
                lesson_just_completed = True
        db.commit()

    return {"session_id":sid,"icelandic":data.get("icelandic",""),
            "english_translation":data.get("english_translation",""),
            "english_correction":correction,
            "difficulty_assessment":data.get("difficulty_assessment",req.level),
            "new_vocabulary":new_vocab,"lesson_progress":lp,
            "lesson_just_completed":lesson_just_completed,
            "mode":req.mode}

@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    sid = req.session_id or str(uuid.uuid4())

    last_user_text = next((m.content for m in reversed(req.messages) if m.role=="user"), "")
    rag_task = asyncio.create_task(retrieve_context(last_user_text, top_k=3)) if last_user_text else None
    await asyncio.sleep(0)

    with get_db() as db:
        if not db.execute("SELECT id FROM sessions WHERE id=?",(sid,)).fetchone():
            first = next((m.content for m in req.messages if m.role=="user"),"New session")
            title = first[:60]+("…" if len(first)>60 else "")
            db.execute("INSERT INTO sessions(id,title,level,mode,scenario_id,lesson_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (sid,title,req.level,req.mode,req.scenario_id,req.lesson_id,now_iso(),now_iso()))
            db.commit()

    system = build_system_prompt(req.mode, req.scenario_id, req.lesson_id, req.level)
    msgs = [{"role":m.role,"content":m.content} for m in req.messages[-6:]]

    if rag_task:
        rag_context = await rag_task
        if rag_context:
            system += f"""

REFERENCE MATERIAL from student's Icelandic grammar books (use when relevant to correct or explain):
{rag_context}

When this material is relevant, naturally reference it in your tip or correction (e.g. "As your grammar book explains..."). Do not force it into every response."""

    async def generate():
        full_buffer = ""
        scan_from   = 0
        in_is       = False   # inside the "icelandic" value
        is_done     = False   # finished extracting icelandic value
        MARKER      = '"icelandic": "'

        model_name = OLLAMA_MODEL if LLM_PROVIDER == "ollama" else ANTHROPIC_MODEL
        t_llm  = time.monotonic()
        first  = True

        with tracer.start_as_current_span("llm.stream") as llm_span:
            llm_span.set_attribute("llm.provider", LLM_PROVIDER)
            llm_span.set_attribute("llm.model",    model_name)
            llm_span.set_attribute("chat.level",   req.level)
            llm_span.set_attribute("chat.mode",    req.mode)
            try:
                async for chunk in stream_llm(msgs, system):
                    if first:
                        ttft = time.monotonic() - t_llm
                        CHAT_TTFT.labels(provider=LLM_PROVIDER).observe(ttft)
                        llm_span.add_event("first_token", {"ttft_ms": round(ttft * 1000)})
                        first = False

                    full_buffer += chunk
                    if is_done:
                        continue
                    if not in_is:
                        idx = full_buffer.find(MARKER)
                        if idx < 0:
                            continue
                        in_is     = True
                        scan_from = idx + len(MARKER)

                    new_text = full_buffer[scan_from:]
                    end      = _unescaped_quote(new_text)
                    if end >= 0:
                        to_emit  = new_text[:end]
                        in_is    = False
                        is_done  = True
                    else:
                        to_emit   = new_text
                        scan_from = len(full_buffer)

                    if to_emit:
                        yield f'data: {json.dumps({"t":"tok","v":to_emit})}\n\n'

            except Exception as e:
                logger.error(f"stream_llm error: {e}")
                llm_span.record_exception(e)
                yield f'data: {json.dumps({"t":"error","msg":"LLM connection failed"})}\n\n'
                return
            finally:
                LLM_DURATION.labels(provider=LLM_PROVIDER, model=model_name).observe(
                    time.monotonic() - t_llm)

        # ── post-stream: parse, persist, send done event ──────────────────────
        data       = parse_json(full_buffer)
        correction = data.get("english_correction",{})
        new_vocab  = data.get("new_vocabulary",[])
        lp         = data.get("lesson_progress",{})

        with get_db() as db:
            last_user = next((m for m in reversed(req.messages) if m.role=="user"),None)
            if last_user:
                db.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                           (sid,"user",last_user.content,now_iso()))
            db.execute("INSERT INTO messages(session_id,role,content,icelandic,correction,created_at) VALUES(?,?,?,?,?,?)",
                       (sid,"assistant",data.get("icelandic",""),data.get("icelandic",""),json.dumps(correction),now_iso()))
            db.execute("UPDATE sessions SET updated_at=?,level=?,turn_count=turn_count+1 WHERE id=?",
                       (now_iso(),req.level,sid))
            today    = today_iso()
            errors_n = len(correction.get("errors",[]))
            if db.execute("SELECT id FROM progress WHERE session_id=? AND date=?",(sid,today)).fetchone():
                db.execute("UPDATE progress SET turns=turns+1,errors_made=errors_made+? WHERE session_id=? AND date=?",
                           (errors_n,sid,today))
            else:
                db.execute("INSERT INTO progress(session_id,date,turns,errors_made,level) VALUES(?,?,1,?,?)",
                           (sid,today,errors_n,req.level))
            for err in correction.get("errors",[]):
                gc = err.get("grammar_category","other")
                if gc not in GRAMMAR_CATEGORIES: gc = "other"
                db.execute("INSERT INTO error_log(session_id,date,error_type,original,correction,explanation,grammar_category) VALUES(?,?,?,?,?,?,?)",
                           (sid,today,gc,err.get("original",""),err.get("correction",""),err.get("explanation",""),gc))
                GRAMMAR_ERRORS.labels(category=gc).inc()
            for v in new_vocab:
                if v.get("icelandic") and v.get("english"):
                    db.execute("INSERT OR IGNORE INTO flashcards(icelandic,english,notes,category,part_of_speech,due_date,created_at,source_session) VALUES(?,?,?,?,?,?,?,?)",
                               (v["icelandic"],v["english"],v.get("notes",""),v.get("category","vocabulary"),v.get("part_of_speech",""),today,now_iso(),sid))
            # Auto-complete lesson when goal met
            lesson_just_completed = False
            if req.mode=="lesson" and req.lesson_id and lp.get("goal_met"):
                already = db.execute("SELECT id FROM lesson_progress WHERE lesson_id=? AND completed=1",(req.lesson_id,)).fetchone()
                if not already:
                    db.execute("INSERT INTO lesson_progress(lesson_id,completed,score,completed_at,session_id) VALUES(?,1,100,?,?)",
                               (req.lesson_id,now_iso(),sid))
                    lesson_just_completed = True
            db.commit()

        yield f'data: {json.dumps({"t":"done","session_id":sid,"icelandic":data.get("icelandic",""),"english_translation":data.get("english_translation",""),"english_correction":correction,"new_vocabulary":new_vocab,"lesson_progress":lp,"lesson_just_completed":lesson_just_completed,"mode":req.mode})}\n\n'

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"},
    )

# ═══════════════════════════════════════════════════════════════════════════════
# SESSIONS
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/sessions")
def list_sessions(limit:int=30):
    with get_db() as db:
        rows = db.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",(limit,)).fetchall()
    return [dict(r) for r in rows]

@app.get("/sessions/{sid}")
def get_session(sid:str):
    with get_db() as db:
        s = db.execute("SELECT * FROM sessions WHERE id=?",(sid,)).fetchone()
        if not s: raise HTTPException(404,"Not found")
        msgs = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at",(sid,)).fetchall()
    result = dict(s); result["messages"]=[]
    for m in msgs:
        md=dict(m)
        if md.get("correction"):
            try: md["correction"]=json.loads(md["correction"])
            except: pass
        result["messages"].append(md)
    return result

@app.delete("/sessions/{sid}")
def delete_session(sid:str):
    with get_db() as db:
        for t,col in [("messages","session_id"),("progress","session_id"),
                      ("error_log","session_id"),("sessions","id")]:
            db.execute(f"DELETE FROM {t} WHERE {col}=?",(sid,))
        db.commit()
    return {"deleted":sid}

# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIOS
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/scenarios")
def list_scenarios(category:Optional[str]=None, level:Optional[str]=None):
    items = SCENARIOS
    if category: items=[s for s in items if s["category"]==category]
    if level:    items=[s for s in items if s["level"]==level]
    return items

@app.get("/scenarios/{sid}")
def get_scenario(sid:str):
    sc = next((s for s in SCENARIOS if s["id"]==sid),None)
    if not sc: raise HTTPException(404,"Scenario not found")
    return sc

# ═══════════════════════════════════════════════════════════════════════════════
# LESSONS
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/lessons")
def list_lessons(track:Optional[str]=None):
    with get_db() as db:
        completed = {r["lesson_id"] for r in db.execute(
            "SELECT DISTINCT lesson_id FROM lesson_progress WHERE completed=1").fetchall()}
    items = LESSONS
    if track: items=[l for l in items if l["track"]==track]
    return [{"completed": l["id"] in completed, **l} for l in items]

@app.get("/lessons/{lid}")
def get_lesson(lid:str):
    ls = next((l for l in LESSONS if l["id"]==lid),None)
    if not ls: raise HTTPException(404,"Lesson not found")
    with get_db() as db:
        prog = db.execute("SELECT * FROM lesson_progress WHERE lesson_id=? ORDER BY id DESC LIMIT 1",(lid,)).fetchone()
    return {**ls,"progress":dict(prog) if prog else None}

@app.post("/lessons/complete")
def complete_lesson(upd: LessonProgressUpdate):
    with get_db() as db:
        db.execute("INSERT INTO lesson_progress(lesson_id,completed,score,completed_at,session_id) VALUES(?,?,?,?,?)",
                   (upd.lesson_id,1 if upd.completed else 0,upd.score,now_iso(),upd.session_id))
        db.commit()
    return {"lesson_id":upd.lesson_id,"completed":upd.completed,"score":upd.score}

# ═══════════════════════════════════════════════════════════════════════════════
# PROGRESS
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/progress")
def get_progress(days:int=30):
    with get_db() as db:
        daily = db.execute("""SELECT date,SUM(turns) as turns,SUM(errors_made) as errors_made,
            SUM(errors_corrected) as errors_corrected,MAX(level) as level
            FROM progress WHERE date>=date('now',?) GROUP BY date ORDER BY date ASC""",
            (f"-{days} days",)).fetchall()
        totals = db.execute("""SELECT SUM(turns) as total_turns,SUM(errors_made) as total_errors,
            COUNT(DISTINCT session_id) as total_sessions,COUNT(DISTINCT date) as active_days
            FROM progress""").fetchone()
        cards_total = db.execute("SELECT COUNT(*) as n FROM flashcards").fetchone()["n"]
        cards_due   = db.execute("SELECT COUNT(*) as n FROM flashcards WHERE due_date<=date('now')").fetchone()["n"]
        lessons_done = db.execute("SELECT COUNT(DISTINCT lesson_id) as n FROM lesson_progress WHERE completed=1").fetchone()["n"]
        completed_lessons = [r["lesson_id"] for r in db.execute(
            "SELECT DISTINCT lesson_id FROM lesson_progress WHERE completed=1").fetchall()]
    return {"daily":[dict(r) for r in daily],"totals":dict(totals),
            "cards_total":cards_total,"cards_due":cards_due,
            "lessons_completed":lessons_done,"completed_lessons":completed_lessons}

# ═══════════════════════════════════════════════════════════════════════════════
# HEATMAP / ERROR ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/heatmap")
def get_heatmap(days:int=90):
    with get_db() as db:
        errors = db.execute("""
            SELECT grammar_category,COUNT(*) as count,
                   GROUP_CONCAT(original,'|||') as originals,
                   GROUP_CONCAT(correction,'|||') as corrections
            FROM error_log WHERE date>=date('now',?)
            GROUP BY grammar_category ORDER BY count DESC
        """,(f"-{days} days",)).fetchall()
        total_errors = db.execute(
            "SELECT COUNT(*) as n FROM error_log WHERE date>=date('now',?)",(f"-{days} days",)).fetchone()["n"]
        daily_errors = db.execute("""
            SELECT date,grammar_category,COUNT(*) as count
            FROM error_log WHERE date>=date('now',?)
            GROUP BY date,grammar_category ORDER BY date ASC
        """,(f"-{days} days",)).fetchall()
    categories = []
    for row in errors:
        r = dict(row)
        pct = round(r["count"]/total_errors*100) if total_errors else 0
        origs = (r["originals"] or "").split("|||")[:3]
        corrs = (r["corrections"] or "").split("|||")[:3]
        examples = [{"original":o,"correction":c} for o,c in zip(origs,corrs) if o]
        categories.append({
            "category": r["grammar_category"],
            "display_name": r["grammar_category"].replace("_"," ").title(),
            "count": r["count"],
            "percentage": pct,
            "examples": examples,
        })
    return {"categories":categories,"total_errors":total_errors,
            "daily":[dict(r) for r in daily_errors]}

@app.get("/heatmap/analysis")
async def get_heatmap_analysis(days:int=90):
    """Ask the LLM to analyze error patterns and give recommendations."""
    with get_db() as db:
        errors = db.execute("""
            SELECT grammar_category,original,correction,explanation,date
            FROM error_log WHERE date>=date('now',?) ORDER BY date DESC LIMIT 100
        """,(f"-{days} days",)).fetchall()
    if not errors:
        return {"weakest_areas":[],"recurring_mistakes":[],"recommendations":[],
                "overall_assessment":"No errors logged yet — start practicing to see your analysis!"}
    error_data = json.dumps([dict(r) for r in errors], ensure_ascii=False)
    system = HEATMAP_ANALYSIS_PROMPT.format(errors=error_data)
    try:
        raw = await call_llm([{"role":"user","content":"Analyze these errors."}],system,max_tokens=1500)
    except Exception as e:
        raise HTTPException(502,f"LLM error: {e}")
    return parse_json(raw)

# ═══════════════════════════════════════════════════════════════════════════════
# PRONUNCIATION — proxy to pronunciation service
# ═══════════════════════════════════════════════════════════════════════════════
@app.post("/pronunciation/score")
async def score_pronunciation(
    audio: UploadFile = File(...),
    expected_text: str = Form(""),
    session_id: str = Form(""),
):
    """Proxy to pronunciation service and log the result."""
    audio_bytes = await audio.read()
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{PRONUN_URL}/score",
                files={"audio": (audio.filename, audio_bytes, audio.content_type)},
                data={"expected_text": expected_text})
            r.raise_for_status()
            result = r.json()
    except Exception as e:
        raise HTTPException(502, f"Pronunciation service error: {e}")

    # Log result
    if session_id:
        with get_db() as db:
            db.execute("""INSERT INTO pronunciation_log
                (session_id,date,expected_text,spoken_text,overall_score,word_scores,phoneme_tips)
                VALUES(?,?,?,?,?,?,?)""",
                (session_id,today_iso(),expected_text,
                 result.get("spoken_text",""),result.get("overall_score",0),
                 json.dumps(result.get("word_scores",[])),
                 json.dumps(result.get("phoneme_tips",[]))))
            db.commit()
    if isinstance(result, dict):
        PRON_SCORE.observe(result.get("overall_score", 0))
    return result

@app.get("/pronunciation/history")
def get_pronunciation_history(session_id:Optional[str]=None, limit:int=20):
    with get_db() as db:
        if session_id:
            rows = db.execute(
                "SELECT * FROM pronunciation_log WHERE session_id=? ORDER BY date DESC LIMIT ?",(session_id,limit)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM pronunciation_log ORDER BY date DESC LIMIT ?",(limit,)).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try: d["word_scores"]=json.loads(d["word_scores"] or "[]")
        except: d["word_scores"]=[]
        try: d["phoneme_tips"]=json.loads(d["phoneme_tips"] or "[]")
        except: d["phoneme_tips"]=[]
        results.append(d)
    return results

# ═══════════════════════════════════════════════════════════════════════════════
# FLASHCARDS (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/flashcards")
def list_flashcards(due_only:bool=False,category:Optional[str]=None,pos:Optional[str]=None,limit:int=200):
    q="SELECT * FROM flashcards WHERE 1=1"; p=[]
    if due_only: q+=" AND due_date<=date('now')"
    if category: q+=" AND category=?"; p.append(category)
    if pos: q+=" AND part_of_speech=?"; p.append(pos)
    q+=" ORDER BY due_date ASC LIMIT ?"; p.append(limit)
    with get_db() as db: rows=db.execute(q,p).fetchall()
    return [dict(r) for r in rows]

@app.post("/flashcards")
def create_flashcard(card:FlashcardCreate):
    due=today_iso()
    with get_db() as db:
        cur=db.execute("INSERT OR IGNORE INTO flashcards(icelandic,english,notes,category,part_of_speech,due_date,created_at) VALUES(?,?,?,?,?,?,?)",
                       (card.icelandic,card.english,card.notes,card.category,card.part_of_speech,due,now_iso()))
        db.commit()
        row=db.execute("SELECT * FROM flashcards WHERE lower(trim(icelandic))=lower(trim(?))",(card.icelandic,)).fetchone()
    return dict(row)

@app.post("/flashcards/{card_id}/review")
def review_card(card_id:int,review:FlashcardReview):
    with get_db() as db:
        card=db.execute("SELECT * FROM flashcards WHERE id=?",(card_id,)).fetchone()
        if not card: raise HTTPException(404,"Not found")
        card=dict(card)
        new_ease,new_interval=sm2(card["ease_factor"],card["interval_days"],review.correct)
        due=(datetime.now(timezone.utc).date()+timedelta(days=new_interval)).isoformat()
        db.execute("UPDATE flashcards SET ease_factor=?,interval_days=?,due_date=?,times_seen=times_seen+1,times_correct=times_correct+? WHERE id=?",
                   (new_ease,new_interval,due,1 if review.correct else 0,card_id))
        db.commit()
    return {"card_id":card_id,"correct":review.correct,"next_due":due,"interval_days":new_interval}

@app.delete("/flashcards/{card_id}")
def delete_card(card_id:int):
    with get_db() as db:
        db.execute("DELETE FROM flashcards WHERE id=?",(card_id,)); db.commit()
    return {"deleted":card_id}

@app.post("/flashcards/generate")
async def generate_flashcards(req:FlashcardGenReq):
    system=FLASHCARD_GEN_PROMPT.format(count=req.count,level=req.level,topic=req.topic)
    try: raw=await call_llm([{"role":"user","content":"Generate now."}],system,2000)
    except Exception as e: raise HTTPException(502,f"LLM error: {e}")
    clean=raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    try: cards_data=json.loads(clean)
    except: raise HTTPException(502,"Invalid JSON")
    due=today_iso(); created=[]
    with get_db() as db:
        for c in cards_data:
            if c.get("icelandic") and c.get("english"):
                cur=db.execute("INSERT OR IGNORE INTO flashcards(icelandic,english,notes,category,part_of_speech,due_date,created_at) VALUES(?,?,?,?,?,?,?)",
                               (c["icelandic"],c["english"],c.get("notes",""),c.get("category","vocabulary"),c.get("part_of_speech",""),due,now_iso()))
                if cur.lastrowid: created.append(cur.lastrowid)
        db.commit()
    FLASHCARDS_GEN.labels(level=req.level).inc(len(created))
    return {"created":len(created),"ids":created}

# ═══════════════════════════════════════════════════════════════════════════════
# WORD OF THE DAY
# ═══════════════════════════════════════════════════════════════════════════════
WOTD_PROMPT = """You are an Icelandic language expert. Generate a single interesting Icelandic word of the day.
Choose words that are useful, culturally interesting, or have fascinating etymology.
Vary the difficulty and topic — sometimes a common word, sometimes something unique to Iceland.

Return ONLY valid JSON, no markdown:
{
  "word": "the Icelandic word",
  "english": "English translation",
  "part_of_speech": "noun/verb/adjective/adverb/phrase",
  "example_is": "A short example sentence in Icelandic using the word",
  "example_en": "English translation of the example sentence",
  "etymology": "Brief interesting note about the word origin or usage (1 sentence)",
  "difficulty": "beginner|intermediate|advanced"
}"""

@app.get("/word-of-day")
async def get_word_of_day():
    """Get today's word of the day. Generates once per day and caches in DB."""
    today = today_iso()
    # Check cache first
    with get_db() as db:
        row = db.execute("SELECT * FROM word_of_day WHERE date=?", (today,)).fetchone()
        if row:
            return dict(row)
    # Generate new word
    try:
        raw = await call_llm(
            [{"role":"user","content":"Generate today's Icelandic word of the day."}],
            system=WOTD_PROMPT,
            max_tokens=400
        )
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")
    clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    logging.warning("WOTD raw response: %r", raw[:500])
    match = re.search(r'\{.*\}', clean, re.DOTALL)
    if not match:
        raise HTTPException(502, "Invalid JSON from LLM")
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        raise HTTPException(502, "Invalid JSON from LLM")
    # Cache it
    with get_db() as db:
        db.execute("""INSERT OR REPLACE INTO word_of_day
            (date, word, english, part_of_speech, example_is, example_en, etymology, difficulty, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (today, data.get("word",""), data.get("english",""),
             data.get("part_of_speech",""), data.get("example_is",""),
             data.get("example_en",""), data.get("etymology",""),
             data.get("difficulty","beginner"), now_iso()))
        db.commit()
        row = db.execute("SELECT * FROM word_of_day WHERE date=?", (today,)).fetchone()
    return dict(row)

@app.get("/word-of-day/history")
def get_wotd_history(limit: int = 30):
    """Get recent words of the day."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM word_of_day ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

# ═══════════════════════════════════════════════════════════════════════════════
# CEFR ASSESSMENT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

CEFR_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

CEFR_DESCRIPTORS = {
    "A1": "Beginner — basic phrases, introductions, simple questions",
    "A2": "Elementary — simple sentences, routine tasks, familiar topics",
    "B1": "Intermediate — main points of clear input, travel, past/future",
    "B2": "Upper-Intermediate — complex texts, spontaneous interaction, opinions",
    "C1": "Advanced — implicit meaning, fluent expression, complex topics",
    "C2": "Mastery — near-native, nuanced, idiomatic, formal/informal register",
}

CEFR_PASSIVE_PROMPT = """You are an expert Icelandic language assessor familiar with CEFR standards.

Analyze this student's learning data and estimate their current CEFR level for Icelandic.

DATA:
{data}

Assess across four skills, then give an overall CEFR level.

Return ONLY valid JSON:
{{
  "level": "A1|A2|B1|B2|C1|C2",
  "score_overall": 0-100,
  "score_grammar": 0-100,
  "score_vocabulary": 0-100,
  "score_comprehension": 0-100,
  "score_speaking": 0-100,
  "evidence": [
    "specific observation supporting the level assessment",
    "another observation"
  ],
  "recommendations": [
    "specific actionable recommendation to reach next level",
    "another recommendation"
  ],
  "next_level": "A2|B1|B2|C1|C2",
  "next_level_gap": "What specifically needs to improve to reach the next level"
}}

Be calibrated — most learners with <50 sessions are A1-A2. Only assign B1+ if the error data and lesson completions clearly support it."""

CEFR_EXAM_GEN_PROMPT = """You are an expert Icelandic CEFR examiner. Generate a 20-question adaptive exam targeting {level} level.

Include exactly:
- 6 vocabulary questions (multiple choice, 4 options)
- 6 grammar questions (fill-in-the-blank or multiple choice)
- 4 reading comprehension questions (short passage + questions)
- 4 speaking prompts (the student will speak their answer aloud)

Return ONLY valid JSON:
{{
  "target_level": "{level}",
  "sections": [
    {{
      "type": "vocabulary|grammar|reading|speaking",
      "title": "Section title",
      "instructions": "What to do",
      "questions": [
        {{
          "id": "q1",
          "type": "multiple_choice|fill_blank|speaking",
          "question": "The question text",
          "context": "optional passage for reading questions",
          "options": ["a) ...", "b) ...", "c) ...", "d) ..."],
          "correct": "a",
          "explanation": "why this is correct",
          "cefr_skill": "vocabulary|grammar|reading|speaking",
          "points": 5
        }}
      ]
    }}
  ],
  "total_points": 100,
  "time_limit_minutes": 20
}}

Questions must be genuinely challenging for {level} but achievable. Use real Icelandic throughout."""

CEFR_SCORING_PROMPT = """You are an expert Icelandic CEFR examiner. Score this completed exam.

EXAM QUESTIONS:
{questions}

STUDENT ANSWERS:
{answers}

Score each answer. For speaking answers, assess grammar, vocabulary, fluency, and relevance.
Be fair but rigorous — partial credit is allowed for partially correct answers.

Return ONLY valid JSON:
{{
  "question_scores": [
    {{
      "id": "q1",
      "correct": true,
      "points_earned": 5,
      "points_possible": 5,
      "feedback": "Brief feedback on this answer"
    }}
  ],
  "section_scores": {{
    "vocabulary": {{"earned": 0, "possible": 30, "percentage": 0}},
    "grammar": {{"earned": 0, "possible": 30, "percentage": 0}},
    "reading": {{"earned": 0, "possible": 20, "percentage": 0}},
    "speaking": {{"earned": 0, "possible": 20, "percentage": 0}}
  }},
  "total_earned": 0,
  "total_possible": 100,
  "percentage": 0,
  "cefr_level": "A1|A2|B1|B2|C1|C2",
  "level_confidence": "low|medium|high",
  "summary": "2-3 sentence overall assessment of the student's performance",
  "strengths": ["specific strength observed"],
  "weaknesses": ["specific weakness observed"],
  "recommendations": ["specific study recommendation"]
}}"""

# ── Pydantic models ────────────────────────────────────────────────────────────
class ExamAnswer(BaseModel):
    question_id: str
    answer: str
    audio_blob: Optional[str] = None  # base64 for speaking answers

class ExamSubmission(BaseModel):
    exam_id: int
    answers: list[ExamAnswer]


# ── RAG retrieval ─────────────────────────────────────────────────────────────
async def retrieve_context(query: str, top_k: int = 3) -> str:
    """Query the RAG service and return formatted context string for injection."""
    t0 = time.monotonic()
    with tracer.start_as_current_span("rag.retrieve") as span:
        span.set_attribute("rag.query_len", len(query))
        span.set_attribute("rag.top_k", top_k)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{RAG_URL}/query",
                    json={"query": query, "top_k": top_k})
                if not r.ok:
                    return ""
                data = r.json()
                chunks = data.get("chunks", [])
                if not chunks:
                    return ""
                parts = []
                for chunk in chunks:
                    relevance = chunk.get("relevance", 0)
                    RAG_RELEVANCE.observe(relevance)
                    if relevance < 0.3:
                        continue
                    source = chunk.get("source", "book")
                    parts.append(f"[From {source}, relevance {relevance:.2f}]\n{chunk['text']}")
                span.set_attribute("rag.chunks_returned", len(parts))
                return "\n\n---\n".join(parts)
        except Exception as e:
            logger.debug(f"RAG retrieval failed (non-critical): {e}")
            return ""
        finally:
            RAG_DURATION.observe(time.monotonic() - t0)

# ── Passive CEFR estimate ─────────────────────────────────────────────────────
@app.get("/cefr/estimate")
async def get_cefr_estimate(force_refresh: bool = False):
    """
    Passive CEFR estimate based on accumulated learning data.
    Cached for 24 hours unless force_refresh=true.
    """
    today = today_iso()
    # Check for recent cached estimate
    if not force_refresh:
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM cefr_assessments WHERE type='passive' AND date(created_at)=? ORDER BY id DESC LIMIT 1",
                (today,)
            ).fetchone()
            if row:
                d = dict(row)
                try: d["evidence"] = json.loads(d["evidence"] or "[]")
                except: d["evidence"] = []
                try: d["recommendations"] = json.loads(d["recommendations"] or "[]")
                except: d["recommendations"] = []
                return d

    # Gather all evidence
    with get_db() as db:
        error_stats = db.execute("""
            SELECT grammar_category, COUNT(*) as count
            FROM error_log GROUP BY grammar_category ORDER BY count DESC
        """).fetchall()
        lesson_stats = db.execute("""
            SELECT COUNT(DISTINCT lesson_id) as completed,
                   (SELECT COUNT(*) FROM (SELECT DISTINCT id FROM (
                       SELECT 'x' as id FROM lesson_progress
                   ))) as total
            FROM lesson_progress WHERE completed=1
        """).fetchone()
        lessons_done = db.execute("""
            SELECT l.lesson_id, l.completed_at
            FROM lesson_progress l WHERE l.completed=1
            ORDER BY l.completed_at DESC
        """).fetchall()
        total_turns = db.execute("SELECT SUM(turns) as n FROM progress").fetchone()["n"] or 0
        total_errors = db.execute("SELECT COUNT(*) as n FROM error_log").fetchone()["n"] or 0
        vocab_count  = db.execute("SELECT COUNT(*) as n FROM flashcards").fetchone()["n"] or 0
        pron_avg     = db.execute("SELECT AVG(overall_score) as avg FROM pronunciation_log").fetchone()["avg"] or 0
        recent_errors = db.execute("""
            SELECT original, correction, grammar_category, explanation
            FROM error_log ORDER BY date DESC LIMIT 30
        """).fetchall()

    # Build data summary for LLM
    data_summary = {
        "total_conversation_turns": total_turns,
        "total_errors_logged": total_errors,
        "vocabulary_cards": vocab_count,
        "avg_pronunciation_score": round(pron_avg, 1),
        "lessons_completed": [dict(r) for r in lessons_done],
        "error_categories": [dict(r) for r in error_stats],
        "recent_errors_sample": [dict(r) for r in recent_errors],
    }

    if total_turns < 5:
        # Not enough data — return A1 default
        return {
            "type": "passive",
            "level": "A1",
            "score_overall": 10,
            "score_grammar": 10,
            "score_vocabulary": 10,
            "score_comprehension": 10,
            "score_speaking": 10,
            "evidence": ["Not enough data yet — keep practicing!"],
            "recommendations": ["Complete at least 10 conversation turns to get a meaningful estimate."],
            "next_level": "A2",
            "next_level_gap": "More practice needed to assess.",
            "created_at": now_iso(),
        }

    system = CEFR_PASSIVE_PROMPT.format(data=json.dumps(data_summary, ensure_ascii=False))
    try:
        raw = await call_llm([{"role":"user","content":"Assess my CEFR level."}], system, max_tokens=800)
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")

    result = parse_json(raw)
    result["type"] = "passive"

    # Cache it
    with get_db() as db:
        db.execute("""INSERT INTO cefr_assessments
            (type, level, score_overall, score_grammar, score_vocabulary,
             score_comprehension, score_speaking, evidence, recommendations, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("passive", result.get("level","A1"),
             result.get("score_overall",0), result.get("score_grammar",0),
             result.get("score_vocabulary",0), result.get("score_comprehension",0),
             result.get("score_speaking",0),
             json.dumps(result.get("evidence",[])),
             json.dumps(result.get("recommendations",[])),
             now_iso()))
        db.commit()

    return result

@app.get("/cefr/history")
def get_cefr_history():
    """Get CEFR assessment history — both passive and exam-based."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM cefr_assessments ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try: d["evidence"] = json.loads(d["evidence"] or "[]")
        except: d["evidence"] = []
        try: d["recommendations"] = json.loads(d["recommendations"] or "[]")
        except: d["recommendations"] = []
        results.append(d)
    return results

# ── Exam generation ───────────────────────────────────────────────────────────
@app.post("/cefr/exam/start")
async def start_exam(target_level: Optional[str] = None):
    """
    Generate a new CEFR exam. If no target_level, use current passive estimate.
    """
    if not target_level:
        # Use current estimate to pick appropriate level
        with get_db() as db:
            row = db.execute(
                "SELECT level FROM cefr_assessments WHERE type='passive' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        target_level = row["level"] if row else "A2"

    if target_level not in CEFR_LEVELS:
        raise HTTPException(400, f"Invalid level. Must be one of {CEFR_LEVELS}")

    system = CEFR_EXAM_GEN_PROMPT.format(level=target_level)
    try:
        raw = await call_llm(
            [{"role":"user","content":f"Generate a CEFR {target_level} exam for Icelandic."}],
            system, max_tokens=3000
        )
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")

    clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    clean = clean.lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        exam_data = json.loads(clean)
    except:
        raise HTTPException(502, "Invalid exam JSON from LLM")

    with get_db() as db:
        cur = db.execute("""INSERT INTO cefr_exams
            (status, level_target, questions, answers, created_at)
            VALUES (?,?,?,?,?)""",
            ("in_progress", target_level, json.dumps(exam_data), "{}", now_iso()))
        db.commit()
        exam_id = cur.lastrowid

    return {"exam_id": exam_id, "exam": exam_data, "target_level": target_level}

@app.get("/cefr/exam/{exam_id}")
def get_exam(exam_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM cefr_exams WHERE id=?", (exam_id,)).fetchone()
    if not row: raise HTTPException(404, "Exam not found")
    d = dict(row)
    try: d["questions"] = json.loads(d["questions"] or "{}")
    except: pass
    try: d["answers"] = json.loads(d["answers"] or "{}")
    except: pass
    try: d["result"] = json.loads(d["result"] or "null")
    except: pass
    return d

@app.post("/cefr/exam/{exam_id}/submit")
async def submit_exam(exam_id: int, submission: ExamSubmission):
    """Score the completed exam and store results."""
    with get_db() as db:
        row = db.execute("SELECT * FROM cefr_exams WHERE id=?", (exam_id,)).fetchone()
    if not row: raise HTTPException(404, "Exam not found")
    if row["status"] == "completed":
        raise HTTPException(400, "Exam already completed")

    exam_data = json.loads(row["questions"])
    answers_dict = {a.question_id: a.answer for a in submission.answers}

    # Build flat question list for scoring
    all_questions = []
    for section in exam_data.get("sections", []):
        for q in section.get("questions", []):
            q["section_type"] = section["type"]
            all_questions.append(q)

    scoring_system = CEFR_SCORING_PROMPT.format(
        questions=json.dumps(all_questions, ensure_ascii=False),
        answers=json.dumps(answers_dict, ensure_ascii=False)
    )

    try:
        raw = await call_llm(
            [{"role":"user","content":"Score this exam."}],
            scoring_system, max_tokens=2000
        )
    except Exception as e:
        raise HTTPException(502, f"LLM scoring error: {e}")

    clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    clean = clean.lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        result = json.loads(clean)
    except:
        raise HTTPException(502, "Invalid scoring JSON")

    # Store result
    with get_db() as db:
        db.execute("""UPDATE cefr_exams SET
            status='completed', answers=?, result=?, completed_at=?
            WHERE id=?""",
            (json.dumps(answers_dict), json.dumps(result), now_iso(), exam_id))
        # Also store as a formal assessment
        db.execute("""INSERT INTO cefr_assessments
            (type, level, score_overall, score_grammar, score_vocabulary,
             score_comprehension, score_speaking, evidence, recommendations, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("exam", result.get("cefr_level","A1"),
             result.get("percentage",0),
             result.get("section_scores",{}).get("grammar",{}).get("percentage",0),
             result.get("section_scores",{}).get("vocabulary",{}).get("percentage",0),
             result.get("section_scores",{}).get("reading",{}).get("percentage",0),
             result.get("section_scores",{}).get("speaking",{}).get("percentage",0),
             json.dumps(result.get("strengths",[])),
             json.dumps(result.get("recommendations",[])),
             now_iso()))
        db.commit()

    return {"exam_id": exam_id, "result": result}
