"""
Icelandic Tutor — Backend v3
New: lesson curriculum, scenario/topic mode, mistake heatmap,
     pronunciation score proxying, error pattern analysis.
"""
import asyncio, os, json, re, sqlite3, logging, httpx, uuid, time, random
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
LITELLM_URL     = os.getenv("LITELLM_URL",     "http://localhost:4000")
LITELLM_KEY     = os.getenv("LITELLM_API_KEY", "sk-anything")
LITELLM_MODEL   = os.getenv("LITELLM_MODEL",   "ollama/qwen3:32b")
WHISPER_URL     = os.getenv("WHISPER_URL",     "http://whisper:8001")
TTS_URL         = os.getenv("TTS_URL",         "http://tts:8002")
PRONUN_URL      = os.getenv("PRONUN_URL",      "http://whisper:8001")
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
    c.execute("PRAGMA journal_mode=WAL")
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
            session_id TEXT, date TEXT NOT NULL,
            expected_text TEXT, spoken_text TEXT,
            overall_score INTEGER, word_scores TEXT,
            phoneme_tips TEXT
        );
        CREATE TABLE IF NOT EXISTS reading_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            page_num INTEGER NOT NULL,
            completed_at TEXT NOT NULL,
            UNIQUE(filename, page_num)
        );
        CREATE TABLE IF NOT EXISTS grammar_drill_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            question TEXT NOT NULL,
            expected TEXT NOT NULL,
            given TEXT NOT NULL,
            correct INTEGER NOT NULL,
            explanation TEXT
        );
        """)
        # Migrations
        try:
            c.execute("ALTER TABLE flashcards ADD COLUMN part_of_speech TEXT DEFAULT ''")
        except Exception:
            pass
        # Make pronunciation_log.session_id nullable (recreate if NOT NULL constraint present)
        try:
            col_info = c.execute("PRAGMA table_info(pronunciation_log)").fetchall()
            session_col = next((r for r in col_info if r["name"] == "session_id"), None)
            if session_col and session_col["notnull"]:
                c.execute("ALTER TABLE pronunciation_log RENAME TO pronunciation_log_old")
                c.execute("""CREATE TABLE pronunciation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT, date TEXT NOT NULL,
                    expected_text TEXT, spoken_text TEXT,
                    overall_score INTEGER, word_scores TEXT,
                    phoneme_tips TEXT
                )""")
                c.execute("INSERT INTO pronunciation_log SELECT * FROM pronunciation_log_old")
                c.execute("DROP TABLE pronunciation_log_old")
        except Exception:
            pass
        # Deduplicate: keep the oldest card per icelandic word, then enforce uniqueness
        c.execute("""
            DELETE FROM flashcards WHERE id NOT IN (
                SELECT MIN(id) FROM flashcards GROUP BY lower(trim(icelandic))
            )
        """)
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_flashcards_icelandic ON flashcards(lower(trim(icelandic)))")
        c.executescript("""
            CREATE INDEX IF NOT EXISTS idx_error_log_date_cat ON error_log(date, grammar_category);
            CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at);
            CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_progress_date ON progress(date);
        """)
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
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting each greeting phrase explicitly with its English meaning: Halló (hello), Góðan daginn (good day), Gaman að hitta þig (nice to meet you), Ég heiti [name] (I am called...), Hvað heitir þú? (What are you called?), Ég er frá [place] (I am from...). Then model a complete introduction exchange yourself as an example. Only after presenting all phrases, invite the student to try introducing themselves. Correct gently and encourage."},
    {"id":"L02","track":"beginner","order":2,"title":"Numbers & Counting",
     "description":"Count to 100, tell your age, discuss quantities.",
     "grammar_focus":"Cardinal numbers, the verb 'að vera' with age",
     "vocabulary":["einn","tveir","þrír","fjórir","fimm","tíu","tuttugu","hundrað"],
     "goal":"Count objects and tell Sigríður your age and phone number.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting numbers 1–10 explicitly, giving each number in Icelandic with its English meaning and asking the student to repeat each one. Then teach 11–20, then the multiples of 10 up to 100, practicing each group before moving on. Important: note that 1–4 are gender-inflected (einn/ein/eitt, tveir/tvær/tvö, þrír/þrjár/þrjú, fjórir/fjórar/fjögur) — explain this with examples. Only after numbers are solid, move to practical exercises: counting objects, telling ages, reading phone numbers."},
    {"id":"L03","track":"beginner","order":3,"title":"Colors & Descriptions",
     "description":"Describe objects using colors and basic adjectives.",
     "grammar_focus":"Adjective agreement with noun gender",
     "vocabulary":["rauður","blár","grænn","gulur","svartur","hvítur","stór","lítill"],
     "goal":"Describe 5 objects in the room using colors and adjectives.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting each color in Icelandic with its English meaning. Then explain adjective gender agreement as a clear rule: masculine nouns take -ur (rauður), feminine nouns take no ending (rauð), neuter nouns take -tt (rautt). Give 3 example sentences showing each gender form with a real noun. Only after the student has seen the full pattern and examples, ask them to describe objects around them using colors and adjectives."},
    {"id":"L04","track":"beginner","order":4,"title":"Family Members",
     "description":"Talk about your family — parents, siblings, children.",
     "grammar_focus":"Possessive pronouns, noun plurals",
     "vocabulary":["móðir","faðir","systir","bróðir","barn","afi","amma","eiginmaður","eiginkona"],
     "goal":"Describe your family to Sigríður.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting all family vocabulary with English meanings: móðir/mamma (mother), faðir/pabbi (father), systir (sister), bróðir (brother), barn (child), afi (grandfather), amma (grandmother), eiginmaður (husband), eiginkona (wife). Then teach possessive forms: mamma mín (my mom), pabbi minn (my dad), systir mín (my sister), bróðir minn (my brother). Give 3 model sentences using these possessives. Only then ask the student to describe their own family."},
    {"id":"L05","track":"beginner","order":5,"title":"Food & Drink",
     "description":"Order food, express preferences, talk about meals.",
     "grammar_focus":"The accusative case with 'að vilja' (to want)",
     "vocabulary":["matur","drykkur","brauð","mjólk","vatn","kaffi","fiskur","kjöt","grænmeti"],
     "goal":"Order a meal and describe what you like to eat.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching the key ordering phrases: Ég vil fá... (I would like...), Má ég fá...? (May I have...?), Hvað mælirðu með? (What do you recommend?). Explain that 'fá' takes accusative case — the food item changes form slightly. Then present core food vocabulary with English meanings. Give 2 complete model ordering sentences. Only after phrases and vocabulary are covered, begin the café or restaurant role-play."},
    {"id":"L06","track":"beginner","order":6,"title":"Days, Months & Time",
     "description":"Tell the time, say what day it is, discuss your schedule.",
     "grammar_focus":"Dative case with time expressions",
     "vocabulary":["mánudagur","þriðjudagur","miðvikudagur","janúar","febrúar","klukkan","í dag","á morgun"],
     "goal":"Describe your weekly schedule to Sigríður.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching all 7 days of the week in order with English meanings, asking the student to repeat them. Then teach the 12 months the same way. Then teach the clock formula: Klukkan er... (It is... o'clock), half hours (hálf...), and key time words: í dag (today), á morgun (tomorrow), í gær (yesterday), í vikunni (during the week). Practice each group before moving to the next. Only after all three systems are covered, move to schedule conversation."},
    {"id":"L07","track":"beginner","order":7,"title":"The Weather",
     "description":"Talk about Icelandic weather — sun, rain, wind, snow, and temperature.",
     "grammar_focus":"Impersonal weather expressions, adjective predicates",
     "vocabulary":["veður","rigning","snjór","vindur","kalt","hlýtt","sól","þoka","frost","stormur"],
     "goal":"Describe today's weather and ask about tomorrow's forecast.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by explaining the impersonal weather construction: Icelandic weather uses 'Það' (it/there) as a dummy subject — Það er kalt (it is cold), Það rignir (it is raining), Það snjóar (it is snowing). Present all weather vocabulary with English meanings. Then teach the predicative adjectives: kalt, hlýtt, vindasamt, þokið, sólríkt. Give 4 model weather sentences. Only then ask the student to describe today's weather and the forecast."},
    {"id":"L08","track":"beginner","order":8,"title":"Getting Around",
     "description":"Use public transport, ask for directions, navigate a city.",
     "grammar_focus":"Imperative mood for directions, accusative with motion verbs",
     "vocabulary":["strætó","stoppistöð","leiga","gangstétt","beyga","beint áfram","til vinstri","til hægri","nálægt","langt"],
     "goal":"Ask Sigríður for directions to three Reykjavík landmarks and understand the answers.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting transport vocabulary with English meanings. Then teach the imperative forms used for directions: Farðu (go), Beygðu (turn), Haltu (stop/wait), and directional phrases: beint áfram (straight ahead), til vinstri (to the left), til hægri (to the right), hjá (near/by), framhjá (past). Give a model direction sequence from one Reykjavík landmark to another. Only after vocabulary and imperatives are practiced, begin the directions role-play."},
    {"id":"L09","track":"beginner","order":9,"title":"At the Hotel",
     "description":"Check in and out, request amenities, handle common hotel situations.",
     "grammar_focus":"Polite requests with 'mætti ég', modal verbs",
     "vocabulary":["herbergi","lykill","morgunmatur","brottför","koma","bókun","baðherbergi","þjónusta"],
     "goal":"Check in to a hotel, ask for a wake-up call, and request extra towels.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching the two key polite request frames with 3 filled-in examples each: Mætti ég fá...? (May I have...? — e.g. Mætti ég fá lykil? / Mætti ég fá morgunmat?) and Gæti þú...? (Could you...? — e.g. Gæti þú vakið mig? / Gæti þú sent handklæði?). Present hotel vocabulary with English meanings. Then walk through the standard check-in sequence in Icelandic. Only after phrases and vocabulary are clear, begin the hotel role-play."},
    {"id":"L10","track":"beginner","order":10,"title":"Feelings & Health",
     "description":"Express how you feel physically and emotionally, describe symptoms.",
     "grammar_focus":"Dative with feeling expressions ('mér líður vel'), body vocabulary",
     "vocabulary":["veikur","þreyttur","sárt","höfuðverkur","kuldafloginn","gleður","reiður","hræddur","líða","heilsa"],
     "goal":"Tell Sigríður how you feel today and describe a recent illness.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by explaining the dative experiencer construction: feelings in Icelandic 'happen to' the person, so the experiencer takes dative — Mér líður vel (I feel well, literally: to-me it-goes well), Mér líður illa (I feel unwell). Contrast with simple predicate: Ég er veikur (I am sick). Then present feeling and health vocabulary with English meanings, and teach body parts. Give 4 model sentences covering different constructions. Only after these patterns are explained, ask how the student feels."},
    {"id":"L11","track":"beginner","order":11,"title":"Shopping & Money",
     "description":"Buy things, ask about prices, handle money and payments.",
     "grammar_focus":"Accusative for quantities and prices, question words",
     "vocabulary":["verð","króna","dýrt","ódýrt","kaupa","selja","greiða","afsláttur","kvittun","búð"],
     "goal":"Buy three items, negotiate a price, and ask for a receipt.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by briefly reviewing numbers in a price context (since shopping requires them). Then present key shopping phrases: Hvað kostar þetta? (How much does this cost?), Það er of dýrt (that is too expensive), Má ég fá afslát? (May I have a discount?), Má ég fá kvittun? (May I have a receipt?). Explain that prices and quantities use accusative case. Give 3 model transaction exchanges. Only after phrases and vocabulary are solid, begin the shopkeeper role-play."},
    {"id":"L12","track":"beginner","order":12,"title":"Telling Stories — Simple Past",
     "description":"Recount events in the past, describe what happened.",
     "grammar_focus":"Simple past tense introduction, time adverbs",
     "vocabulary":["í gær","í síðustu viku","áður","síðan","fyrst","þá","loks"],
     "goal":"Tell Sigríður about what you did yesterday using at least 5 past tense verbs.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching how to form the simple past tense. For weak verbs: add -ði or -ti (tala → talaði / talked, keyra → keyrði / drove). For common strong verbs: the stem vowel changes (ablaut) — present these explicitly: fara → fór (go/went), koma → kom (come/came), gera → gerði (do/did), sjá → sá (see/saw), segja → sagði (say/said). Give 3 model past-tense sentences with time adverbs (í gær, í síðustu viku). Only after these forms are presented, ask the student to narrate their own past events."},
    # ── Intermediate track ────────────────────────────────────────────────────
    {"id":"L13","track":"intermediate","order":1,"title":"The Four Cases — Nominative & Accusative",
     "description":"Understand when to use nominative vs accusative case.",
     "grammar_focus":"Nominative (subject) vs Accusative (direct object)",
     "vocabulary":["hús","hundur","köttur","stóll","borð","bók","penni"],
     "goal":"Correctly use nouns in both nominative and accusative in 10 sentences.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn with a clear conceptual explanation: nominative is used for the subject (the one doing the action); accusative is used for the direct object (the one receiving the action). Show the ending change for masculine nouns: hundur (nom) → hund (acc), with the definite article: hundurinn (nom) → hundinn (acc). Give 3 worked sentence pairs showing both cases in context. Only after the rule and examples are clear, move to fill-in-the-blank drills and then free production. Correct all case errors explicitly."},
    {"id":"L14","track":"intermediate","order":2,"title":"The Dative Case",
     "description":"Master the dative case — used with many prepositions and indirect objects.",
     "grammar_focus":"Dative case endings, prepositions that take dative",
     "vocabulary":["með","á","í","frá","til","hjá","eftir"],
     "goal":"Use dative correctly with 5 prepositions.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting the dative case endings as a clear table: masculine -i (hundur → hundi), feminine -u (kona → konu), neuter -i (barn → barni), with plural -um for all genders. Decline 2 example nouns fully in dative. Then go through each dative preposition one at a time (í, á, með, frá, hjá) with an example sentence for each. Only after the endings and prepositions are presented, move to drills. Correct dative errors specifically throughout."},
    {"id":"L15","track":"intermediate","order":3,"title":"The Genitive Case",
     "description":"Express possession and relationships using genitive.",
     "grammar_focus":"Genitive case endings, possessive constructions",
     "vocabulary":["eigandi","hluti","nafn","heimilisfang","kennitala"],
     "goal":"Describe ownership of 8 things using genitive.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by explaining the genitive case: it marks possession, like English 's. Show the endings by noun class: masculine -s (hundur → hunds), feminine -ar (kona → konár), neuter -s (barn → barns). Give 3 possession examples with literal translations (Bók Sigríðar = book of-Sigríður = Sigríður's book). Then teach the genitive of names. Only after the pattern is shown clearly, have the student describe ownership of things."},
    {"id":"L16","track":"intermediate","order":4,"title":"Verb Conjugation — Present Tense",
     "description":"Conjugate strong and weak verbs across all persons.",
     "grammar_focus":"Present tense conjugation patterns, strong vs weak verbs",
     "vocabulary":["að fara","að koma","að gera","að segja","að sjá","að vita","að vilja"],
     "goal":"Conjugate 7 common verbs correctly in all persons.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by writing out the complete conjugation of 'að fara' as a fully worked example: ég fer, þú ferð, hann/hún/það fer, við förum, þið farið, þeir/þær/þau fara. Identify the stem and the person endings. Then work through 'að koma' and 'að gera' the same way as further worked examples. Only after 3 full paradigms have been shown, have the student conjugate new verbs. Correct all conjugation errors."},
    {"id":"L17","track":"intermediate","order":5,"title":"Past Tense",
     "description":"Talk about what happened in the past.",
     "grammar_focus":"Past tense (þátíð) of strong and weak verbs",
     "vocabulary":["fór","kom","gerði","sagði","sá","vissi","vildi"],
     "goal":"Tell Sigríður about what you did yesterday, entirely in past tense.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting the main past tense patterns systematically. Weak verbs: add -ði or -ti to the stem (tala → talaði, keyra → keyrði). Strong verbs change their stem vowel (ablaut) — give a table of 6 common strong verbs: fara→fór, koma→kom, sjá→sá, gefa→gaf, bíða→beið, ríða→reið. Explain that weak verbs are predictable; strong verbs must be memorized. Give 3 model past-tense sentences. Then have the student convert present-tense sentences to past before narrating freely."},
    {"id":"L18","track":"intermediate","order":6,"title":"Subjunctive & Conditionals",
     "description":"Express wishes, hypotheticals, and polite requests.",
     "grammar_focus":"Subjunctive mood (viðtengingarhátt), conditional sentences",
     "vocabulary":["myndi","væri","hefði","mætti","skyldi"],
     "goal":"Form 5 conditional sentences and 3 polite requests.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting the two main conditional patterns as templates: (1) myndi + infinitive for present/future hypotheticals: Ég myndi fara ef... (I would go if...); (2) hefði + past participle for perfect/past hypotheticals: Ég hefði farið ef... (I would have gone if...). Give 3 complete conditional sentences as models. Then present polite request forms: Mætti ég fá...? (May I...?), Gæti þú hjálpað mér? (Could you help me?). Only after these patterns are shown, have the student form their own conditionals and requests."},
    {"id":"L19","track":"intermediate","order":7,"title":"Comparatives & Superlatives",
     "description":"Compare things — bigger, better, more expensive, the best.",
     "grammar_focus":"Comparative and superlative adjective forms, 'en' (than)",
     "vocabulary":["stærri","stærstur","betri","bestur","dýrari","dýrastur","fleiri","flestir","meira","mest"],
     "goal":"Make 8 comparison sentences describing people, places, and things.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching the regular comparative and superlative pattern: add -ari for comparative, -astur for superlative (stór → stærri → stærstur). Then explicitly list all irregular forms that must be memorized: góður/betri/bestur (good), lítill/minni/minstur (small), margur/fleiri/flestir (many), mikill/meira/mestur (much). Give 4 model comparison sentences using 'en' (than): þetta er stærra en... Only after the regular pattern and all irregulars are presented, have the student make comparisons."},
    {"id":"L20","track":"intermediate","order":8,"title":"Reflexive Verbs & Pronouns",
     "description":"Master the reflexive pronoun 'sig' and reflexive verb constructions.",
     "grammar_focus":"Reflexive pronoun sig/sér/sín, reflexive verbs",
     "vocabulary":["sig","sér","sín","klæða sig","setja sig","líða","finna fyrir sér","hreyfa sig","þvo sér"],
     "goal":"Use sig/sér/sín correctly in 6 sentences and conjugate 3 reflexive verbs.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting a clear decision table: sig (accusative — direct object: Hann klæður sig / He dresses himself), sér (dative — indirect object/location: Hún kaupir sér mat / She buys herself food), sín (genitive — possession: Hann tekur bókina sína / He takes his own book). Contrast explicitly with English 'himself/herself/his own'. Give one anchor sentence for each case. Then present reflexive verbs: klæða sig (to dress), þvo sér (to wash), hreyfa sig (to exercise). Drill each case before free production."},
    {"id":"L21","track":"intermediate","order":9,"title":"Passive Voice",
     "description":"Express actions without naming the subject — 'it was done', 'it is said'.",
     "grammar_focus":"Passive construction with 'vera' + past participle, impersonal passive",
     "vocabulary":["gert","sagt","talið","séð","heyrt","fundið","búið til","opnað","lokað"],
     "goal":"Produce 5 passive sentences describing events or states.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by showing 3 active/passive sentence pairs side by side: Jón opnaði dyrnar → Dyrnar voru opnaðar (Jón opened the door → The door was opened). Explain the pattern: vera (to be) + past participle, with the participle agreeing in gender with the subject. Then teach the impersonal -st passive: Það er sagt að... (It is said that...), Það er talið að... (It is believed that...). Have the student transform active sentences to passive before producing new passive sentences freely."},
    {"id":"L22","track":"intermediate","order":10,"title":"Modal Verbs in Depth",
     "description":"Master must, can, need, may — and the cases they govern.",
     "grammar_focus":"Modal verbs: mega, verða, þurfa, geta, skylda + correct case",
     "vocabulary":["mega","verða","þurfa","geta","skylda","má","verð","þarf","get","á að"],
     "goal":"Use all 5 modal verbs correctly in conversation with proper case agreement.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching one modal at a time, presenting each with its specific construction and a model sentence before moving to the next: (1) geta + infinitive: Ég get gert þetta (I can do this); (2) verða að + infinitive: Ég verð að fara (I must go); (3) þurfa að + infinitive: Ég þarf að sofa (I need to sleep); (4) mega + infinitive: Ég má fara (I may go); (5) eiga að + infinitive: Ég ætti að hjálpa (I should help). Confirm understanding of each modal before introducing the next. Only after all five are presented, practice combining them in context."},
    {"id":"L23","track":"intermediate","order":11,"title":"Prepositions & Cases Deep Dive",
     "description":"Master which prepositions take which cases — and why.",
     "grammar_focus":"Case government of prepositions, motion vs location distinction",
     "vocabulary":["í","á","við","fyrir","eftir","um","til","frá","með","án","gegnum","meðfram"],
     "goal":"Use 8 prepositions correctly with the right case in context.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by stating the core rule as a memorable principle: motion toward a place takes accusative; location/rest takes dative. Give the pivot pair: Ég fer í skólann (acc — going to school) vs Ég er í skólanum (dat — at school). Then present a table of the most common dual-case prepositions (í, á, við, fyrir) showing both forms and meanings. Give 6 worked sentence pairs covering motion vs. location. Only after the rule and examples are clear, move to sentence-completion drills. Correct case errors immediately."},
    {"id":"L24","track":"intermediate","order":12,"title":"Talking About the Future",
     "description":"Express plans, predictions, and intentions.",
     "grammar_focus":"Future with 'ætla að', 'mun', 'verður að', present for near future",
     "vocabulary":["ætla","mun","verður","vonast","búast við","líklega","kannski","ef til vill","á morgun","í næstu viku"],
     "goal":"Describe your plans for next week using at least 3 different future constructions.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching each future construction with its specific meaning and 2 model sentences: (1) ætla að = personal intention/plan: Ég ætla að fara í bíó (I'm going to go to the cinema); (2) mun = prediction or certainty: Það mun rigna á morgun (It will rain tomorrow); (3) verður að = obligation or inevitability: Hún verður að vinna (She will have to work). Explain when to use each. Only after all three are presented with examples, ask the student to express their own plans using all three constructions."},
    # ── Advanced track ────────────────────────────────────────────────────────
    {"id":"L25","track":"advanced","order":1,"title":"Noun Declension Mastery",
     "description":"Full command of all noun declension classes.",
     "grammar_focus":"All four declension classes, irregular nouns",
     "vocabulary":["maður","kona","barn","hestur","skip","borg"],
     "goal":"Decline 6 nouns correctly in all four cases, singular and plural.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by walking through the complete declension of 'hestur' (horse) as a fully worked example — all four cases (nom/acc/dat/gen), singular and plural: hestur, hest, hesti, hests / hestar, hesta, hestum, hesta. Identify the pattern. Then work through 'kona' (fem) and 'barn' (neut) the same way. Only after these three paradigms are shown and understood, give the student blank tables to fill in. Challenge with irregular nouns (maður, skip) after the regular classes are solid."},
    {"id":"L26","track":"advanced","order":2,"title":"Complex Sentences & Relative Clauses",
     "description":"Build sophisticated sentences with embedded clauses.",
     "grammar_focus":"Relative pronouns, subordinate clauses, word order",
     "vocabulary":["sem","þar sem","þegar","þótt","þar til","svo að"],
     "goal":"Produce 5 complex sentences with relative clauses.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by stating the subordinate clause word order rule explicitly: in Icelandic main clauses the verb comes second (V2); in subordinate clauses the verb follows the subject normally without inversion. Show the contrast: Hann fer heim (main: he goes home) vs ...þegar hann fer heim (subclause: ...when he goes home). Then teach relative clauses with 'sem': Konan sem ég sá (the woman that I saw). Give 3 complete example sentences for each type. Only after word order and relative clauses are demonstrated, have the student combine simple sentences."},
    {"id":"L27","track":"advanced","order":3,"title":"Idiomatic Expressions",
     "description":"Sound natural with common Icelandic idioms and expressions.",
     "grammar_focus":"Idiomatic usage, fixed phrases",
     "vocabulary":["Mér líður vel","Þetta reddast","Hvernig gengur?","Gangi þér vel","Vertu sæll"],
     "goal":"Use 8 idiomatic expressions naturally in conversation.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting each idiom with: (1) its Icelandic form, (2) a word-by-word literal translation, (3) its actual meaning, and (4) a brief context showing when you'd use it. Cover at minimum: Þetta reddast (this-will-sort-itself — 'it'll work out', said to reassure), Mér líður vel (to-me it-goes well — 'I'm fine'), Hvernig gengur? (how goes? — 'how are you?'), Gangi þér vel (may-it-go-you well — 'good luck'), Vertu sæl/sæll (be-you happy — 'goodbye/take care'). After presenting each idiom, have the student use it in a new sentence before moving on."},
    {"id":"L28","track":"advanced","order":4,"title":"The Middle Voice (-st verbs)",
     "description":"Master the unique Icelandic middle voice — verbs ending in -st.",
     "grammar_focus":"Middle voice formation, reciprocal and reflexive -st verbs",
     "vocabulary":["kallast","finnast","líðast","hittast","kynnast","skiljanst","gleðjast","kvíðast","minnast","þykjast"],
     "goal":"Use 6 middle voice verbs correctly and explain the difference from active forms.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching the three uses of -st as separate mini-lessons, one at a time. First: reflexive -st (the subject does the action to itself) — klæðast = to dress oneself, þvást = to wash oneself; give 3 active/middle pairs. Confirm understanding before continuing. Second: reciprocal -st (mutual action between subjects) — hittast = to meet each other, kynnast = to get acquainted; give 3 pairs. Third: impersonal/medio-passive — finnst mér = it seems to me, líðst vel = things go well; give 3 examples. Only after all three are taught, combine them in free practice."},
    {"id":"L29","track":"advanced","order":5,"title":"Noun Declension of Proper Names",
     "description":"Decline Icelandic personal names correctly across all four cases.",
     "grammar_focus":"Name declension patterns, -ar vs -s genitive, declined first names",
     "vocabulary":["Jón","Jóns","Jóni","Björk","Björku","Sigríður","Sigríðar","Gunnar","Gunnars"],
     "goal":"Correctly decline 5 Icelandic names in all four cases.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting a full 4-case declension table for a masculine name: Jón (nom), Jón (acc), Jóni (dat), Jóns (gen). Then a feminine name: Sigríður (nom), Sigríði (acc), Sigríði (dat), Sigríðar (gen). Explain the patterns and note where they differ from common nouns. Practice with 3 more names before introducing the patronymic system. Teach patronymics as a separate step: father's name → genitive form + son/dóttir suffix (Jón → Jóns → Jónsson/Jónsdóttir)."},
    {"id":"L30","track":"advanced","order":6,"title":"Formal vs Informal Register",
     "description":"Navigate between casual speech and formal written Icelandic.",
     "grammar_focus":"Register differences, formal vocabulary, written vs spoken forms",
     "vocabulary":["kæri","virðulegur","þér","yður","hér með","meðfylgjandi","gjörðu svo vel","með vinsemd"],
     "goal":"Write a formal email and contrast it with how you'd say the same thing casually.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting 8 formal/informal vocabulary pairs side by side: Kæri/Hæ (dear/hey), Með vinsemd og virðingu/Kveðja (sincerely/best), Gjörðu svo vel/Endilega (please/go ahead), þér/þú (formal/informal 'you'). Then show a complete model formal email with each formal feature labeled and explained. Only after the pairs and the model are both presented, have the student draft their own formal email, then rewrite it in casual register."},
    {"id":"L31","track":"advanced","order":7,"title":"Icelandic Phonology Deep Dive",
     "description":"Master the sounds that make Icelandic distinctive — pre-aspiration, lateral fricative, vowel shifts.",
     "grammar_focus":"Pre-aspiration, lateral fricative ll, rl cluster, vowel quantity",
     "vocabulary":["köttur","vatn","epli","þorskur","fjall","völlur","herbergi","allt","fellt"],
     "goal":"Correctly pronounce 10 words featuring challenging Icelandic phonemes.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by explaining one phonological feature at a time, using TTS to model example words before asking the student to attempt anything. First: pre-aspiration — double consonants like 'tt' in köttur (cat) have a breath before them; give 3 TTS example words. Second: lateral fricative 'll' — like an English 'tl' with a hiss (fjall, völlur); give 3 TTS examples. Third: 'rl' cluster — like 'rdl' (Karl, ferli); give 3 TTS examples. Fourth: vowel length distinctions (e.g. 'i' vs 'í'). For each feature, describe what the student should listen for, then have them attempt the words."},
    {"id":"L32","track":"advanced","order":8,"title":"Reading Old Norse Cognates",
     "description":"Connect modern Icelandic to its Old Norse roots through cognates with English.",
     "grammar_focus":"Etymology, sound correspondences, Norse loan words in English",
     "vocabulary":["skip","gleyma","vindur","drykkur","systir","faðir","egg","hnífur","gluggi","húsbóndi"],
     "goal":"Identify 10 Old Norse cognates in English and explain their sound shifts.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching the main sound-shift correspondences as learnable rules, giving 3 examples per rule: Norse 'sk' → English 'sh' (skip → ship, skinn → skin, skógur → ?); Norse initial 'v' → English 'w' (vindur → wind, vatn → water); Norse 'k' before front vowels → English 'ch' (kirkja → church, kinn → chin). After teaching the rules, turn the lesson into a guessing game: give the student an Icelandic word and ask them to predict the English cognate before revealing it. Praise correct reasoning."},
    {"id":"L33","track":"advanced","order":9,"title":"Newspaper & Media Language",
     "description":"Read Icelandic news headlines, understand formal broadcast language.",
     "grammar_focus":"Headline grammar (omitted verbs), nominalization, formal connectives",
     "vocabulary":["þingmaður","ríkisstjórn","hagvöxtur","verðlag","hlutfall","rannsókn","tilkynning","skýrsla","viðtal","greinargerð"],
     "goal":"Read and explain 3 Icelandic news headlines and summarize a short news item.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting 2 model Icelandic news headlines with full annotation: identify what verb was omitted, what nominalization was used, and what formal connective appeared. Then explicitly teach the 5 key formal connectives with English meanings: þar sem (since/given that), þrátt fyrir (despite), vegna þess að (because of), samkvæmt (according to), að því er kemur fram (according to what appears). Give the student the political/economic vocabulary with translations. Only after models and vocabulary are covered, give 3 new headlines to analyze and ask for a short news summary."},
    # ── Cultural track ────────────────────────────────────────────────────────
    {"id":"C01","track":"cultural","order":1,"title":"Icelandic Names & Patronymics",
     "description":"Understand how Icelandic names work — patronymics, matronymics, and address customs.",
     "grammar_focus":"Patronymic formation, name declension in practice",
     "vocabulary":["Jónsson","Jónsdóttir","fornafn","eftirnafn","kenninafn","faðir","móðir","-son","-dóttir"],
     "goal":"Explain the Icelandic naming system and correctly form 4 patronymics.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by explaining the Icelandic patronymic system step by step. First teach the genitive formation step — the father's name must go into genitive form before adding son/dóttir: Sigurður → Sigurðar, Jón → Jóns, Gunnar → Gunnars. Show 3 complete worked examples: Jón's son Pétur = Pétur Jónsson; Jón's daughter Anna = Anna Jónsdóttir. Also explain: people are listed by first name in the phone book; always address Icelanders by first name. Only after examples are shown, give the student 4 father's names to form patronymics from."},
    {"id":"C02","track":"cultural","order":2,"title":"The Sagas — Key Passages",
     "description":"Read simplified passages from the Icelandic Sagas with vocabulary support.",
     "grammar_focus":"Old/formal narrative style, saga vocabulary, past tense narrative",
     "vocabulary":["víkingur","goði","þing","útlægur","blót","frændi","hefnd","drengskapur","skáld","Ísland"],
     "goal":"Read and discuss a simplified saga passage, identifying key narrative elements.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn with a short cultural briefing in Icelandic (with English translation): what sagas are, when they were written (1100–1400 AD), and why they matter as world literature. Then pre-teach 5 key saga vocabulary words with meanings and example sentences: goði (chieftain), þing (assembly), útlægur (outlaw), hefnd (revenge), drengskapur (honorable conduct). Then present the simplified saga passage in chunks of 2–3 sentences, pausing to discuss the meaning, vocabulary, and narrative style after each chunk before moving on."},
    {"id":"C03","track":"cultural","order":3,"title":"Icelandic Holidays & Traditions",
     "description":"Learn about þorrablót, Jónsmessa, Verslunarmannahelgi, and other Icelandic celebrations.",
     "grammar_focus":"Describing customs and traditions, temporal expressions",
     "vocabulary":["þorrablót","Jónsmessa","hákarl","svið","brennivín","jólasveinar","Verslunarmannahelgi","páskar","sumardagurinn fyrsti"],
     "goal":"Describe three Icelandic holidays and explain what happens at þorrablót.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting each holiday as a structured mini-briefing: name, when it occurs, 2–3 key cultural facts, and key vocabulary. Cover: Þorrablót (midwinter feast, January — hákarl/fermented shark, svið/sheep head, brennivín/schnapps), Jónsmessa (midsummer, June 24th), sumardagurinn fyrsti (first day of summer, April), and the 13 Jólasveinar (instead of Santa Claus). For each holiday, present the information first, then ask the student to describe it back to you using the vocabulary you taught, before opening to free discussion."},
    {"id":"C04","track":"cultural","order":4,"title":"Music & Pop Culture",
     "description":"Discuss Icelandic music, art, and contemporary culture.",
     "grammar_focus":"Discussing preferences and opinions, comparative language",
     "vocabulary":["tónlist","lag","hljómsveit","söngvari","kvikmynd","listir","bók","höfundur","íslenska","menning"],
     "goal":"Discuss a favourite type of music and ask about Icelandic cultural life.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by teaching the opinion expressions the student will need throughout this lesson: Mér finnst... (I find.../I think...), Að mínu mati... (in my opinion...), Ég held að... (I believe that...), Mér líkar... (I like...). Give 3 model sentences for each. Then introduce 3–4 key Icelandic cultural facts: Björk, Sigur Rós, Iceland Airwaves festival, the fact that Iceland publishes more books per capita than any country on Earth. Only after these building blocks are in place, ask the student for their opinions on music and culture."},
    {"id":"C05","track":"cultural","order":5,"title":"Vikings & The Settlement of Iceland",
     "description":"Discuss the Viking Age, the settlement of Iceland (874 AD), and early Icelandic history.",
     "grammar_focus":"Historical past tense, formal narrative vocabulary",
     "vocabulary":["landnám","Ingólfur Arnarson","Alþingi","goðorð","þræll","búnaður","landnámsmaður","víkingaöld","Norðmenn","Garðarshólmur"],
     "goal":"Recount the story of Iceland's settlement and explain what the Alþingi was.",
     "system_addon":"TEACH FIRST, THEN PRACTICE. Open your very first turn by presenting the settlement story as a short structured Icelandic narrative (4–5 sentences) with key words glossed in English as you go: Ingólfur Arnarson kom til Íslands árið 874 (came to Iceland in the year 874). Pre-teach key vocabulary before the narrative: landnám (settlement), þræll (slave), goðorð (chieftaincy), Alþingi (parliament, founded 930 AD — world's oldest still in operation). After the student has heard and understood the story, ask them to retell it in their own Icelandic words using the past tense vocabulary from the narrative."},
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

LESSON_TEACHING_RULES = """
LESSON TEACHING RULES — obey every turn without exception:

