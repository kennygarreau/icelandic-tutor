"""
Icelandic Tutor — RAG Service
Embeds PDF books into ChromaDB and serves semantic search queries.
Uses multilingual-e5-small for embeddings (runs on CPU, supports Icelandic).
"""
import os, json, re, logging, time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Histogram, Counter
from telemetry import setup_tracing

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

tracer = setup_tracing("icelandic-tutor-rag")

EMBED_DURATION = Histogram(
    "rag_embed_duration_seconds", "Time to embed a query",
    buckets=[.01, .05, .1, .25, .5, 1, 2])
QUERY_DURATION = Histogram(
    "rag_chroma_query_duration_seconds", "ChromaDB vector search duration",
    buckets=[.005, .01, .025, .05, .1, .25, .5])
CHUNKS_INGESTED = Counter(
    "rag_chunks_ingested_total", "Total text chunks embedded into ChromaDB")

PDFS_DIR    = os.getenv("PDFS_DIR",    "/app/pdfs")
CHROMA_DIR  = os.getenv("CHROMA_DIR",  "/data/chroma")
EMBED_MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")
CHUNK_SIZE  = int(os.getenv("CHUNK_SIZE",  "400"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "60"))
TOP_K       = int(os.getenv("TOP_K", "3"))

os.makedirs(CHROMA_DIR, exist_ok=True)
os.makedirs(PDFS_DIR,   exist_ok=True)

# ── Lazy-load heavy deps so startup is fast ───────────────────────────────────
_embed_model = None
_chroma_client = None
_collection = None

def get_embed_model():
    global _embed_model
    if _embed_model is None:
        logger.info(f"Loading embedding model: {EMBED_MODEL}")
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(EMBED_MODEL)
        logger.info("Embedding model loaded.")
    return _embed_model

def get_collection():
    global _chroma_client, _collection
    if _collection is None:
        import chromadb
        from chromadb.config import Settings
        _chroma_client = chromadb.PersistentClient(
            path=CHROMA_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
        _collection = _chroma_client.get_or_create_collection(
            name="icelandic_books",
            metadata={"hnsw:space": "cosine"}
        )
        logger.info(f"ChromaDB collection ready — {_collection.count()} chunks indexed.")
    return _collection

# ── Text chunking ─────────────────────────────────────────────────────────────
def chunk_text(text: str, source: str) -> list[dict]:
    """Split text into overlapping chunks with metadata."""
    # Clean up whitespace artifacts from PDF extraction
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    text = text.strip()

    # Split on paragraph boundaries first, then by size
    paragraphs = re.split(r'\n\n+', text)
    chunks = []
    current = ""
    current_start = 0
    chunk_idx = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If adding this paragraph exceeds chunk size, save current and start new
        words = (current + " " + para).split()
        if len(words) > CHUNK_SIZE and current:
            chunks.append({
                "text": current.strip(),
                "source": source,
                "chunk_id": chunk_idx,
            })
            chunk_idx += 1
            # Keep overlap: last N words of current chunk
            overlap_words = current.split()[-CHUNK_OVERLAP:]
            current = " ".join(overlap_words) + "\n\n" + para
        else:
            current = (current + "\n\n" + para).strip() if current else para

    # Don't forget the last chunk
    if current.strip():
        chunks.append({
            "text": current.strip(),
            "source": source,
            "chunk_id": chunk_idx,
        })

    return chunks

# ── PDF ingestion ─────────────────────────────────────────────────────────────
def ingest_pdf(pdf_path: str) -> dict:
    """Extract text from PDF, chunk it, embed and store in ChromaDB."""
    import pdfplumber

    path = Path(pdf_path)
    source_name = path.stem
    collection = get_collection()
    model = get_embed_model()

    # Check if already ingested
    existing = collection.get(where={"source": source_name}, limit=1)
    if existing["ids"]:
        count = collection.count()
        logger.info(f"{source_name} already ingested, skipping.")
        return {"status": "already_ingested", "source": source_name, "total_chunks": count}

    logger.info(f"Ingesting {path.name}...")
    full_text = ""
    page_count = 0

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text:
                full_text += f"\n\n{text}"
            if i % 50 == 0:
                logger.info(f"  Extracted {i+1}/{page_count} pages...")

    logger.info(f"Extracted {len(full_text)} chars from {page_count} pages. Chunking...")
    chunks = chunk_text(full_text, source_name)
    logger.info(f"Created {len(chunks)} chunks. Embedding...")

    # Embed in batches
    BATCH = 64
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i+BATCH]
        texts = [f"passage: {c['text']}" for c in batch]  # e5 prefix
        t0 = time.perf_counter()
        embeddings = model.encode(texts, normalize_embeddings=True).tolist()
        EMBED_DURATION.observe(time.perf_counter() - t0)
        ids = [f"{source_name}_{c['chunk_id']}" for c in batch]
        metadatas = [{"source": c["source"], "chunk_id": c["chunk_id"]} for c in batch]
        documents = [c["text"] for c in batch]
        collection.add(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents)
        CHUNKS_INGESTED.inc(len(batch))
        logger.info(f"  Embedded chunks {i}–{min(i+BATCH, len(chunks))}/{len(chunks)}")

    total = collection.count()
    logger.info(f"Ingestion complete. Total chunks in DB: {total}")
    return {"status": "ingested", "source": source_name, "chunks": len(chunks), "total_in_db": total}

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Icelandic RAG Service")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)

