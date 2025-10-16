# -*- coding: utf-8 -*-
"""
routes.py
---------
Drop-in datoteka z varnim uvodom hibridnega iskanja in citatov.
- Ohranja 'router = APIRouter()' in doda en POST endpoint /ask (če že obstaja, preimenuj path ali izbriši spodnji endpoint).
- Če imaš svoj obstoječ endpoint, lahko samo uporabiš funkcijo `prepare_prompt_parts(...)`.

Odvisnosti:
- ai.py (opcijsko): build_prompt(question, vector_context, extra) in call_llm(prompt) ali ask_llm(prompt).
  Če ai.py tega nima, endpoint vrne debug JSON (da ne pade).
- vector_search.py: get_vector_context(...)
- db_manager: objekt, ki ga že uporabljaš za branje iz baze (predaj v dependency ali ga tu uvozi).
"""

from __future__ import annotations
from typing import Any, Dict, Optional, Tuple
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.logging_config import get_logger
from app.vector_search import get_vector_context  # naš novi modul

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------
# AI adapter (fleksibilen)
# ---------------------------------------------------------
_build_prompt = None
_call_llm = None
try:
    # Poskusi najti v ai.py tipične funkcije
    import ai  # type: ignore
    for name in ("build_prompt", "make_prompt", "compose_prompt"):
        if hasattr(ai, name):
            _build_prompt = getattr(ai, name)
            logger.info(f"routes: našel build_prompt v ai.py: {name}()")
            break
    for name in ("call_llm", "ask_llm", "generate", "infer"):
        if hasattr(ai, name):
            _call_llm = getattr(ai, name)
            logger.info(f"routes: našel LLM klic v ai.py: {name}()")
            break
except Exception as e:
    logger.debug(f"routes: ai adapter ni na voljo ({e})")

# ---------------------------------------------------------
# DB manager (sem postavi svojo injekcijo ali import)
# ---------------------------------------------------------

def get_db_manager() -> Any:
    """
    V tvoj projekt vnesi pravi db_manager.
    Primer:
        from mydb import DatabaseManager
        return DatabaseManager(...)
    Trenutno vrnemo globalni objekt, če obstaja, sicer pa sprožimo napako ob uporabi.
    """
    try:
        # Če imaš svoj global ali provider, sem ga daj.
        import db  # type: ignore
        if hasattr(db, "db_manager"):
            return getattr(db, "db_manager")
    except Exception:
        pass
    # Fallback: uporabnik naj prepiše to funkcijo na svoj vir
    class _Dummy:
        def __getattr__(self, item):
            raise RuntimeError("DB manager ni konfiguriran. Uredi get_db_manager() v routes.py.")
    return _Dummy()

# ---------------------------------------------------------
# Pomožne funkcije za pripravo promp­ta
# ---------------------------------------------------------