1. TEACH BEFORE TESTING — Never ask the student to produce a word, phrase, or grammatical construction you have not explicitly shown them WITH an English meaning in this conversation. If you want them to say it, you must model it first.

2. WORKED EXAMPLE BEFORE EVERY PRODUCTION TASK — Before asking the student to produce anything, give a complete filled-in example sentence: e.g. "For instance: 'Ég er þrjátíu ára gamall' means 'I am thirty years old'. Now you try with your actual age."

3. ONE CONCEPT AT A TIME — Introduce a single item or pattern per beat. Wait for the student to demonstrate they have it before moving to the next. Do not front-load multiple new things in one turn during practice.

4. NARRATE EVERY TRANSITION — When the student masters a micro-goal, explicitly name the achievement and announce what comes next: "You've got 1–5 — nicely done. Now let's do 6–10. Here they are: ..." Never raise the bar silently.

5. RETEACH ON REPEATED ERRORS — If the student makes the same error twice in a row, stop, rephrase the underlying rule, and give a fresh worked example before asking them to try again. Do not simply correct and continue.

6. VOCABULARY BOUNDARY — Only ask the student to use vocabulary from this lesson's declared pool or universally safe basics (Halló, Ég, þú, vera, takk). Do not introduce vocabulary in passing without explicitly teaching it first.

