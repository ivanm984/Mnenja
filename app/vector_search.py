# -*- coding: utf-8 -*-
"""
vector_search.py
----------------
Hibridni priklic znanja (vektorsko + BM25, če je na voljo) z MMR re-rankingom,
povzetki s citati in varno degradacijo. Namenjeno kot drop-in zamenjava.

Neodvisno od konkretne baze in AI odjemalca:
- DB adapter pričakujemo prek objekta db_manager (glej spodaj metode, vse so opcijske).
- Embedding funkcijo lahko podaš kot argument; če je ne, modul poskusi najti v ai.py.

Avtor: tvoja bodoča najljubša funkcija ;)
"""

from __future__ import annotations
import hashlib
import logging
import math
import time
from dataclasses import dataclass, asdict
from functools import lru_cache
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    fmt = logging.Formatter("[%(levelname)s] %(asctime)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------
# Vrste & helperji
# ---------------------------------------------------------

@dataclass
class Row:
    id: str
    text: str
    similarity: float = 0.0
    source: Optional[str] = None
    article: Optional[str] = None   # člen
    paragraph: Optional[str] = None # odstavek
    page: Optional[str] = None
    eup: Optional[str] = None
    land_use: Optional[str] = None
    year: Optional[str] = None
    embedding: Optional[List[float]] = None
    meta: Dict[str, Any] = None

    @staticmethod
    def from_any(d: Dict[str, Any]) -> "Row":
        # Najdi id
        _id = str(
            d.get("id")
            or d.get("row_id")
            or d.get("doc_id")
            or d.get("pk")
            or d.get("_id")
            or hashlib.sha1(repr(sorted(d.items())).encode("utf-8")).hexdigest()[:16]
        )
        # Najdi tekst
        text = d.get("text") or d.get("vsebina") or d.get("content") or d.get("chunk") or ""
        # Najdi podobnost / score
        sim = d.get("similarity")
        if sim is None:
            sim = d.get("score")
        try:
            similarity = float(sim) if sim is not None else 0.0
        except Exception:
            similarity = 0.0

        emb = d.get("embedding") or d.get("doc_embedding") or None

        return Row(
            id=_id,
            text=text or "",
            similarity=similarity,
            source=d.get("vir") or d.get("source") or d.get("origin"),
            article=d.get("clen") or d.get("article"),
            paragraph=d.get("odstavek") or d.get("paragraph"),
            page=d.get("stran") or d.get("page"),
            eup=d.get("eup") or d.get("EUP"),
            land_use=d.get("namenska_raba") or d.get("land_use"),
            year=d.get("leto") or d.get("year"),
            embedding=emb,
            meta=d,
        )

def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b: return 0.0
    s = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a)) or 1.0
    nb = math.sqrt(sum(y*y for y in b)) or 1.0
    return s / (na * nb)

def _token_set(s: str) -> set:
    return {t for t in "".join(ch.lower() if ch.isalnum() else " " for ch in (s or "")).split() if t}

def _jaccard(a: str, b: str) -> float:
    A, B = _token_set(a), _token_set(b)
    if not A or not B: return 0.0
    return len(A & B) / float(len(A | B))

def _normalise(values: List[float]) -> List[float]:
    if not values: return []
    lo, hi = min(values), max(values)
    if abs(hi - lo) < 1e-12:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]

# ---------------------------------------------------------
# Embedding klient (fleksibilen)
# ---------------------------------------------------------

# Poskusi samodejno najti embed funkcijo v ai.py (če obstaja)
_default_embed_fn: Optional[Callable[[str], List[float]]] = None
try:
    # Poskus več mogočih imen
    import ai  # type: ignore
    for cand in ("embed_query", "embed_text", "embed_content", "embed", "get_embedding"):
        if hasattr(ai, cand) and callable(getattr(ai, cand)):
            _default_embed_fn = getattr(ai, cand)  # type: ignore
            logger.info(f"vector_search: našel embed funkcijo v ai.py: {cand}()")
            break
except Exception:
    pass

@lru_cache(maxsize=1024)
def _cached_embed_hash(clean_text: str) -> str:
    return hashlib.sha256(clean_text.encode("utf-8")).hexdigest()

