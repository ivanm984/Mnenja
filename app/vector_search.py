"""Helpers for retrieving relevant knowledge snippets from the vector store."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

import google.generativeai as genai

from .config import API_KEY, EMBEDDING_MODEL
from .database import DatabaseManager

# Ensure the Gemini client is initialised for embedding requests as well.
genai.configure(api_key=API_KEY)


def _normalise_embedding(raw_embedding: object) -> List[float]:
    if isinstance(raw_embedding, list) and raw_embedding and isinstance(raw_embedding[0], (int, float)):
        return [float(x) for x in raw_embedding]
    if isinstance(raw_embedding, list) and raw_embedding and isinstance(raw_embedding[0], list):
        return [float(x) for x in raw_embedding[0]]
    if isinstance(raw_embedding, dict):
        values = raw_embedding.get("values") or raw_embedding.get("embedding")
        if isinstance(values, list):
            return _normalise_embedding(values)
    raise ValueError("Gemini embed_content ni vrnil veljavnega vektorja.")


def _build_query_text(
    project_text: str,
    *,
    eup: Optional[Sequence[str]] = None,
    namenske_rabe: Optional[Sequence[str]] = None,
) -> str:
    parts: List[str] = []
    eup_clean = [item.strip() for item in eup or [] if item and item.strip()]
    rabe_clean = [item.strip() for item in namenske_rabe or [] if item and item.strip()]
    if rabe_clean:
        parts.append("Namenske rabe: " + ", ".join(sorted(dict.fromkeys(rabe_clean))))
    if eup_clean:
        parts.append("Enote urejanja prostora: " + ", ".join(sorted(dict.fromkeys(eup_clean))))
    trimmed_text = " ".join((project_text or "").split())
    if len(trimmed_text) > 8000:
        trimmed_text = trimmed_text[:8000]
    if trimmed_text:
        parts.append(trimmed_text)
    return "\n\n".join(parts)


def embed_query(text: str) -> List[float]:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Vsebina za vektorsko poizvedbo je prazna.")
    response = genai.embed_content(
        model=EMBEDDING_MODEL,
        content=cleaned,
        task_type="RETRIEVAL_QUERY",
    )
    if isinstance(response, dict):
        embedding_obj = response.get("embedding")
    else:
        embedding_obj = getattr(response, "embedding", None)
    if embedding_obj is None:
        raise ValueError("Rezultat vektorske poizvedbe ne vsebuje polja 'embedding'.")
    return _normalise_embedding(embedding_obj)


def summarise_chunk(text: str, *, max_chars: int = 700) -> str:
    normalised = re.sub(r"\s+", " ", (text or "").strip())
    if len(normalised) <= max_chars:
        return normalised
    truncated = normalised[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated + "…"


def search_vector_knowledge(
    db_manager: DatabaseManager,
    project_text: str,
    *,
    limit: int = 12,
    eup: Optional[Sequence[str]] = None,
    namenske_rabe: Optional[Sequence[str]] = None,
) -> Tuple[str, List[Dict[str, object]]]:
    """Return formatted context and raw rows from the vector knowledge base."""

    try:
        query_text = _build_query_text(project_text, eup=eup, namenske_rabe=namenske_rabe)
        embedding = embed_query(query_text)
    except Exception as exc:  # pragma: no cover - external service
        print(f"⚠️ Napaka pri pripravi vektorske poizvedbe: {exc}")
        return "", []

    try:
        rows = db_manager.search_vector_knowledge(embedding, limit=limit)
    except Exception as exc:  # pragma: no cover - database specific
        print(f"⚠️ Vektorsko iskanje v bazi ni uspelo: {exc}")
        return "", []

    formatted: List[Dict[str, object]] = []
    lines: List[str] = []
    for index, row in enumerate(rows, 1):
        record = dict(row)
        summary = summarise_chunk(str(record.get("vsebina") or ""))
        record["summary"] = summary
        record["similarity"] = float(record.get("similarity") or 0.0)
        formatted.append(record)
        lines.append(
            f"{index}. Vir: {record.get('vir', '—')} | Ključ: {record.get('kljuc', '—')} | Podobnost: {record['similarity']:.3f}\n"
            f"{summary}"
        )

    return "\n\n".join(lines), formatted


__all__ = ["search_vector_knowledge", "embed_query", "summarise_chunk"]