7. NEVER STRAND THE STUDENT — If a task requires knowledge the student does not yet have, provide that knowledge in the same turn before asking. A student who cannot answer is not failing — the lesson scaffolding has failed them.
"""

FLASHCARD_GEN_PROMPT = """Icelandic language expert. Generate {count} flashcards for a {level} learner on: {topic}
Return ONLY a JSON array, no markdown:
[{{"icelandic":"...","english":"...","notes":"...","category":"vocabulary|grammar|phrase","part_of_speech":"noun|verb|adjective|adverb|preposition|conjunction|pronoun|phrase|other"}}]
"""

SENTENCE_GEN_PROMPT = """You are an Icelandic language expert. Generate exactly {count} common Icelandic sentence flashcards for a {level} learner on the situation: {topic}.

Return ONLY a valid JSON array, no markdown:
[{{"icelandic":"Full Icelandic sentence","english":"Natural English equivalent","notes":"One-sentence grammar or usage note, or empty string","category":"sentence"}}]

Guidelines:
- Use natural, conversational Icelandic (not textbook-formal)
- Sentence length: 4–15 words
- beginner: present tense, simple common structures, high-frequency vocabulary
- intermediate: various tenses, modal verbs, common idioms and prepositions
- advanced: complex clauses, subjunctive mood, nuanced register and vocabulary
- Mix questions, statements, and requests in roughly equal proportion
- notes: highlight one interesting grammar point (e.g. "Uses dative experiencer construction" or "Subjunctive after 'ef'"); leave empty string if nothing notable
- Never duplicate icelandic content within the batch"""

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

def extract_json(raw: str) -> str:
    """Strip think blocks and code fences, then return the first JSON object/array found."""
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Try to find a JSON object or array anywhere in the response
    m = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text
def today_iso(): return datetime.now(timezone.utc).date().isoformat()

def sm2(ease, interval, correct):
    if correct: return min(2.5, max(1.3, ease+0.1)), max(1, round(interval*ease))
    return max(1.3, ease-0.2), 1

async def call_ollama(messages, system, max_tokens=1500):
    payload = {"model":OLLAMA_MODEL,
               "messages":[{"role":"system","content":system}]+messages,
               "stream":False,
               "options":{"num_predict": max_tokens}}
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

async def call_litellm(messages, system, max_tokens=1500):
    payload = {"model": LITELLM_MODEL,
               "messages": [{"role":"system","content":system}]+messages,
               "max_tokens": max_tokens}
    headers = {"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=270) as c:
        r = await c.post(f"{LITELLM_URL}/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

async def stream_litellm(messages, system):
    payload = {"model": LITELLM_MODEL,
               "messages": [{"role":"system","content":system}]+messages,
               "stream": True}
    headers = {"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=270) as c:
        async with c.stream("POST", f"{LITELLM_URL}/chat/completions",
                            json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    token = json.loads(line[6:])["choices"][0]["delta"].get("content","")
                    if token:
                        yield token
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

async def call_llm(messages, system, max_tokens=1500):
    if LLM_PROVIDER=="ollama":    return await call_ollama(messages,system,max_tokens)
    if LLM_PROVIDER=="litellm":   return await call_litellm(messages,system,max_tokens)
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
    elif LLM_PROVIDER == "litellm":
        async for chunk in stream_litellm(messages, system):
            yield chunk
    else:
        async for chunk in stream_anthropic(messages, system):
            yield chunk

def parse_json(raw):
    try: return json.loads(extract_json(raw))
    except: return {"icelandic":raw,"english_correction":{"errors":[],"positive":"","tip":""},
                    "difficulty_assessment":"beginner","new_vocabulary":[],"lesson_progress":{}}

def _lesson_phase(user_turn_count: int) -> tuple[str, str]:
    """Return (phase_name, phase_instruction) based on how many user turns have elapsed."""
    if user_turn_count <= 1:
        return (
            "INTRODUCTION",
            "You are in the INTRODUCTION phase. Your job this turn is to TEACH, not to test. "
            "Present every vocabulary item and grammar pattern listed below with its English meaning. "
            "Model at least two complete example sentences. "
            "End your turn with a warm invitation to try ONE simple thing — but only after all material has been presented."
        )
    elif user_turn_count <= 4:
        return (
            "GUIDED PRACTICE",
            "You are in the GUIDED PRACTICE phase. Vocabulary has been introduced. "
            "Practice each item in isolation: model the target form first, then ask the student to reproduce it "
            "or use it in a simple slot-fill. One concept per beat. "
            "Always give a complete worked example immediately before each production task."
        )
    else:
        return (
            "FREE PRACTICE",
            "You are in the FREE PRACTICE phase. The student has seen all lesson material. "
            "Engage in natural conversational practice using the lesson vocabulary and grammar in context. "
            "When the student struggles, explicitly reference what was taught "
            "('Remember: Ég er X ára — now try with your own age'). "
            "Work toward completing the lesson goal."
        )


def build_system_prompt(mode, scenario_id, lesson_id, level, user_turn_count: int = 0):
    system = BASE_SYSTEM
    if mode=="scenario" and scenario_id:
        sc = next((s for s in SCENARIOS if s["id"]==scenario_id), None)
        if sc:
            system += f"\n\nSCENARIO MODE — {sc['title']}\n{sc['system_addon']}\n"
            system += f"\nVocabulary to introduce: {', '.join(sc['vocabulary'])}"
    elif mode=="lesson" and lesson_id:
        ls = next((l for l in LESSONS if l["id"]==lesson_id), None)
        if ls:
            phase_name, phase_instruction = _lesson_phase(user_turn_count)
            system += f"\n\nLESSON MODE — {ls['title']}\n"
            system += f"Grammar focus: {ls['grammar_focus']}\n"
            system += f"Lesson goal: {ls['goal']}\n"
            system += f"Lesson-specific guidance: {ls['system_addon']}\n"
            system += f"\nVOCABULARY POOL (the only new words you may use or test this lesson):\n{', '.join(ls['vocabulary'])}\n"
            system += f"\nCURRENT PHASE ({user_turn_count} user turn(s) completed): {phase_name}\n{phase_instruction}\n"
            system += "\nTrack goal_percent (0-100) and set goal_met=true when the student has achieved the lesson goal."
            system += LESSON_TEACHING_RULES
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
    type: str = "vocabulary"  # vocabulary | sentence

class LessonProgressUpdate(BaseModel):
    lesson_id: str; completed: bool; score: int = 0; session_id: Optional[str]=None

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health(): return {"status":"ok","llm":LLM_PROVIDER}

@app.get("/dashboard")
def get_dashboard():
    today = today_iso()
    week_start = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
    with get_db() as db:
        # ── Streak ───────────────────────────────────────────────────────────
        all_dates = [r["date"] for r in db.execute(
            "SELECT DISTINCT date FROM progress ORDER BY date DESC LIMIT 365"
        ).fetchall()]
        streak = 0
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        if all_dates and all_dates[0] in (today, yesterday):
            expected = all_dates[0]
            for d in all_dates:
                if d == expected:
                    streak += 1
                    expected = (datetime.fromisoformat(expected).date() - timedelta(days=1)).isoformat()
                else:
                    break
        active_dates = [r["date"] for r in db.execute(
            "SELECT DISTINCT date FROM progress WHERE date >= ?", (week_start,)
        ).fetchall()]
        # ── Cards ─────────────────────────────────────────────────────────────
        vocab_due = db.execute(
            "SELECT COUNT(*) as n FROM flashcards WHERE due_date <= date('now') AND category NOT IN ('phrase','sentence')"
        ).fetchone()["n"]
        sent_due = db.execute(
            "SELECT COUNT(*) as n FROM flashcards WHERE due_date <= date('now') AND category IN ('phrase','sentence')"
        ).fetchone()["n"]
        # ── Lessons ───────────────────────────────────────────────────────────
        completed_ids = {r["lesson_id"] for r in db.execute(
            "SELECT DISTINCT lesson_id FROM lesson_progress WHERE completed=1"
        ).fetchall()}
        # ── CEFR ──────────────────────────────────────────────────────────────
        cefr_row = db.execute(
            "SELECT level FROM cefr_assessments ORDER BY id DESC LIMIT 1"
        ).fetchone()
        # ── Weakest category (chat errors, last 30 days) ──────────────────────
        weak_row = db.execute(
            "SELECT grammar_category, COUNT(*) as count FROM error_log "
            "WHERE date >= date('now','-30 days') AND error_type != 'drill' "
            "GROUP BY grammar_category ORDER BY count DESC LIMIT 1"
        ).fetchone()
        # ── Word of the Day ───────────────────────────────────────────────────
        wotd = db.execute("SELECT * FROM word_of_day WHERE date=?", (today,)).fetchone()

    next_lesson = next((l for l in LESSONS if l["id"] not in completed_ids), None)

    return {
        "streak": streak,
        "active_dates": active_dates,
        "vocab_due": vocab_due,
        "sentences_due": sent_due,
        "lessons_completed": len(completed_ids),
        "lessons_total": len(LESSONS),
        "next_lesson": {"id": next_lesson["id"], "title": next_lesson["title"], "track": next_lesson["track"]} if next_lesson else None,
        "cefr_level": cefr_row["level"] if cefr_row else None,
        "weak_category": weak_row["grammar_category"] if weak_row else None,
        "word_of_day": dict(wotd) if wotd else None,
    }

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

    user_turn_count = sum(1 for m in req.messages if m.role == "user")
    system = build_system_prompt(req.mode, req.scenario_id, req.lesson_id, req.level, user_turn_count)
    window = 12 if req.mode == "lesson" else 6
    msgs = [{"role":m.role,"content":m.content} for m in req.messages[-window:]]

    # Collect RAG result — has been running concurrently during the sync work above.
    rag_sources = []
    if rag_task:
        rag_context, rag_sources = await rag_task
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
            GRAMMAR_ERRORS.labels(category=gc).inc()
        # Save vocabulary (INSERT OR IGNORE deduplicates on icelandic text)
        due = today
        for v in new_vocab:
            if v.get("icelandic") and v.get("english"):
                inserted = db.execute("INSERT OR IGNORE INTO flashcards(icelandic,english,notes,category,part_of_speech,due_date,created_at,source_session) VALUES(?,?,?,?,?,?,?,?)",
                           (v["icelandic"],v["english"],v.get("notes",""),v.get("category","vocabulary"),v.get("part_of_speech",""),due,now_iso(),sid))
                if inserted.rowcount:
                    FLASHCARDS_GEN.labels(level=req.level).inc()
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
            "mode":req.mode,"rag_sources":rag_sources}

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

    user_turn_count = sum(1 for m in req.messages if m.role == "user")
    system = build_system_prompt(req.mode, req.scenario_id, req.lesson_id, req.level, user_turn_count)
    window = 12 if req.mode == "lesson" else 6
    msgs = [{"role":m.role,"content":m.content} for m in req.messages[-window:]]

    rag_sources = []
    if rag_task:
        rag_context, rag_sources = await rag_task
        if rag_context:
            system += f"""

