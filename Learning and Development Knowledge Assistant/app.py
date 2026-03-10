import os
import re
from dataclasses import dataclass
from typing import List, Tuple, Dict

import numpy as np 
import streamlit as st
import requests
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer
import faiss

# ----------------------------
# SETTINGS
# ----------------------------
PDF_PATH = ""

# Local LLM (Ollama)
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3.1:8b"

# Retrieval / chunking
DEFAULT_TOP_K = 5           # send this many chunks to the LLM
RETRIEVAL_POOL = 30         # retrieve more, then filter/rerank
MIN_SCORE_DEFAULT = 0.15    # fallback kicks in if nothing passes

CHUNK_MAX_WORDS = 220       # paragraph-packed chunk size
OVERLAP_PARAS = 1           # overlap in paragraphs

# LLM generation
NUM_PREDICT = 256
TIMEOUT = 600


# ----------------------------
# DATA STRUCTURES
# ----------------------------
@dataclass(frozen=True)
class Chunk:
    text: str
    page: int  # 1-based page number


# ----------------------------
# PDF -> TEXT HELPERS
# ----------------------------
def clean_text(t: str) -> str:
    if not t:
        return ""
    t = t.replace("\r", "\n")
    # collapse excessive spaces/tabs
    t = re.sub(r"[ \t]+", " ", t)
    # normalize many blank lines
    t = re.sub(r"\n{3,}", "\n\n", t)
    # remove repeated page header/footer artifacts (optional, light-touch)
    return t.strip()


def chunk_paragraphs(text: str, max_words: int = 220, overlap_paras: int = 1) -> List[str]:
    """
    Chunk by paragraphs to preserve headings + bullets.
    Builds chunks up to ~max_words each, then overlaps the last N paragraphs.
    """
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paras:
        return []

    chunks: List[str] = []
    buf: List[str] = []
    buf_words = 0

    def flush():
        nonlocal buf, buf_words
        if buf:
            chunks.append("\n\n".join(buf).strip())
        buf, buf_words = [], 0

    for p in paras:
        p_words = len(p.split())
        if buf and (buf_words + p_words > max_words):
            flush()
        buf.append(p)
        buf_words += p_words

    flush()

    # apply paragraph overlap
    if overlap_paras > 0 and len(chunks) > 1:
        overlapped: List[str] = []
        prev_paras_cache: List[str] = []
        for i, ch in enumerate(chunks):
            ch_paras = ch.split("\n\n")
            if i == 0:
                overlapped.append(ch)
            else:
                overlap = prev_paras_cache[-overlap_paras:] if prev_paras_cache else []
                overlapped.append("\n\n".join(overlap + ch_paras).strip())
            prev_paras_cache = ch_paras
        chunks = overlapped

    return chunks


def load_pdf_chunks(pdf_path: str) -> List[Chunk]:
    reader = PdfReader(pdf_path)
    all_chunks: List[Chunk] = []

    for i, page in enumerate(reader.pages, start=1):
        raw = page.extract_text() or ""
        raw = clean_text(raw)
        if not raw:
            continue

        page_chunks = chunk_paragraphs(raw, max_words=CHUNK_MAX_WORDS, overlap_paras=OVERLAP_PARAS)
        for ch in page_chunks:
            # avoid tiny chunks
            if len(ch.split()) < 20:
                continue
            all_chunks.append(Chunk(text=ch, page=i))

    return all_chunks


# ----------------------------
# EMBEDDINGS + FAISS
# ----------------------------
@st.cache_resource
def get_embedder() -> SentenceTransformer:
    return SentenceTransformer("all-MiniLM-L6-v2")


def build_faiss_index(chunks: List[Chunk], embedder: SentenceTransformer):
    texts = [c.text for c in chunks]
    embs = embedder.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False
    ).astype(np.float32)

    dim = int(embs.shape[1])
    index = faiss.IndexFlatIP(dim)  # cosine similarity via normalized vectors
    index.add(embs)
    return index


def retrieve_embeddings(
    query: str,
    chunks: List[Chunk],
    embedder: SentenceTransformer,
    index,
    pool: int = 30
) -> List[Tuple[float, Chunk]]:
    q_emb = embedder.encode([query], convert_to_numpy=True, normalize_embeddings=True).astype(np.float32)
    scores, idxs = index.search(q_emb, pool)

    results: List[Tuple[float, Chunk]] = []
    for score, idx in zip(scores[0], idxs[0]):
        if idx == -1:
            continue
        results.append((float(score), chunks[int(idx)]))

    results.sort(key=lambda x: x[0], reverse=True)
    return results


def keyword_retrieve(query: str, chunks: List[Chunk], k: int = 8) -> List[Tuple[float, Chunk]]:
    """
    Very simple lexical fallback: counts how many query terms appear in a chunk.
    Great for exact phrases, acronyms, section numbers, etc.
    """
    q_terms = [t.lower() for t in re.findall(r"\w+", query)]
    if not q_terms:
        return []

    scored: List[Tuple[float, Chunk]] = []
    for ch in chunks:
        txt = ch.text.lower()
        hits = 0
        for t in q_terms:
            if t in txt:
                hits += 1
        if hits > 0:
            scored.append((float(hits), ch))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:k]