class QueryRequest(BaseModel):
    query: str
    top_k: Optional[int] = None
    source_filter: Optional[str] = None

@app.get("/health")
def health():
    try:
        col = get_collection()
        return {"status": "ok", "chunks_indexed": col.count(), "model": EMBED_MODEL}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.post("/ingest")
async def ingest_endpoint(pdf_filename: Optional[str] = None):
    """
    Ingest PDFs from the /app/pdfs directory.
    If pdf_filename is given, ingest only that file.
    Otherwise ingest all PDFs in the directory.
    """
    pdfs_path = Path(PDFS_DIR)
    if pdf_filename:
        files = [pdfs_path / pdf_filename]
    else:
        files = list(pdfs_path.glob("*.pdf"))

    if not files:
        raise HTTPException(404, f"No PDFs found in {PDFS_DIR}")

    results = []
    for f in files:
        if not f.exists():
            results.append({"file": str(f), "error": "not found"})
            continue
        try:
            result = ingest_pdf(str(f))
            results.append({"file": f.name, **result})
        except Exception as e:
            logger.error(f"Error ingesting {f.name}: {e}")
            results.append({"file": f.name, "error": str(e)})

    return {"results": results}

@app.post("/query")
def query_endpoint(req: QueryRequest):
    """
    Search for relevant passages from the books.
    Returns top_k most relevant chunks with their source.
    """
    if not req.query.strip():
        raise HTTPException(400, "Query cannot be empty")

    collection = get_collection()
    if collection.count() == 0:
        return {"chunks": [], "message": "No books indexed yet. Run /ingest first."}

    model = get_embed_model()
    with tracer.start_as_current_span("rag.query") as span:
        span.set_attribute("rag.query_text", req.query[:200])

        # e5 models use "query: " prefix for queries
        t0 = time.perf_counter()
        query_embedding = model.encode(
            f"query: {req.query}",
            normalize_embeddings=True
        ).tolist()
        EMBED_DURATION.observe(time.perf_counter() - t0)

        k = req.top_k or TOP_K
        where = {"source": req.source_filter} if req.source_filter else None

        t1 = time.perf_counter()
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(k, collection.count()),
            where=where,
            include=["documents", "metadatas", "distances"]
        )
        QUERY_DURATION.observe(time.perf_counter() - t1)

    chunks = []
    for i, doc in enumerate(results["documents"][0]):
        chunks.append({
            "text": doc,
            "source": results["metadatas"][0][i].get("source", "unknown"),
            "relevance": round(1 - results["distances"][0][i], 3),
        })

    return {"chunks": chunks, "query": req.query}

@app.get("/sources")
def list_sources():
    """List all ingested book sources."""
    collection = get_collection()
    if collection.count() == 0:
        return {"sources": [], "total_chunks": 0}
    # Get unique sources
    all_meta = collection.get(include=["metadatas"])
    sources = list({m["source"] for m in all_meta["metadatas"]})
    return {"sources": sources, "total_chunks": collection.count()}

@app.delete("/source/{source_name}")
def delete_source(source_name: str):
    """Remove all chunks from a specific source."""
    collection = get_collection()
    collection.delete(where={"source": source_name})
    return {"deleted": source_name, "remaining_chunks": collection.count()}

# ── Auto-ingest on startup ────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_ingest():
    """Auto-ingest any PDFs found in PDFS_DIR on startup."""
    pdfs = list(Path(PDFS_DIR).glob("*.pdf"))
    if not pdfs:
        logger.info(f"No PDFs found in {PDFS_DIR} — add PDFs and call POST /ingest")
        return
    logger.info(f"Found {len(pdfs)} PDFs in {PDFS_DIR}, checking if ingestion needed...")
    for pdf in pdfs:
        try:
            ingest_pdf(str(pdf))
        except Exception as e:
            logger.error(f"Startup ingest error for {pdf.name}: {e}")