REFERENCE MATERIAL from student's Icelandic grammar books (use when relevant to correct or explain):
{rag_context}

When this material is relevant, naturally reference it in your tip or correction (e.g. "As your grammar book explains..."). Do not force it into every response."""

    async def generate():
        full_buffer      = ""
        icelandic_buffer = ""   # raw JSON string fragments for the icelandic field
        scan_from        = 0
        in_is            = False   # inside the "icelandic" value
        is_done          = False   # finished extracting icelandic value
        tts_sent         = False   # tts_ready event already yielded
        MARKER           = '"icelandic": "'

        model_name = OLLAMA_MODEL if LLM_PROVIDER == "ollama" else LITELLM_MODEL if LLM_PROVIDER == "litellm" else ANTHROPIC_MODEL
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
                        icelandic_buffer += to_emit
                        yield f'data: {json.dumps({"t":"tok","v":to_emit})}\n\n'

                    if is_done and not tts_sent:
                        tts_sent = True
                        try:
                            icelandic_text = json.loads(f'"{icelandic_buffer}"')
                        except Exception:
                            icelandic_text = icelandic_buffer
                        yield f'data: {json.dumps({"t":"tts_ready","icelandic":icelandic_text})}\n\n'

            except Exception as e:
                logger.error(f"stream_llm error: {e}")
                llm_span.record_exception(e)
                yield f'data: {json.dumps({"t":"error","msg":"LLM connection failed"})}\n\n'
                return
            finally:
                LLM_DURATION.labels(provider=LLM_PROVIDER, model=model_name).observe(
                    time.monotonic() - t_llm)

        # ── post-stream: parse, persist, send done event ──────────────────────
        logger.info("post-stream: reached")
        t_post = time.monotonic()
        try:
            with tracer.start_as_current_span("chat.parse_json") as parse_span:
                data       = parse_json(full_buffer)
                correction = data.get("english_correction",{})
                new_vocab  = data.get("new_vocabulary",[])
                lp         = data.get("lesson_progress",{})
                parse_span.set_attribute("buffer_len", len(full_buffer))
            logger.info(f"post-stream: parse took {(time.monotonic()-t_post)*1000:.0f}ms")

            t_db = time.monotonic()
            with tracer.start_as_current_span("chat.db_write") as db_span:
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
                            inserted = db.execute("INSERT OR IGNORE INTO flashcards(icelandic,english,notes,category,part_of_speech,due_date,created_at,source_session) VALUES(?,?,?,?,?,?,?,?)",
                                       (v["icelandic"],v["english"],v.get("notes",""),v.get("category","vocabulary"),v.get("part_of_speech",""),today,now_iso(),sid))
                            if inserted.rowcount:
                                FLASHCARDS_GEN.labels(level=req.level).inc()
                    lesson_just_completed = False
                    if req.mode=="lesson" and req.lesson_id and lp.get("goal_met"):
                        already = db.execute("SELECT id FROM lesson_progress WHERE lesson_id=? AND completed=1",(req.lesson_id,)).fetchone()
                        if not already:
                            db.execute("INSERT INTO lesson_progress(lesson_id,completed,score,completed_at,session_id) VALUES(?,1,100,?,?)",
                                       (req.lesson_id,now_iso(),sid))
                            lesson_just_completed = True
                    db.commit()
                db_span.set_attribute("errors_logged", errors_n)
                db_span.set_attribute("vocab_inserted", len(new_vocab))
            logger.info(f"post-stream: db took {(time.monotonic()-t_db)*1000:.0f}ms  total={( time.monotonic()-t_post)*1000:.0f}ms")
        except Exception as exc:
            logger.error(f"post-stream error: {exc}", exc_info=True)
            lesson_just_completed = False

        yield f'data: {json.dumps({"t":"done","session_id":sid,"icelandic":data.get("icelandic",""),"english_translation":data.get("english_translation",""),"english_correction":correction,"new_vocabulary":new_vocab,"lesson_progress":lp,"lesson_just_completed":lesson_just_completed,"mode":req.mode,"rag_sources":rag_sources})}\n\n'

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
        rows = db.execute("""
            SELECT s.*,
                   (SELECT icelandic FROM messages WHERE session_id=s.id AND role='assistant'
                    ORDER BY created_at DESC LIMIT 1) as last_icelandic
            FROM sessions s ORDER BY s.updated_at DESC LIMIT ?
        """, (limit,)).fetchall()
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

class SessionPatch(BaseModel):
    title: str

@app.patch("/sessions/{sid}")
def patch_session(sid: str, patch: SessionPatch):
    with get_db() as db:
        if not db.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Not found")
        db.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                   (patch.title.strip(), now_iso(), sid))
        db.commit()
    return {"id": sid, "title": patch.title.strip()}

SESSION_TITLE_PROMPT = """Generate a short English title (3-6 words, no quotes, no trailing punctuation) for this Icelandic tutoring conversation.
Focus on the main topic or activity discussed.
Examples: "Ordering food at a café", "Past tense verb practice", "Weather vocabulary", "Getting directions in Reykjavík"