def hybrid_retrieve(
    query: str,
    chunks: List[Chunk],
    embedder: SentenceTransformer,
    index,
    top_k: int,
    min_score: float,
    pool: int = 30
) -> List[Tuple[float, Chunk]]:
    emb_pool = retrieve_embeddings(query, chunks, embedder, index, pool=pool)

    # filter by min_score; fallback if empty
    emb_filtered = [(s, c) for (s, c) in emb_pool if s >= min_score]
    if not emb_filtered:
        emb_filtered = emb_pool[:max(3, top_k)]

    kw = keyword_retrieve(query, chunks, k=max(5, top_k))

    # merge and de-dupe 
    seen = set()
    merged: List[Tuple[float, Chunk]] = []

    for s, ch in emb_filtered + kw:
        key = (ch.page, ch.text)
        if key in seen:
            continue
        seen.add(key)
        merged.append((s, ch))

    return merged[:top_k]


# ----------------------------
# LOCAL LLM (OLLAMA)
# ----------------------------
def ollama_generate(prompt: str, temperature: float = 0.2) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": float(temperature),
            "num_predict": int(NUM_PREDICT),
        },
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def build_prompt(question: str, retrieved: List[Tuple[float, Chunk]]) -> str:
    excerpts = []
    for i, (_, ch) in enumerate(retrieved, start=1):
        excerpts.append(f"[Source {i} | Page {ch.page}]\n{ch.text}")

    context = "\n\n---\n\n".join(excerpts)

    return f"""
You are an internal Learning & Development Policy Assistant.

Rules:
- Answer ONLY using the provided policy excerpts.
- If the answer is not present in the excerpts, say exactly: "I can't find that in the policy document."
- Be concise and factual.
- Include citations like (Page X).
- If you can't find an exact answer, state that, and optionally mention the closest relevant excerpt with a page citation.

Policy excerpts:
{context}

Question:
{question}

Answer:
""".strip()


# ----------------------------
# STREAMLIT APP
# ----------------------------
st.set_page_config(page_title="L&D Policy Assistant", layout="wide")
st.title("📘 L&D Policy Assistant")
st.caption("Local Document Search with AI")

with st.sidebar:
    st.subheader("Settings")
    st.write(f"PDF: `{PDF_PATH}`")

    temp = st.slider("LLM temperature", 0.0, 1.0, 0.2, 0.1)
    top_k = st.slider("Top K sources (sent to LLM)", 3, 10, DEFAULT_TOP_K, 1)
    pool = st.slider("Retrieval pool size", 10, 80, RETRIEVAL_POOL, 5)
    min_score = st.slider("Min embedding relevance threshold", 0.0, 1.0, MIN_SCORE_DEFAULT, 0.05)

    st.markdown("---")
    debug = st.checkbox("Debug: show retrieval scores", value=False)
    show_sources = st.checkbox("Show sources", value=True)

    st.markdown("---")
    st.markdown("**Local LLM requirement:** Ollama running on your PC.")
    st.code(f"ollama pull {OLLAMA_MODEL}\nollama serve", language="bash")


# Load PDF
if not os.path.exists(PDF_PATH):
    st.error(f"Cannot find `{PDF_PATH}` in the same folder as app.py. Please place the PDF there.")
    st.stop()

embedder = get_embedder()

@st.cache_resource
def load_index_cached(pdf_path: str):
    chunks = load_pdf_chunks(pdf_path)
    if not chunks:
        raise ValueError("No text extracted from PDF. If it's scanned, you may need OCR.")
    index = build_faiss_index(chunks, embedder)
    return chunks, index

try:
    chunks, index = load_index_cached(PDF_PATH)
except Exception as e:
    st.error(f"Failed to build index: {e}")
    st.stop()

st.success(f"Policy document is ready for questions")

q = st.text_input("Ask a question (e.g.,'Who is eligible?')")

ask_btn = st.button("🔎 Search + Answer", type="primary")

if ask_btn and q.strip():
    query = q.strip()

    retrieved = hybrid_retrieve(
        query=query,
        chunks=chunks,
        embedder=embedder,
        index=index,
        top_k=top_k,
        min_score=min_score,
        pool=pool
    )

    if not retrieved:
        st.warning("No matches found in the PDF. Try rephrasing your question or lowering the threshold.")
        st.stop()

    if debug:
        st.subheader("Debug: Retrieved chunks")
        for i, (score, ch) in enumerate(retrieved, start=1):
            st.write(f"{i}. Score={score:.3f} | Page {ch.page} | {ch.text[:160]}...")

    prompt = build_prompt(query, retrieved)

    with st.spinner("Finding the answer..."):
        try:
            ans = ollama_generate(prompt, temperature=temp)
        except Exception as e:
            st.error(f"LLM error: {e}")
            st.stop()

    st.subheader("Answer")
    st.write(ans)

    if show_sources:
        st.subheader("Sources used")
        for i, (score, ch) in enumerate(retrieved, start=1):
            with st.expander(f"Source {i} • Page {ch.page} • Score {score:.3f}"):
                st.write(ch.text)