def embed_query(text: str, embed_fn: Optional[Callable[[str], List[float]]] = None) -> List[float]:
    """
    Vrne embedding za poizvedbo. Keširano po besedilu.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    # Keš ključ
    _ = _cached_embed_hash(cleaned)

    fn = embed_fn or _default_embed_fn
    if fn is None:
        raise RuntimeError(
            "embed_query: manjka embed funkcija. Podaj embed_fn ali poskrbi, da ai.py vsebuje npr. embed_query(text)->vector."
        )
    try:
        vec = fn(cleaned)  # pričakujemo List[float]
        if not isinstance(vec, (list, tuple)):
            raise TypeError("Embed funkcija ni vrnila seznama.")
        return list(vec)
    except Exception as e:
        logger.warning(f"embed_query: padel embed klic: {e}")
        return []

# ---------------------------------------------------------
# Query builder
# ---------------------------------------------------------

def _build_query_text(key_data: Dict[str, Any], eup: Optional[str], namenska_raba: Optional[str]) -> str:
    """
    Zgradi fokusiran tekst za embedding iz ključnih podatkov.
    """
    parts = []

    def add(label: str, value: Optional[str]):
        v = (value or "").strip()
        if v:
            parts.append(f"{label}: {v}")

    # Bistvene komponente
    if key_data:
        # najprej številčne omejitve, ker so močne pri filtriranju pravil
        for k in ("odmik", "FZ", "FI", "etažnost", "višina", "gabarit", "naklon", "parcelna_meja"):
            if k in key_data and key_data[k] not in (None, ""):
                add(k, str(key_data[k]))
        # ostalo
        for k, v in key_data.items():
            if k in ("odmik", "FZ", "FI", "etažnost", "višina", "gabarit", "naklon", "parcelna_meja"):
                continue
            add(k, str(v))

    add("EUP", eup)
    add("namenska raba", namenska_raba)

    return " | ".join(parts) or "prostorska pravila EUP namenska raba pogoji FZ FI odmiki"

# ---------------------------------------------------------
# Hibridni priklic + MMR
# ---------------------------------------------------------

def _db_has(obj: Any, name: str) -> bool:
    return hasattr(obj, name) and callable(getattr(obj, name))

def _normalise_scores(rows: List[Row]) -> Dict[str, float]:
    vals = [r.similarity for r in rows] or [0.0]
    norm_vals = _normalise(vals)
    return {r.id: s for r, s in zip(rows, norm_vals)}

def _mmr(rows: List[Row], top_k: int = 12, lambda_: float = 0.75) -> List[Row]:
    """
    Maximal Marginal Relevance na podlagi dokumentnih embeddingov, če so,
    sicer fallback na Jaccard podobnost besedila (za diverziteto).
    """
    if not rows:
        return []

    # Pripravi podobnost med dokumenti
    def div_sim(a: Row, b: Row) -> float:
        if a.embedding and b.embedding:
            return _cos(a.embedding, b.embedding)
        return _jaccard(a.text, b.text)

    selected: List[Row] = []
    pool: List[Row] = rows[:]

    while pool and len(selected) < top_k:
        best = None
        best_score = -1e9
        for cand in pool:
            rel = cand.similarity  # že normalizirano pred klicem
            if selected:
                max_div = max(div_sim(cand, s) for s in selected)
            else:
                max_div = 0.0
            score = lambda_ * rel - (1.0 - lambda_) * max_div
            if score > best_score:
                best = cand
                best_score = score
        selected.append(best)  # type: ignore
        pool.remove(best)      # type: ignore

    return selected

def _fetch_doc_embeddings_if_possible(db_manager: Any, ids: List[str]) -> Dict[str, List[float]]:
    vecs: Dict[str, List[float]] = {}
    if _db_has(db_manager, "get_document_embeddings"):
        try:
            res = db_manager.get_document_embeddings(ids)
            # pričakujemo dict ali seznam map
            if isinstance(res, dict):
                for k, v in res.items():
                    if isinstance(v, (list, tuple)):
                        vecs[str(k)] = list(v)
            elif isinstance(res, list):
                for r in res:
                    rid = str(r.get("id") or r.get("doc_id") or r.get("row_id"))
                    emb = r.get("embedding") or r.get("doc_embedding")
                    if rid and isinstance(emb, (list, tuple)):
                        vecs[rid] = list(emb)
        except Exception as e:
            logger.debug(f"get_document_embeddings ni uspelo: {e}")
    return vecs

def _to_rows(raw: Iterable[Dict[str, Any]]) -> List[Row]:
    return [Row.from_any(d) for d in (raw or [])]

def hybrid_search(
    db_manager: Any,
    query_text: str,
    query_embedding: List[float],
    k: int = 12,
    alpha: float = 0.6,
) -> List[Row]:
    """
    Združi vektorsko in ključno-besedno iskanje (če BM25 obstaja). Nato MMR.
    """
    t0 = time.time()

    # 1) vektorsko
    vec_raw: List[Dict[str, Any]] = []
    if _db_has(db_manager, "search_vector_knowledge") and query_embedding:
        try:
            vec_raw = db_manager.search_vector_knowledge(query_embedding, limit=max(3 * k, 50))
        except Exception as e:
            logger.warning(f"search_vector_knowledge padel: {e}")
    vec_rows = _to_rows(vec_raw)

    # 2) BM25 / FTS (opcijsko)
    bm_raw: List[Dict[str, Any]] = []
    if _db_has(db_manager, "search_keyword_knowledge") and query_text:
        try:
            bm_raw = db_manager.search_keyword_knowledge(query_text, limit=max(3 * k, 50))
        except Exception as e:
            logger.debug(f"search_keyword_knowledge ni uspelo: {e}")
    bm_rows = _to_rows(bm_raw)

    # Če BM25 ni, gremo samo z vektorji
    if not bm_rows and not vec_rows:
        logger.info("hybrid_search: ni rezultatov (niti vektor niti BM25).")
        return []

    if not bm_rows:
        # Samo vektorji + MMR
        # Normaliziraj similarity za MMR
        norm = _normalise_scores(vec_rows)
        for r in vec_rows:
            r.similarity = norm.get(r.id, 0.0)
        # Dopolni embeddinge, če jih ni
        emb_map = _fetch_doc_embeddings_if_possible(db_manager, [r.id for r in vec_rows])
        for r in vec_rows:
            if r.embedding is None and r.id in emb_map:
                r.embedding = emb_map[r.id]
        reranked = _mmr(vec_rows, top_k=k, lambda_=0.75)
        logger.info(f"hybrid_search: samo VEKTORSKO, vrnjenih {len(reranked)} (t={time.time()-t0:.3f}s)")
        return reranked

    # 3) Fuzija
    v_norm = _normalise_scores(vec_rows) if vec_rows else {}
    b_norm = _normalise_scores(bm_rows)

    merged: Dict[str, Row] = {}
    score: Dict[str, float] = {}

    for r in vec_rows:
        merged[r.id] = r
        score[r.id] = alpha * v_norm.get(r.id, 0.0)

    for r in bm_rows:
        if r.id not in merged:
            merged[r.id] = r
        score[r.id] = max(score.get(r.id, 0.0), (1.0 - alpha) * b_norm.get(r.id, 0.0))

    # Pretvori v seznam in nastavi rel. score za MMR
    rows = list(merged.values())
    for r in rows:
        r.similarity = score.get(r.id, 0.0)

    # Dopolni embeddinge za MMR, če so na voljo v bazi
    emb_map = _fetch_doc_embeddings_if_possible(db_manager, [r.id for r in rows])
    for r in rows:
        if r.embedding is None and r.id in emb_map:
            r.embedding = emb_map[r.id]

    reranked = _mmr(rows, top_k=k, lambda_=0.75)
    logger.info(f"hybrid_search: HYBRID+MMR, vrnjenih {len(reranked)} (t={time.time()-t0:.3f}s)")
    return reranked

# ---------------------------------------------------------
# Povzetek + citati + kontekst
# ---------------------------------------------------------

def summarise_row_for_prompt(r: Row, idx: int) -> str:
    """
    Jedrnata vrstica + citat. Pazimo na enovrstičnost zaradi tokenov.
    """
    cite_bits = []
    if r.source: cite_bits.append(f"Vir: {r.source}")
    if r.article: cite_bits.append(f"Člen: {r.article}")
    if r.paragraph: cite_bits.append(f"Odst.: {r.paragraph}")
    if r.page: cite_bits.append(f"Stran: {r.page}")
    if r.eup: cite_bits.append(f"EUP: {r.eup}")
    if r.land_use: cite_bits.append(f"Namenska raba: {r.land_use}")
    if r.year: cite_bits.append(f"Leto: {r.year}")

    cite = ", ".join(cite_bits) if cite_bits else "Vir: (neznan)"
    text_one_line = " ".join((r.text or "").strip().split())
    if len(text_one_line) > 350:
        text_one_line = text_one_line[:347] + "..."

    return f"[{idx}] {cite} — “{text_one_line}”"

def build_context_block(rows: List[Row]) -> str:
    """
    Blok za prompt: kratke alineje z ID-ji in citati.
    """
    lines = [summarise_row_for_prompt(r, i+1) for i, r in enumerate(rows)]
    header = "Relevantna pravila (citati):"
    return header + "\n" + "\n".join(lines)

# ---------------------------------------------------------
# Glavni vstop: pripravi kontekst za LLM
# ---------------------------------------------------------

def get_vector_context(
    db_manager: Any,
    key_data: Dict[str, Any],
    eup: Optional[str],
    namenska_raba: Optional[str],
    *,
    k: int = 12,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Vrne:
      - context_text (str): lepo formatiran blok s citati
      - rows_json (List[dict]): surovi podatki (za log ali UI)

    Ta funkcija je namenjena uporabi v routes.py (drop-in zamenjava za dosedanji 'vector_context_text').
    """
    query_text = _build_query_text(key_data or {}, eup, namenska_raba)
    q_emb = []
    try:
        q_emb = embed_query(query_text, embed_fn=embed_fn)
    except Exception as e:
        logger.warning(f"get_vector_context: embed_query ni uspelo: {e}")

    rows = hybrid_search(db_manager, query_text, q_emb, k=k, alpha=0.6)
    context_text = build_context_block(rows)
    rows_json = [asdict(r) for r in rows]
    return context_text, rows_json