User said: {user_msg}
Tutor responded about: {assistant_msg}

Return only the title, nothing else."""

@app.post("/sessions/{sid}/generate-title")
async def generate_session_title(sid: str):
    with get_db() as db:
        if not db.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Not found")
        msgs = db.execute(
            "SELECT role, content, icelandic FROM messages WHERE session_id=? ORDER BY created_at LIMIT 2",
            (sid,)
        ).fetchall()
    if not msgs:
        raise HTTPException(400, "No messages yet")
    user_msg = next((m["content"] for m in msgs if m["role"] == "user"), "")
    asst_msg = next((m["icelandic"] for m in msgs if m["role"] == "assistant"), "")
    prompt = SESSION_TITLE_PROMPT.format(user_msg=user_msg[:200], assistant_msg=asst_msg[:200])
    try:
        raw = await call_llm([{"role": "user", "content": "Generate the title."}], prompt, max_tokens=30)
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")
    title = re.sub(r'^["\'\`]|["\'\`]$', '', raw.strip())[:80]
    with get_db() as db:
        db.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))
        db.commit()
    return {"id": sid, "title": title}

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
            FROM progress WHERE date>=date('now',?)""",
            (f"-{days} days",)).fetchone()
        cards_total = db.execute("SELECT COUNT(*) as n FROM flashcards").fetchone()["n"]
        cards_due   = db.execute("SELECT COUNT(*) as n FROM flashcards WHERE due_date<=date('now')").fetchone()["n"]
        lessons_done = db.execute("SELECT COUNT(DISTINCT lesson_id) as n FROM lesson_progress WHERE completed=1").fetchone()["n"]
        completed_lessons = [r["lesson_id"] for r in db.execute(
            "SELECT DISTINCT lesson_id FROM lesson_progress WHERE completed=1").fetchall()]
        # Streak: computed from all-time data, independent of the period filter
        all_dates = {r["date"] for r in db.execute(
            "SELECT DISTINCT date FROM progress").fetchall()}
    streak = 0
    d = datetime.now(timezone.utc).date()
    if d.isoformat() not in all_dates:
        d -= timedelta(days=1)
    while d.isoformat() in all_dates:
        streak += 1
        d -= timedelta(days=1)
    return {"daily":[dict(r) for r in daily],"totals":dict(totals),
            "cards_total":cards_total,"cards_due":cards_due,
            "lessons_completed":lessons_done,"completed_lessons":completed_lessons,
            "streak":streak}