def prepare_prompt_parts(
    *,
    question: str,
    key_data: Dict[str, Any],
    eup: Optional[str],
    namenska_raba: Optional[str],
    db_manager: Any,
) -> Tuple[str, Dict[str, Any]]:
    """
    Pripravi:
      - prompt_text: končni prompt (če obstaja ai.build_prompt); sicer minimalni prompt.
      - debug_payload: vsebina za log ali UI.
    """
    # 1) Pridobi kontekst iz hibridnega iskanja
    started = time.perf_counter()
    vector_context_text, rows = get_vector_context(
        db_manager=db_manager,
        key_data=key_data or {},
        eup=eup,
        namenska_raba=namenska_raba,
        k=12,
        embed_fn=None,  # če želiš, lahko podaš svojo embed funkcijo
    )

    # 2) Zgradi prompt (če imaš funkcijo v ai.py), drugače minimalni fallback
    if _build_prompt:
        try:
            prompt_text = _build_prompt(
                question=question,
                vector_context=vector_context_text,
                extra={"key_data": key_data, "eup": eup, "namenska_raba": namenska_raba},
            )
        except Exception as e:
            logger.warning(f"build_prompt padel, uporabim fallback: {e}")
            prompt_text = (
                "NAVODILA: Odgovori natančno in citiraj samo vire v razdelku 'Relevantna pravila (citati)'.\n\n"
                + vector_context_text + "\n\n"
                + f"VPRAŠANJE: {question}"
            )
    else:
        prompt_text = (
            "NAVODILA: Odgovori natančno in citiraj samo vire v razdelku 'Relevantna pravila (citati)'.\n\n"
            + vector_context_text + "\n\n"
            + f"VPRAŠANJE: {question}"
        )

    prompt_build_duration = time.perf_counter() - started
    debug_payload = {
        "vector_context_preview": vector_context_text,
        "rows": rows,  # za UI ali log
        "key_data": key_data,
        "eup": eup,
        "namenska_raba": namenska_raba,
    }
    logger.info(
        "prepare_prompt_parts: prompt ready (prompt_chars=%d, rows=%d, duration=%.3fs)",
        len(prompt_text),
        len(rows),
        prompt_build_duration,
    )
    logger.debug("prepare_prompt_parts: prompt preview=%r", prompt_text[:500])
    return prompt_text, debug_payload

# ---------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------
router = APIRouter()

class AskIn(BaseModel):
    question: str
    key_data: Dict[str, Any] = {}
    eup: Optional[str] = None
    namenska_raba: Optional[str] = None

class AskOut(BaseModel):
    answer: str
    debug: Dict[str, Any]

@router.post("/ask", response_model=AskOut)
def ask_endpoint(
    payload: AskIn,
    db_manager: Any = Depends(get_db_manager),
):
    """
    Vprašaš model; dobiš odgovor + debug (citati, vrstice).
    Če v ai.py ni klica modela, vrnemo prompt za debug (da sistem ne pade).
    """
    request_started = time.perf_counter()
    logger.info(
        "ask_endpoint: received question (chars=%d, key_fields=%d, eup=%s, namenska=%s)",
        len(payload.question or ""),
        len(payload.key_data or {}),
        payload.eup or "-",
        payload.namenska_raba or "-",
    )
    logger.debug("ask_endpoint: payload=%s", payload.dict())
    try:
        prompt_text, debug_payload = prepare_prompt_parts(
            question=payload.question,
            key_data=payload.key_data,
            eup=payload.eup,
            namenska_raba=payload.namenska_raba,
            db_manager=db_manager,
        )
    except Exception as e:
        logger.exception("Napaka pri pripravi promp­ta")
        raise HTTPException(status_code=500, detail=f"Napaka pri pripravi konteksta: {e}")

    # Če obstaja LLM klic v ai.py, ga uporabimo
    if _call_llm:
        try:
            llm_start = time.perf_counter()
            answer = _call_llm(prompt_text)
            llm_duration = time.perf_counter() - llm_start
            total_duration = time.perf_counter() - request_started
            logger.info(
                "ask_endpoint: LLM answered in %.3fs (total=%.3fs, answer_chars=%d)",
                llm_duration,
                total_duration,
                len(answer or ""),
            )
            logger.debug("ask_endpoint: answer preview=%r", (answer or "")[:500])
            return AskOut(answer=answer, debug=debug_payload)
        except Exception as e:
            logger.warning(f"LLM klic padel, vrnem fallback: {e}")

    # Fallback: vrni prompt,
    # da lahko vidiš kontekst in preveriš integracijo brez padca sistema
    total_duration = time.perf_counter() - request_started
    logger.info(
        "ask_endpoint: returning fallback prompt (total=%.3fs)",
        total_duration,
    )
    return AskOut(
        answer="(DEBUG fallback) LLM klic ni konfiguriran. Tukaj je prompt, ki bi ga poslal:\n\n" + prompt_text,
        debug=debug_payload,
    )
