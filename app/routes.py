from __future__ import annotations
from .ai import embed_query, call_gemini, call_gemini_for_initial_extraction, parse_ai_response

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

"""Application routes for the Mnenja assistant UI and API."""

from typing import Any, Dict, Iterable, List, Optional, Tuple
import sys
import io
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import DATA_DIR
from .database import DatabaseManager
from .files import save_revision_files
from .frontend import build_homepage
from .knowledge_base import (
    IZRAZI_TEXT,
    UREDBA_TEXT,
    build_requirements_from_db,
)
from .parsers import convert_pdf_pages_to_images, parse_pdf
from .prompts import build_prompt
from .reporting import generate_word_report
from .schemas import ConfirmReportPayload, SaveSessionPayload
from . import state as state_store
from .utils import infer_project_name
from .vector_search import get_vector_context

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "analysis.log"

LOGGER = logging.getLogger("mnenja.app")
if not LOGGER.handlers:
    formatter = logging.Formatter("[%(levelname)s] %(asctime)s %(name)s: %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

LOGGER.setLevel(logging.INFO)


IN_MEMORY_SAVED_SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_LOCK = threading.Lock()
REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


_DB_MANAGER: Optional[DatabaseManager] = None
_DB_ATTEMPTED = False
_DB_LOCK = threading.Lock()


def get_db_manager() -> Optional[DatabaseManager]:
    """Return a cached database manager instance, if configuration allows it."""

    global _DB_MANAGER, _DB_ATTEMPTED
    if _DB_ATTEMPTED:
        return _DB_MANAGER

    with _DB_LOCK:
        if _DB_ATTEMPTED:
            return _DB_MANAGER
        try:
            manager = DatabaseManager()
            manager.init_db()
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            LOGGER.warning("Database ni na voljo: %s", exc)
            _DB_MANAGER = None
        else:
            LOGGER.info("Vzpostavljena je bila povezava s podatkovno bazo (%s)", manager.backend)
            _DB_MANAGER = manager
        _DB_ATTEMPTED = True
    return _DB_MANAGER


def _ensure_session(session_id: str) -> Dict[str, Any]:
    with SESSION_LOCK:
        session = state_store.TEMP_STORAGE.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Seja ne obstaja ali je potekla.")
    return session


def _store_session(session_id: str, payload: Dict[str, Any]) -> None:
    with SESSION_LOCK:
        state_store.TEMP_STORAGE[session_id] = payload


def _update_session(session_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    with SESSION_LOCK:
        data = state_store.TEMP_STORAGE.get(session_id, {})
        data.update(updates)
        state_store.TEMP_STORAGE[session_id] = data
    return data


def _normalise_list(values: Iterable[str]) -> List[str]:
    result = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            result.append(text)
    return result


def _load_revision_images(image_payloads: List[bytes]):
    from PIL import Image  # Imported lazily to avoid mandatory dependency during cold start

    images = []
    for payload in image_payloads:
        try:
            images.append(Image.open(io.BytesIO(payload)))
        except Exception:  # pragma: no cover - depends on third party libraries
            LOGGER.warning("Grafične priloge ni mogoče odpreti.")
    return images


def _collect_analysis_context(
    db_manager: Optional[DatabaseManager],
    key_data: Dict[str, Any],
    eup_list: List[str],
    raba_list: List[str],
) -> Dict[str, Any]:
    if not db_manager:
        return {"context_text": "", "rows": []}

    eup = eup_list[0] if eup_list else None
    raba = raba_list[0] if raba_list else None
    try:
        context_text, rows = get_vector_context(
            db_manager=db_manager,
            key_data=key_data,
            eup=eup,
            namenska_raba=raba,
            k=12,
            embed_fn=embed_query
        )
    except Exception as exc:
        LOGGER.warning("Hibridno iskanje ni uspelo: %s", exc)
        return {"context_text": "", "rows": []}
    return {"context_text": context_text, "rows": rows}


def _append_revision(session: Dict[str, Any], requirement_id: Optional[str], record: Dict[str, Any]) -> None:
    revisions = session.setdefault("requirement_revisions", {})
    key = str(requirement_id or "__celotno")
    revisions.setdefault(key, []).append(record)


frontend_router = APIRouter()


@frontend_router.get("/", response_class=HTMLResponse)
def homepage() -> HTMLResponse:
    """Serve the SPA frontend."""

    return HTMLResponse(build_homepage())


def _read_upload(file: UploadFile) -> bytes:
    try:
        return file.file.read() if hasattr(file.file, "read") else file.read()
    finally:
        try:
            file.file.close()
        except Exception:
            pass


@frontend_router.post("/extract-data")
async def extract_data(
    pdf_files: List[UploadFile] = File(...),
    files_meta_json: str = Form("[]"),
) -> JSONResponse:
    """Extract key project data and initialise a new analysis session."""

    manifest: List[Dict[str, Any]]
    try:
        manifest = json.loads(files_meta_json) if files_meta_json else []
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Neveljaven opis datotek: {exc}") from exc

    if not pdf_files:
        raise HTTPException(status_code=400, detail="Naložite vsaj en PDF dokument.")

    aggregate_text_parts: List[str] = []
    image_payloads: List[bytes] = []
    stored_files: List[Dict[str, Any]] = []

    for index, upload in enumerate(pdf_files):
        file_bytes = _read_upload(upload)
        if not file_bytes:
            continue
        text_content = parse_pdf(file_bytes)
        if text_content:
            aggregate_text_parts.append(text_content)

        page_meta = ""
        if index < len(manifest):
            page_meta = manifest[index].get("pages", "") or ""
        for image in convert_pdf_pages_to_images(file_bytes, page_meta):
            buffer = io.BytesIO()
            try:
                image.save(buffer, format="PNG")
                image_payloads.append(buffer.getvalue())
            finally:
                buffer.close()

        stored_files.append(
            {
                "filename": upload.filename,
                "size": len(file_bytes),
                "pages": page_meta,
                "content": file_bytes,
            }
        )

    project_text = "\n\n".join(part for part in aggregate_text_parts if part)
    if not project_text.strip():
        raise HTTPException(status_code=400, detail="Iz PDF datotek ni bilo mogoče prebrati besedila.")

    extraction = call_gemini_for_initial_extraction(project_text, _load_revision_images(image_payloads))

    session_id = uuid4().hex
    timestamp = datetime.utcnow().isoformat()

    session_payload = {
        "session_id": session_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "project_text": project_text,
        "image_payloads": image_payloads,
        "files": stored_files,
        "details": extraction.get("details", {"eup": [], "namenska_raba": []}),
        "key_data": extraction.get("key_data", {}),
        "metadata": extraction.get("metadata", {}),
        "requirements": [],
        "results_map": {},
        "analysis_scope": None,
        "total_analyzed": None,
        "total_available": None,
        "requirement_revisions": {},
        "analysis_history": [],
        "saved_state": None,
    }
    _store_session(session_id, session_payload)

    # Prepare a response for the frontend that preserves the nested structure
    response_payload = {
        "session_id": session_id,
        "details": session_payload["details"],
        "metadata": session_payload["metadata"],
        "key_data": session_payload["key_data"],
    }
    return JSONResponse(response_payload)


@frontend_router.post("/analyze-report")
async def analyze_report(request: Request) -> JSONResponse:
    form = await request.form()
    session_id = (form.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="Seja ni podana.")

    session = _ensure_session(session_id)

    eup_list = _normalise_list(form.getlist("final_eup_list")) or session.get("eup", [])
    raba_list = _normalise_list(form.getlist("final_raba_list")) or session.get("namenska_raba", [])

    key_data = dict(session.get("key_data", {}))
    for key in key_data.keys():
        key_data[key] = (form.get(key) or "").strip() or key_data.get(key, "")

    metadata = dict(session.get("metadata", {}))
    for meta_key in ("ime_projekta", "stevilka_projekta", "datum_projekta", "projektant"):
        value = form.get(meta_key)
        if value is not None:
            metadata[meta_key] = value.strip()

    selected_ids: List[str] = []
    selected_ids_json = form.get("selected_ids_json")
    if selected_ids_json:
        try:
            selected_ids = [str(item) for item in json.loads(selected_ids_json) if item]
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Neveljaven seznam zahtev: {exc}") from exc

    project_text = session.get("project_text", "")
    if not project_text:
        raise HTTPException(status_code=400, detail="Seja ne vsebuje projektne dokumentacije.")

    requirements = build_requirements_from_db(eup_list, raba_list, project_text)
    analysis_scope = "partial" if selected_ids else "full"
    scoped_requirements = [req for req in requirements if (not selected_ids or req["id"] in selected_ids)]

    db_manager = get_db_manager()
    hybrid_context = _collect_analysis_context(db_manager, key_data, eup_list, raba_list)

    prompt = build_prompt(
        project_text=project_text,
        zahteve=scoped_requirements,
        izrazi_text=IZRAZI_TEXT,
        uredba_text=UREDBA_TEXT,
        vector_context=hybrid_context.get("context_text", ""),
    )

    images = _load_revision_images(session.get("image_payloads", []))
    started_at = time.perf_counter()
    ai_response_text, llm_metadata = call_gemini(prompt, images)
    elapsed = time.perf_counter() - started_at
    parsed_results = parse_ai_response(ai_response_text, scoped_requirements)

    existing_results = dict(session.get("results_map", {}))
    existing_results.update(parsed_results)

    non_compliant_ids = [
        rid
        for rid, result in existing_results.items()
        if isinstance(result, dict)
        and isinstance(result.get("skladnost"), str)
        and "nesklad" in result["skladnost"].lower()
    ]

    analysis_summary = (
        f"Analiziranih {len(scoped_requirements)} od {len(requirements)} zahtev. "
        f"Neskladnih: {len(non_compliant_ids)}."
    )

    llm_usage = (llm_metadata or {}).get("usage", {}) if llm_metadata else {}
    llm_duration = (llm_metadata or {}).get("duration") if llm_metadata else None
    analysis_history = list(session.get("analysis_history", []))
    analysis_record = {
        "timestamp": datetime.utcnow().isoformat(),
        "session_id": session_id,
        "scope": analysis_scope,
        "total_analyzed": len(scoped_requirements),
        "total_available": len(requirements),
        "non_compliant": len(non_compliant_ids),
        "prompt_tokens": llm_usage.get("prompt_tokens"),
        "response_tokens": llm_usage.get("candidates_tokens"),
        "total_tokens": llm_usage.get("total_tokens"),
        "llm_duration": llm_duration,
        "elapsed_seconds": round(elapsed, 3),
        "vector_rows": len(hybrid_context.get("rows", [])),
        "model": (llm_metadata or {}).get("model"),
        "analysis_summary": analysis_summary,
    }
    analysis_history.append(analysis_record)

    LOGGER.info(
        "Analiza zaključena | session=%s scope=%s zahteve=%s/%s neskladne=%s tokens=%s trajanje=%.3fs llm=%.3fs context=%s",
        session_id,
        analysis_scope,
        len(scoped_requirements),
        len(requirements),
        len(non_compliant_ids),
        llm_usage.get("total_tokens") or "—",
        elapsed,
        llm_duration if llm_duration is not None else 0.0,
        len(hybrid_context.get("rows", [])),
    )

    session_update = {
        "eup": eup_list,
        "namenska_raba": raba_list,
        "key_data": key_data,
        "metadata": metadata,
        "requirements": requirements,
        "results_map": existing_results,
        "analysis_scope": analysis_scope,
        "total_analyzed": len(scoped_requirements),
        "total_available": len(requirements),
        "latest_context_rows": hybrid_context.get("rows", []),
        "analysis_summary": analysis_summary,
        "analysis_history": analysis_history,
        "updated_at": datetime.utcnow().isoformat(),
    }
    _update_session(session_id, session_update)

    response_payload = {
        "session_id": session_id,
        "zahteve": requirements,
        "results_map": existing_results,
        "analysis_scope": analysis_scope,
        "total_analyzed": len(scoped_requirements),
        "total_available": len(requirements),
        "non_compliant_ids": non_compliant_ids,
        "requirement_revisions": session.get("requirement_revisions", {}),
        "vector_rows": hybrid_context.get("rows", []),
        "analysis_summary": analysis_summary,
        "analysis_history": analysis_history,
    }
    return JSONResponse(response_payload)


@frontend_router.post("/non-compliant/{session_id}/{requirement_id}/upload")
async def upload_requirement_revision(
    session_id: str,
    requirement_id: str,
    files: List[UploadFile] = File(...),
    note: str = Form(""),
) -> JSONResponse:
    session = _ensure_session(session_id)
    if not files:
        raise HTTPException(status_code=400, detail="Ni priloženih datotek.")

    stored_files = []
    for upload in files:
        stored_files.append((upload.filename, _read_upload(upload), upload.content_type or "application/pdf"))

    filenames, file_paths, mime_types = save_revision_files(session_id, stored_files, requirement_id=requirement_id)
    timestamp = datetime.utcnow().isoformat()

    record = {
        "filenames": filenames,
        "file_paths": file_paths,
        "mime_types": mime_types,
        "note": note,
        "uploaded_at": timestamp,
    }

    _append_revision(session, requirement_id, record)
    _update_session(session_id, {"requirement_revisions": session["requirement_revisions"], "updated_at": timestamp})

    db_manager = get_db_manager()
    if db_manager:
        try:
            db_manager.record_revision(
                session_id=session_id,
                filenames=filenames,
                file_paths=file_paths,
                requirement_id=requirement_id,
                note=note,
                mime_types=mime_types,
                uploaded_at_override=timestamp,
            )
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            LOGGER.warning("Zapis popravka v bazo ni uspel: %s", exc)

    return JSONResponse(
        {
            "message": "Popravek je shranjen.",
            "requirement_revisions": session["requirement_revisions"],
        }
    )


@frontend_router.post("/upload-revision")
async def upload_revision(
    session_id: str = Form(...),
    revision_pages: str = Form(""),
    revision_files: List[UploadFile] = File(...),
) -> JSONResponse:
    session = _ensure_session(session_id)
    if not revision_files:
        raise HTTPException(status_code=400, detail="Ni izbranih datotek za popravek.")

    stored_files = []
    image_payloads = []
    for upload in revision_files:
        file_bytes = _read_upload(upload)
        stored_files.append((upload.filename, file_bytes, upload.content_type or "application/pdf"))
        for image in convert_pdf_pages_to_images(file_bytes, revision_pages):
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            image_payloads.append(buffer.getvalue())
            buffer.close()

    filenames, file_paths, mime_types = save_revision_files(session_id, stored_files)
    timestamp = datetime.utcnow().isoformat()

    record = {
        "filenames": filenames,
        "file_paths": file_paths,
        "mime_types": mime_types,
        "note": revision_pages,
        "uploaded_at": timestamp,
    }
    _append_revision(session, None, record)

    existing_images = session.get("image_payloads", [])
    existing_images.extend(image_payloads)

    session_update = {
        "image_payloads": existing_images,
        "requirement_revisions": session["requirement_revisions"],
        "updated_at": timestamp,
    }
    _update_session(session_id, session_update)

    db_manager = get_db_manager()
    if db_manager:
        try:
            db_manager.record_revision(
                session_id=session_id,
                filenames=filenames,
                file_paths=file_paths,
                requirement_id=None,
                note=revision_pages,
                mime_types=mime_types,
                uploaded_at_override=timestamp,
            )
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            LOGGER.warning("Zapis celotnega popravka v bazo ni uspel: %s", exc)

    return JSONResponse(
        {
            "message": "Popravek je shranjen.",
            "last_revision": record,
            "requirement_revisions": session["requirement_revisions"],
        }
    )


@frontend_router.post("/re-analyze")
async def re_analyze_non_compliant(
    session_id: str = Form(...),
    non_compliant_ids_json: str = Form(...),
    revision_files: List[UploadFile] = File(...),
) -> JSONResponse:
    """Handles re-analysis of non-compliant items based on uploaded revision files."""

    session = _ensure_session(session_id)

    try:
        non_compliant_ids = json.loads(non_compliant_ids_json)
        if not isinstance(non_compliant_ids, list):
            raise ValueError("Seznam ID-jev mora biti seznam.")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Neveljaven seznam neskladnih ID-jev: {exc}")

    if not revision_files:
        raise HTTPException(status_code=400, detail="Naložite vsaj en popravljen PDF dokument.")

    new_text_parts: List[str] = []
    new_image_payloads: List[bytes] = []

    for upload in revision_files:
        file_bytes = _read_upload(upload)
        if not file_bytes:
            continue

        text_content = parse_pdf(file_bytes)
        if text_content:
            new_text_parts.append(text_content)

        # Zaenkrat predpostavimo, da za ponovno analizo besedilo zadošča.
        # Po potrebi lahko dodamo pretvorbo v slike in jih shranimo v new_image_payloads.

    updated_project_text = session.get("project_text", "")
    if new_text_parts:
        updated_project_text = (
            updated_project_text + "\n\n--- DODATEK IZ POPRAVKA ---\n\n" + "\n\n".join(new_text_parts)
        )

    all_requirements = session.get("requirements", [])
    if not all_requirements:
        raise HTTPException(status_code=400, detail="V seji ni zahtev za analizo. Najprej zaženite polno analizo.")

    scoped_requirements = [req for req in all_requirements if req.get("id") in non_compliant_ids]
    if not scoped_requirements:
        raise HTTPException(status_code=400, detail="Ni najdenih neskladnih zahtev za ponovno analizo.")

    db_manager = get_db_manager()
    hybrid_context = _collect_analysis_context(
        db_manager,
        session.get("key_data", {}),
        session.get("eup", []),
        session.get("namenska_raba", []),
    )

    prompt = build_prompt(
        project_text=updated_project_text,
        zahteve=scoped_requirements,
        izrazi_text=IZRAZI_TEXT,
        uredba_text=UREDBA_TEXT,
        vector_context=hybrid_context.get("context_text", ""),
    )

    existing_images = list(session.get("image_payloads", []) or [])
    existing_images.extend(new_image_payloads)
    images = _load_revision_images(existing_images)
    started_at = time.perf_counter()
    ai_response_text, llm_metadata = call_gemini(prompt, images)
    elapsed = time.perf_counter() - started_at
    new_results = parse_ai_response(ai_response_text, scoped_requirements)

    existing_results = session.get("results_map", {})
    existing_results.update(new_results)

    final_non_compliant_ids = [
        rid
        for rid, result in existing_results.items()
        if isinstance(result, dict)
        and isinstance(result.get("skladnost"), str)
        and "nesklad" in result["skladnost"].lower()
    ]

    analysis_summary = (
        f"Ponovno analiziranih {len(scoped_requirements)} zahtev. "
        f"Preostalih neskladnih: {len(final_non_compliant_ids)}."
    )

    llm_usage = (llm_metadata or {}).get("usage", {}) if llm_metadata else {}
    llm_duration = (llm_metadata or {}).get("duration") if llm_metadata else None
    analysis_history = list(session.get("analysis_history", []))
    analysis_record = {
        "timestamp": datetime.utcnow().isoformat(),
        "session_id": session_id,
        "scope": "re-analysis",
        "total_analyzed": len(scoped_requirements),
        "total_available": len(all_requirements),
        "non_compliant": len(final_non_compliant_ids),
        "prompt_tokens": llm_usage.get("prompt_tokens"),
        "response_tokens": llm_usage.get("candidates_tokens"),
        "total_tokens": llm_usage.get("total_tokens"),
        "llm_duration": llm_duration,
        "elapsed_seconds": round(elapsed, 3),
        "vector_rows": len(hybrid_context.get("rows", [])),
        "model": (llm_metadata or {}).get("model"),
        "analysis_summary": analysis_summary,
    }
    analysis_history.append(analysis_record)

    LOGGER.info(
        "Ponovna analiza zaključena | session=%s zahteve=%s neskladne=%s tokens=%s trajanje=%.3fs llm=%.3fs context=%s",
        session_id,
        len(scoped_requirements),
        len(final_non_compliant_ids),
        llm_usage.get("total_tokens") or "—",
        elapsed,
        llm_duration if llm_duration is not None else 0.0,
        len(hybrid_context.get("rows", [])),
    )

    session_update = {
        "project_text": updated_project_text,
        "results_map": existing_results,
        "image_payloads": existing_images,
        "analysis_summary": analysis_summary,
        "analysis_history": analysis_history,
        "latest_context_rows": hybrid_context.get("rows", []),
        "updated_at": datetime.utcnow().isoformat(),
    }
    _update_session(session_id, session_update)

    return JSONResponse(
        {
            "message": "Ponovna analiza je zaključena.",
            "updated_results": new_results,
            "non_compliant_ids": final_non_compliant_ids,
            "analysis_summary": analysis_summary,
            "analysis_history": analysis_history,
        }
    )


@frontend_router.post("/confirm-report")
async def confirm_report(payload: ConfirmReportPayload) -> JSONResponse:
    session = _ensure_session(payload.session_id)
    if not session.get("requirements"):
        raise HTTPException(status_code=400, detail="Najprej izvedite analizo.")

    excluded_ids = set(payload.excluded_ids or [])
    filtered_requirements = [req for req in session["requirements"] if req["id"] not in excluded_ids]
    filtered_results = {
        rid: result
        for rid, result in session.get("results_map", {}).items()
        if rid not in excluded_ids
    }

    metadata = session.get("metadata", {})
    project_name = metadata.get("ime_projekta") or infer_project_name(session.get("saved_state", {}) or {})

    filename = f"{payload.session_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.docx"
    output_path = REPORTS_DIR / filename
    generate_word_report(filtered_requirements, filtered_results, metadata, str(output_path))

    session_update = {
        "excluded_ids": list(excluded_ids),
        "last_report_path": str(output_path),
        "updated_at": datetime.utcnow().isoformat(),
    }
    _update_session(payload.session_id, session_update)

    state_store.LAST_DOCX_PATH = str(output_path)
    state_store.LAST_XLSX_PATH = None  # rezervirano za prihodnjo nadgradnjo
    state_store.LATEST_REPORT_CACHE[payload.session_id] = {
        "docx_path": state_store.LAST_DOCX_PATH,
        "metadata": metadata,
        "key_data": session.get("key_data", {}),
        "analysis_scope": session.get("analysis_scope"),
        "total_analyzed": session.get("total_analyzed"),
        "total_available": session.get("total_available"),
        "excluded_ids": list(excluded_ids),
    }

    db_manager = get_db_manager()
    if db_manager:
        try:
            db_manager.record_report(
                session_id=payload.session_id,
                project_name=project_name,
                summary=session.get("analysis_summary"),
                metadata=metadata,
                key_data=session.get("key_data", {}),
                excluded_ids=excluded_ids,
                analysis_scope=session.get("analysis_scope"),
                total_analyzed=session.get("total_analyzed"),
                total_available=session.get("total_available"),
                docx_path=str(output_path.relative_to(Path.cwd())) if output_path.exists() else str(output_path),
                xlsx_path=None,
            )
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            LOGGER.warning("Zapis poročila v bazo ni uspel: %s", exc)

    return JSONResponse({"message": "Poročilo je pripravljeno za prenos."})


@frontend_router.get("/download")
def download_report() -> FileResponse:
    session_paths = [
        item.get("docx_path") for item in state_store.LATEST_REPORT_CACHE.values() if item.get("docx_path")
    ]
    latest_path = session_paths[-1] if session_paths else state_store.LAST_DOCX_PATH
    if not latest_path:
        raise HTTPException(status_code=404, detail="Poročilo ni pripravljeno.")
    path = Path(latest_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Poročila ni mogoče najti na strežniku.")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=path.name)


@frontend_router.post("/save-session")
async def save_session(payload: SaveSessionPayload) -> JSONResponse:
    session = _ensure_session(payload.session_id)
    timestamp = datetime.utcnow().isoformat()
    session["saved_state"] = payload.data
    session["updated_at"] = timestamp
    _store_session(payload.session_id, session)

    IN_MEMORY_SAVED_SESSIONS[payload.session_id] = {
        "session_id": payload.session_id,
        "project_name": payload.project_name or infer_project_name(payload.data),
        "summary": payload.summary,
        "data": payload.data,
        "updated_at": timestamp,
    }

    db_manager = get_db_manager()
    if db_manager:
        try:
            db_manager.save_session(
                session_id=payload.session_id,
                project_name=payload.project_name or infer_project_name(payload.data),
                summary=payload.summary,
                data=payload.data,
                updated_at_override=timestamp,
            )
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            LOGGER.warning("Shranjevanje v bazo ni uspelo: %s", exc)

    return JSONResponse({"status": "ok"})


@frontend_router.get("/saved-sessions")
async def list_saved_sessions() -> JSONResponse:
    db_manager = get_db_manager()
    if db_manager:
        try:
            sessions = db_manager.fetch_sessions()
            return JSONResponse({"sessions": sessions})
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            LOGGER.warning("Branje iz baze ni uspelo: %s", exc)

    sessions = [
        {
            "session_id": sid,
            "project_name": data.get("project_name"),
            "summary": data.get("summary"),
            "updated_at": data.get("updated_at"),
        }
        for sid, data in sorted(
            IN_MEMORY_SAVED_SESSIONS.items(), key=lambda item: item[1].get("updated_at", ""), reverse=True
        )
    ]
    return JSONResponse({"sessions": sessions})


@frontend_router.get("/saved-sessions/{session_id}")
async def load_saved_session(session_id: str) -> JSONResponse:
    db_manager = get_db_manager()
    if db_manager:
        try:
            record = db_manager.fetch_session(session_id)
            if record:
                return JSONResponse(record)
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            LOGGER.warning("Branje shranjene seje ni uspelo: %s", exc)

    record = IN_MEMORY_SAVED_SESSIONS.get(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Seja ni bila najdena.")
    return JSONResponse(record)


@frontend_router.delete("/saved-sessions/{session_id}")
async def delete_saved_session(session_id: str) -> JSONResponse:
    IN_MEMORY_SAVED_SESSIONS.pop(session_id, None)

    db_manager = get_db_manager()
    if db_manager:
        try:
            db_manager.delete_session(session_id)
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            LOGGER.warning("Brisanje seje v bazi ni uspelo: %s", exc)

    return JSONResponse({"status": "deleted"})


# ---------------------------------------------------------------------------
# Hibridni /ask endpoint (ohranjen zaradi združljivosti)
# ---------------------------------------------------------------------------


legacy_router = APIRouter()

_build_prompt = None
_call_llm = None
try:  # pragma: no cover - dinamično zaznavanje AI adapterja
    import ai  # type: ignore

    for name in ("build_prompt", "make_prompt", "compose_prompt"):
        if hasattr(ai, name):
            _build_prompt = getattr(ai, name)
            LOGGER.info("routes: našel build_prompt v ai.py: %s()", name)
            break
    for name in ("call_llm", "ask_llm", "generate", "infer"):
        if hasattr(ai, name):
            _call_llm = getattr(ai, name)
            LOGGER.info("routes: našel LLM klic v ai.py: %s()", name)
            break
except Exception as exc:  # pragma: no cover - informativno
    LOGGER.debug("routes: ai adapter ni na voljo (%s)", exc)


def prepare_prompt_parts(
    *,
    question: str,
    key_data: Dict[str, Any],
    eup: Optional[str],
    namenska_raba: Optional[str],
    db_manager: Optional[DatabaseManager],
) -> Tuple[str, Dict[str, Any]]:
    """
    Pripravi:
      - prompt_text: končni prompt (če obstaja ai.build_prompt); sicer minimalni prompt.
      - debug_payload: vsebina za log ali UI.
    """
    # 1) Pridobi kontekst iz hibridnega iskanja
    if not db_manager:
        context_text, rows = "", []
    else:
        context_text, rows = get_vector_context(
            db_manager=db_manager,
            key_data=key_data or {},
            eup=eup,
            namenska_raba=namenska_raba,
            k=12,
            embed_fn=None,
        )

    # 2) Zgradi prompt (če imaš funkcijo v ai.py), drugače minimalni fallback
    prompt_text = (
        "NAVODILA: Odgovori natančno in citiraj samo vire v razdelku 'Relevantna pravila (citati)'.\n\n"
        + context_text
        + "\n\n"
        + f"VPRAŠANJE: {question}"
    )

    if _build_prompt:
        try:
            prompt_text = _build_prompt(  # type: ignore[misc]
                question=question,
                vector_context=context_text,
                extra={"key_data": key_data, "eup": eup, "namenska_raba": namensak_raba},
            )
        except Exception as exc:
            LOGGER.warning("build_prompt padel, uporabim fallback: %s", exc)
            # prompt_text ostane nastavljen na privzeto obliko zgoraj

    debug_payload = {
        "vector_context_preview": context_text,
        "rows": rows,
        "key_data": key_data,
        "eup": eup,
        "namenska_raba": namenska_raba,
    }
    return prompt_text, debug_payload


class AskIn(BaseModel):
    question: str
    key_data: Dict[str, Any] = {}
    eup: Optional[str] = None
    namenska_raba: Optional[str] = None


class AskOut(BaseModel):
    answer: str
    debug: Dict[str, Any]


@legacy_router.post("/ask", response_model=AskOut)
def ask_endpoint(payload: AskIn, db_manager: Optional[DatabaseManager] = Depends(get_db_manager)) -> AskOut:
    prompt_text, debug_payload = prepare_prompt_parts(
        question=payload.question,
        key_data=payload.key_data,
        eup=payload.eup,
        namenska_raba=payload.namenska_raba,
        db_manager=db_manager,
    )

    if _call_llm:
        try:
            answer = _call_llm(prompt_text)  # type: ignore[misc]
            return AskOut(answer=answer, debug=debug_payload)
        except Exception as exc:  # pragma: no cover - varnostni mehanizem
            LOGGER.warning("LLM klic ni uspel: %s", exc)

    return AskOut(
        answer="(DEBUG fallback) LLM klic ni konfiguriran. Tukaj je prompt, ki bi ga poslal:\n\n" + prompt_text,
        debug=debug_payload,
    )


app = FastAPI(title="Mnenja – Poročila o skladnosti")
app.include_router(frontend_router)
app.include_router(legacy_router)


__all__ = ["app"]