# ═══════════════════════════════════════════════════════════════════════════════
# HEATMAP / ERROR ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/heatmap")
def get_heatmap(days:int=90):
    window = f"-{days} days"
    with get_db() as db:
        cat_rows = db.execute("""
            SELECT grammar_category, COUNT(*) as count
            FROM error_log WHERE date>=date('now',?)
            GROUP BY grammar_category ORDER BY count DESC
        """,(window,)).fetchall()
        total_errors = db.execute(
            "SELECT COUNT(*) as n FROM error_log WHERE date>=date('now',?)",(window,)).fetchone()["n"]
        daily_errors = db.execute("""
            SELECT date, grammar_category, COUNT(*) as count
            FROM error_log WHERE date>=date('now',?)
            GROUP BY date, grammar_category ORDER BY date ASC
        """,(window,)).fetchall()
        # error_map: group by (original, correction) pair for the grid
        pair_rows = db.execute("""
            SELECT original, correction, grammar_category, COUNT(*) as count,
                   MAX(explanation) as explanation
            FROM error_log WHERE date>=date('now',?)
              AND original IS NOT NULL AND original != ''
            GROUP BY lower(trim(original)), lower(trim(correction))
            ORDER BY count DESC LIMIT 60
        """,(window,)).fetchall()
        # top_errors: same data, top 10 with explanation
        top_rows = db.execute("""
            SELECT original, correction, grammar_category, COUNT(*) as count,
                   MAX(explanation) as explanation
            FROM error_log WHERE date>=date('now',?)
              AND original IS NOT NULL AND original != ''
            GROUP BY lower(trim(original)), lower(trim(correction))
            ORDER BY count DESC LIMIT 10
        """,(window,)).fetchall()

    # by_category: plain dict expected by frontend bar chart
    by_category = {r["grammar_category"]: r["count"] for r in cat_rows}

    # error_map: dict keyed by "original|||correction" for the grid
    error_map = {}
    for r in pair_rows:
        key = f"{r['original']}|||{r['correction']}"
        error_map[key] = {"original": r["original"], "correction": r["correction"],
                          "category": r["grammar_category"], "count": r["count"]}

    top_errors = [{"original":r["original"],"correction":r["correction"],
                   "category":r["grammar_category"],"count":r["count"],
                   "explanation":r["explanation"] or ""} for r in top_rows]

    return {"by_category":by_category,"error_map":error_map,"top_errors":top_errors,
            "total_errors":total_errors,"daily":[dict(r) for r in daily_errors]}

@app.get("/heatmap/strengths")
def get_heatmap_strengths(days:int=90):
    window = f"-{days} days"
    all_cats = ["case_nominative","case_accusative","case_dative","case_genitive",
                "verb_conjugation","verb_tense","noun_gender","adjective_agreement",
                "word_order","pronunciation","vocabulary","spelling","other"]
    with get_db() as db:
        # Error counts per category
        cat_counts = {r["grammar_category"]: r["count"] for r in db.execute(
            "SELECT grammar_category, COUNT(*) as count FROM error_log WHERE date>=date('now',?) GROUP BY grammar_category",
            (window,)).fetchall()}
        total_errors = sum(cat_counts.values())
        # Accuracy trend: error rate per day
        trend = db.execute("""
            SELECT p.date, SUM(p.turns) as turns, SUM(p.errors_made) as errors
            FROM progress p WHERE p.date>=date('now',?)
            GROUP BY p.date ORDER BY p.date ASC
        """,(window,)).fetchall()
        # Positive notes from messages
        pos_rows = db.execute("""
            SELECT json_extract(correction,'$.positive') as positive, created_at
            FROM messages
            WHERE correction IS NOT NULL
              AND json_extract(correction,'$.positive') IS NOT NULL
              AND json_extract(correction,'$.positive') != ''
            ORDER BY created_at DESC LIMIT 12
        """).fetchall()
        # Flashcard mastery
        mastered = db.execute("""
            SELECT icelandic, english, times_seen, times_correct
            FROM flashcards WHERE times_seen >= 3
            ORDER BY CAST(times_correct AS REAL)/times_seen DESC LIMIT 10
        """).fetchall()

    strong   = [c for c in all_cats if cat_counts.get(c,0) == 0]
    low      = [{"category":c,"count":cat_counts[c]} for c in all_cats if 0 < cat_counts.get(c,0) <= 3]
    weak     = [{"category":c,"count":cat_counts[c]} for c in all_cats if cat_counts.get(c,0) > 3]

    accuracy_trend = [{"date":r["date"],
                        "error_rate": round(r["errors"]/max(r["turns"],1)*100),
                        "turns": r["turns"], "errors": r["errors"]}
                      for r in trend]

    praise = [{"text": r["positive"], "date": r["created_at"][:10]} for r in pos_rows]

    mastered_cards = [{"icelandic":r["icelandic"],"english":r["english"],
                       "seen":r["times_seen"],"correct":r["times_correct"],
                       "pct":round(r["times_correct"]/r["times_seen"]*100)}
                      for r in mastered]

    return {"strong_categories": strong, "low_error_categories": low,
            "weak_categories": weak, "total_errors": total_errors,
            "accuracy_trend": accuracy_trend, "praise": praise,
            "mastered_cards": mastered_cards}

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

@app.get("/heatmap/full")
async def get_heatmap_full(days:int=90):
    """Combined endpoint: returns heatmap + strengths + analysis in one round-trip."""
    heatmap   = get_heatmap(days)
    strengths = get_heatmap_strengths(days)
    analysis  = await get_heatmap_analysis(days)
    return {"heatmap": heatmap, "strengths": strengths, "analysis": analysis}

# ═══════════════════════════════════════════════════════════════════════════════
# PRONUNCIATION — proxy to pronunciation service
# ═══════════════════════════════════════════════════════════════════════════════
@app.post("/pronunciation/score")
async def score_pronunciation(
    audio: UploadFile = File(...),
    expected_text: str = Form(""),
    session_id: str = Form(""),
    translate: str = Form(""),
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

    spoken = result.get("spoken_text", "")

    # Optionally translate what was heard back into English
    if translate and spoken:
        try:
            raw = await call_llm(
                [{"role": "user", "content": spoken}],
                "Translate the following Icelandic text to English. "
                "Return only the English translation, no explanation.",
                max_tokens=120,
            )
            result["spoken_english"] = raw.strip().strip('"')
        except Exception:
            pass

    # Log result — session_id is optional metadata, always log
    with get_db() as db:
        db.execute("""INSERT INTO pronunciation_log
            (session_id,date,expected_text,spoken_text,overall_score,word_scores,phoneme_tips)
            VALUES(?,?,?,?,?,?,?)""",
            (session_id or None, today_iso(), expected_text,
             spoken, result.get("overall_score",0),
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
def list_flashcards(due_only:bool=False,category:Optional[str]=None,pos:Optional[str]=None,limit:int=2000):
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

@app.get("/flashcards/quiz")
def get_vocab_quiz(count: int = 10):
    with get_db() as db:
        all_cards = [dict(r) for r in db.execute(
            "SELECT id, icelandic, english, notes FROM flashcards ORDER BY RANDOM()"
        ).fetchall()]
    if len(all_cards) < 4:
        raise HTTPException(400, "Need at least 4 flashcards to generate a quiz.")
    count = min(count, len(all_cards))
    question_cards = all_cards[:count]
    questions = []
    for i, card in enumerate(question_cards):
        others = [c for c in all_cards if c["id"] != card["id"]]
        distractors = random.sample(others, min(3, len(others)))
        if i % 2 == 0:
            q_text = f"How do you say \"{card['english']}\" in Icelandic?"
            correct = card["icelandic"]
            wrongs = [d["icelandic"] for d in distractors]
        else:
            q_text = f"What does \"{card['icelandic']}\" mean in English?"
            correct = card["english"]
            wrongs = [d["english"] for d in distractors]
        options = [correct] + wrongs[:3]
        random.shuffle(options)
        questions.append({
            "id": i, "card_id": card["id"],
            "question": q_text, "options": options,
            "correct": options.index(correct),
            "direction": "en_to_is" if i % 2 == 0 else "is_to_en",
            "icelandic": card["icelandic"], "english": card["english"],
            "notes": card.get("notes") or "",
        })
    return {"questions": questions, "total": len(questions)}

class QuizAnswer(BaseModel):
    card_id: int
    correct: bool

class QuizResultsReq(BaseModel):
    answers: list[QuizAnswer]

@app.post("/flashcards/quiz/results")
def submit_quiz_results(req: QuizResultsReq):
    with get_db() as db:
        for a in req.answers:
            card = db.execute("SELECT * FROM flashcards WHERE id=?", (a.card_id,)).fetchone()
            if not card:
                continue
            card = dict(card)
            new_ease, new_interval = sm2(card["ease_factor"], card["interval_days"], a.correct)
            due = (datetime.now(timezone.utc).date() + timedelta(days=new_interval)).isoformat()
            db.execute(
                "UPDATE flashcards SET ease_factor=?,interval_days=?,due_date=?,times_seen=times_seen+1,times_correct=times_correct+? WHERE id=?",
                (new_ease, new_interval, due, 1 if a.correct else 0, a.card_id)
            )
        db.commit()
    return {"updated": len(req.answers)}

@app.post("/flashcards/generate")
async def generate_flashcards(req:FlashcardGenReq):
    if req.type == "sentence":
        system = SENTENCE_GEN_PROMPT.format(count=req.count, level=req.level, topic=req.topic)
    else:
        system=FLASHCARD_GEN_PROMPT.format(count=req.count,level=req.level,topic=req.topic)
    try: raw=await call_llm([{"role":"user","content":"Generate now."}],system,2000)
    except Exception as e: raise HTTPException(502,f"LLM error: {e}")
    try: cards_data=json.loads(extract_json(raw))
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
async def retrieve_context(query: str, top_k: int = 3) -> tuple[str, list[dict]]:
    """Query the RAG service; return (context_string_for_llm, sources_for_frontend)."""
    t0 = time.monotonic()
    with tracer.start_as_current_span("rag.retrieve") as span:
        span.set_attribute("rag.query_len", len(query))
        span.set_attribute("rag.top_k", top_k)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{RAG_URL}/query",
                    json={"query": query, "top_k": top_k})
                if not r.is_success:
                    return "", []
                data = r.json()
                chunks = data.get("chunks", [])
                if not chunks:
                    return "", []
                parts, sources = [], []
                for chunk in chunks:
                    relevance = chunk.get("relevance", 0)
                    RAG_RELEVANCE.observe(relevance)
                    if relevance < 0.84:
                        continue
                    source = chunk.get("source", "book")
                    page   = chunk.get("page_number")
                    parts.append(f"[From {source}, relevance {relevance:.2f}]\n{chunk['text']}")
                    sources.append({"source": source, "page_number": page, "relevance": relevance})
                span.set_attribute("rag.chunks_returned", len(parts))
                return "\n\n---\n".join(parts), sources
        except Exception as e:
            logger.debug(f"RAG retrieval failed (non-critical): {e}")
            return "", []
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

    try:
        exam_data = json.loads(extract_json(raw))
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

    try:
        result = json.loads(extract_json(raw))
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

# ═══════════════════════════════════════════════════════════════════════════════
# LIBRARY / READING PROGRESS
# ═══════════════════════════════════════════════════════════════════════════════

class ReadingProgressReq(BaseModel):
    filename: str
    page_num: int
    completed: bool

@app.post("/library/progress")
def set_reading_progress(req: ReadingProgressReq):
    with get_db() as db:
        if req.completed:
            db.execute(
                "INSERT OR IGNORE INTO reading_progress(filename, page_num, completed_at) VALUES(?,?,?)",
                (req.filename, req.page_num, now_iso())
            )
        else:
            db.execute(
                "DELETE FROM reading_progress WHERE filename=? AND page_num=?",
                (req.filename, req.page_num)
            )
        db.commit()
    return {"filename": req.filename, "page_num": req.page_num, "completed": req.completed}

@app.get("/library/progress")
def get_all_reading_progress():
    """Return completed page count per filename — used by the library landing grid."""
    with get_db() as db:
        rows = db.execute(
            "SELECT filename, COUNT(*) as completed FROM reading_progress GROUP BY filename"
        ).fetchall()
    return {r["filename"]: r["completed"] for r in rows}

@app.get("/library/progress/{filename}")
def get_reading_progress(filename: str):
    with get_db() as db:
        rows = db.execute(
            "SELECT page_num FROM reading_progress WHERE filename=? ORDER BY page_num",
            (filename,)
        ).fetchall()
    return {"filename": filename, "completed_pages": [r["page_num"] for r in rows]}

# ═══════════════════════════════════════════════════════════════════════════════
# GRAMMAR DRILL
# ═══════════════════════════════════════════════════════════════════════════════

DRILL_CATEGORIES = [
    "case_nominative","case_accusative","case_dative","case_genitive",
    "verb_conjugation","verb_tense","noun_gender","adjective_agreement",
]

_drill_cache: dict = {}  # cache_key -> (timestamp, questions)
_DRILL_CACHE_TTL = 3600.0

DRILL_GEN_PROMPT = """You are an Icelandic grammar drill generator. Generate exactly {count} drill questions for category '{category}' at difficulty '{level}'.

Return ONLY a valid JSON array, no markdown, no explanation. Each object:
{{
  "question": "Clear English prompt describing what form to produce",
  "base_form": "the dictionary/infinitive/nominative form",
  "expected": "exact correct Icelandic answer (lowercase)",
  "answer_variants": ["Capitalized variant", "alternative spelling if any"],
  "explanation": "One sentence explaining the rule applied",
  "category": "{category}"
}}

Category guidance:
- case_nominative: Ask for the nominative definite or indefinite form of a noun
- case_accusative: Ask for the accusative form of a noun or pronoun
- case_dative: Ask for the dative form, ideally with a preposition context (í, á, með, frá, hjá)
- case_genitive: Ask for the genitive (possessive) form of a noun
- verb_conjugation: Give an infinitive + subject pronoun, ask for the present tense form
- verb_tense: Give a present tense verb form, ask for the simple past (þátíð)
- noun_gender: Give a noun in nominative, ask whether it is masculine, feminine, or neuter
- adjective_agreement: Give an adjective, target noun (with gender), case, and definiteness — ask for the correct adjective form

Difficulty:
- beginner: common nouns/verbs, regular patterns only (hestur, kona, barn, tala, vera, fara)
- intermediate: strong verbs, all four cases with irregular nouns, common adjectives (stór, gamall)
- advanced: uncommon strong verbs, archaic forms, complex declensions, unusual patterns

Rules:
- Never duplicate base_form within the batch
- expected must be lowercase; answer_variants may include a capitalized form
- For noun_gender questions, expected is one of: masculine, feminine, neuter
- Make questions self-contained — include gender and noun class where relevant"""


def _levenshtein(a: str, b: str) -> int:
    if a == b: return 0
    if not a: return len(b)
    if not b: return len(a)
    prev = list(range(len(b) + 1))
    for ca in a:
        curr = [prev[0] + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(ca != cb)))
        prev = curr
    return prev[-1]


def _norm(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", s.strip().lower())


class DrillAnswerReq(BaseModel):
    category: str
    difficulty: str
    question: str
    expected: str
    answer_variants: list = []
    given: str
    explanation: str = ""


@app.get("/drill/questions")
async def get_drill_questions(category: str = "case_accusative", level: str = "beginner", count: int = 10):
    if category not in DRILL_CATEGORIES:
        raise HTTPException(400, f"Unknown category. Valid: {DRILL_CATEGORIES}")
    if level not in ["beginner","intermediate","advanced"]:
        raise HTTPException(400, "level must be beginner/intermediate/advanced")
    count = max(1, min(count, 20))
    cache_key = f"{category}:{level}"
    now = time.time()
    if cache_key in _drill_cache:
        ts, questions = _drill_cache[cache_key]
        if now - ts < _DRILL_CACHE_TTL:
            return {"questions": questions, "category": category, "level": level, "cached": True}
    # Evict all expired entries before inserting a new one
    expired = [k for k, (ts, _) in _drill_cache.items() if now - ts >= _DRILL_CACHE_TTL]
    for k in expired:
        del _drill_cache[k]
    prompt = DRILL_GEN_PROMPT.format(count=count, category=category, level=level)
    try:
        raw = await call_llm([{"role":"user","content":"Generate the drill questions now."}], prompt, max_tokens=2000)
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")
    try:
        questions = json.loads(extract_json(raw))
        if not isinstance(questions, list):
            raise ValueError("not a list")
    except Exception:
        raise HTTPException(502, "LLM returned invalid JSON for drill questions")
    _drill_cache[cache_key] = (now, questions)
    return {"questions": questions, "category": category, "level": level, "cached": False}


@app.post("/drill/answer")
def submit_drill_answer(req: DrillAnswerReq):
    given_norm = _norm(req.given)
    all_correct = [_norm(req.expected)] + [_norm(v) for v in req.answer_variants]
    correct = given_norm in all_correct
    near_miss = False
    if not correct:
        near_miss = any(_levenshtein(given_norm, c) <= 1 for c in all_correct)
        if near_miss:
            correct = True
    with get_db() as db:
        db.execute(
            "INSERT INTO grammar_drill_log(date,category,difficulty,question,expected,given,correct,explanation) VALUES(?,?,?,?,?,?,?,?)",
            (today_iso(), req.category, req.difficulty, req.question, req.expected, req.given, int(correct), req.explanation)
        )
        if not correct:
            db.execute(
                "INSERT INTO error_log(session_id,date,error_type,original,correction,explanation,grammar_category) VALUES(?,?,?,?,?,?,?)",
                ("drill", today_iso(), "drill", req.given, req.expected, req.explanation, req.category)
            )
        db.commit()
    return {"correct": correct, "near_miss": near_miss, "expected": req.expected, "explanation": req.explanation}


@app.get("/drill/stats")
def get_drill_stats():
    with get_db() as db:
        rows = db.execute("""
            SELECT category, COUNT(*) as attempts, SUM(correct) as correct_count
            FROM grammar_drill_log GROUP BY category
        """).fetchall()
        recent = db.execute("""
            SELECT date, category, correct, question, expected, given
            FROM grammar_drill_log ORDER BY id DESC LIMIT 20
        """).fetchall()
    by_category = {}
    for r in rows:
        a, c = r["attempts"], r["correct_count"] or 0
        by_category[r["category"]] = {
            "attempts": a, "correct": c,
            "accuracy": round(c / a * 100) if a else 0,
        }
    return {"by_category": by_category, "recent": [dict(r) for r in recent]}
