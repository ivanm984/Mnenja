from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from fastapi import File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import FastAPI

from . import state
from .ai import (
    call_gemini,
    call_gemini_for_details,
    call_gemini_for_key_data,
    call_gemini_for_metadata,
    parse_ai_response,
)
from .database import DatabaseManager, compute_session_summary
from .files import save_revision_files
from .forms import generate_priloga_10a
from .frontend import build_homepage
from .knowledge_base import IZRAZI_TEXT, UREDBA_TEXT, build_requirements_from_db
from .parsers import convert_pdf_pages_to_images, parse_pdf
from .prompts import build_prompt
from .reporting import generate_word_report
from .schemas import ConfirmReportPayload, SaveSessionPayload
from .utils import infer_project_name
from .vector_search import search_vector_knowledge


db_manager = DatabaseManager()
db_manager.init_db()

app = FastAPI(title="Avtomatski API za Skladnost", version="21.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def frontend() -> str:
    return build_homepage()


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/save-session")
async def save_session(payload: SaveSessionPayload):
    session_id = payload.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="Manjka veljaven ID seje.")
    if not isinstance(payload.data, dict):
        raise HTTPException(status_code=400, detail="Podatki analize morajo biti v obliki JSON objekta.")

    data = payload.data
    project_name = payload.project_name or infer_project_name(data)
    summary = payload.summary or compute_session_summary(data)

    try:
        db_manager.upsert_session(session_id, project_name, summary, data)
    except Exception as exc:  # pragma: no cover - depends on DB backend
        raise HTTPException(status_code=500, detail=f"Shranjevanje analize ni uspelo: {exc}") from exc

    return {
        "message": "Analiza je shranjena.",
        "session_id": session_id,
        "project_name": project_name,
        "summary": summary,
    }


@app.get("/saved-sessions")
async def list_saved_sessions() -> Dict[str, List[Dict[str, Any]]]:
    rows = db_manager.fetch_sessions()
    sessions = [
        {
            "session_id": row["session_id"],
            "project_name": row.get("project_name") or "Neimenovan projekt",
            "summary": row.get("summary") or "",
            "updated_at": row.get("updated_at"),
        }
        for row in rows
    ]
    return {"sessions": sessions}


@app.get("/saved-sessions/{session_id}")
async def get_saved_session(session_id: str):
    record = db_manager.fetch_session(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Shranjena analiza ne obstaja.")
    revisions = db_manager.fetch_revisions(session_id)
    record["revisions"] = revisions
    return record


@app.delete("/saved-sessions/{session_id}")
async def remove_saved_session(session_id: str):
    record = db_manager.fetch_session(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Shranjena analiza ne obstaja.")
    db_manager.delete_session(session_id)
    return {"message": "Shranjena analiza je izbrisana.", "session_id": session_id}


@app.post("/extract-data")
async def extract_data(
    pdf_files: List[UploadFile] = File(...),
    files_meta_json: Optional[str] = Form(None),
):
    session_id = str(datetime.now().timestamp())
    if not pdf_files:
        raise HTTPException(status_code=400, detail="Dodajte vsaj eno PDF datoteko za analizo.")

    page_overrides: Dict[str, str] = {}
    if files_meta_json:
        try:
            parsed = json.loads(files_meta_json)
            if isinstance(parsed, list):
                for entry in parsed:
                    if isinstance(entry, dict):
                        name = entry.get("name")
                        pages = entry.get("pages")
                        if name and isinstance(pages, str) and pages.strip():
                            page_overrides[name] = pages.strip()
            else:
                raise ValueError
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Neveljavni podatki o straneh za grafični pregled.") from exc

    combined_text_parts: List[str] = []
    all_images: List[Any] = []
    files_manifest: List[Dict[str, Any]] = []

    for index, upload in enumerate(pdf_files):
        pdf_bytes = await upload.read()
        if not pdf_bytes:
            continue
        file_label = upload.filename or f"Dokument_{index + 1}.pdf"
        text = parse_pdf(pdf_bytes)
        if text:
            combined_text_parts.append(f"=== VIR: {file_label} ===\n{text}")
        page_hint = page_overrides.get(upload.filename) or page_overrides.get(file_label)
        if page_hint:
            images_for_file = convert_pdf_pages_to_images(pdf_bytes, page_hint)
            if images_for_file:
                all_images.extend(images_for_file)
        files_manifest.append(
            {
                "filename": file_label,
                "pages": page_hint or "",
                "size": len(pdf_bytes),
            }
        )

    if not combined_text_parts:
        raise HTTPException(status_code=400, detail="Iz naloženih datotek ni bilo mogoče prebrati besedila.")

    project_text = "\n\n".join(combined_text_parts)
    images = all_images
    ai_details = call_gemini_for_details(project_text, images)
    metadata = call_gemini_for_metadata(project_text)
    key_data = call_gemini_for_key_data(project_text, images)

    state.TEMP_STORAGE[session_id] = {
        "project_text": project_text,
        "images": images,
        "metadata": metadata,
        "ai_details": ai_details,
        "key_data": key_data,
        "source_files": files_manifest,
        "revision_history": [],
    }

    response_data = {
        "session_id": session_id,
        "eup": ai_details.get("eup", []),
        "namenska_raba": ai_details.get("namenska_raba", []),
        **metadata,
        **key_data,
        "uploaded_files": files_manifest,
    }
    return response_data


@app.post("/upload-revision")
async def upload_revision(
    session_id: str = Form(...),
    revision_files: List[UploadFile] = File(...),
    revision_pages: Optional[str] = Form(None),
):
    if session_id not in state.TEMP_STORAGE:
        raise HTTPException(status_code=404, detail="Seja ni aktivna ali je potekla. Ponovite prvi korak nalaganja dokumentacije.")

    if not revision_files:
        raise HTTPException(status_code=400, detail="Dodajte popravljeno projektno dokumentacijo.")

    combined_text_parts: List[str] = []
    combined_images: List[Any] = []
    revision_names: List[str] = []
    stored_files: List[tuple] = []

    for index, revision_file in enumerate(revision_files):
        pdf_bytes = await revision_file.read()
        if not pdf_bytes:
            continue
        file_label = revision_file.filename or f"popravek_{index + 1}.pdf"
        revision_names.append(file_label)
        stored_files.append((file_label, pdf_bytes, revision_file.content_type or "application/pdf"))
        text = parse_pdf(pdf_bytes)
        if text:
            combined_text_parts.append(f"=== POPRAVEK: {file_label} ===\n{text}")
        if revision_pages and revision_pages.strip():
            new_images = convert_pdf_pages_to_images(pdf_bytes, revision_pages)
            if new_images:
                combined_images.extend(new_images)

    if not combined_text_parts and not combined_images:
        raise HTTPException(status_code=400, detail="Popravljena dokumentacija ne vsebuje uporabnih podatkov za analizo.")

    filenames, file_paths, mime_types = save_revision_files(session_id, stored_files, None)
    record_info = db_manager.record_revision(session_id, filenames, file_paths, mime_types=mime_types)

    data = state.TEMP_STORAGE.get(session_id, {})
    if combined_text_parts:
        data["project_text"] = "\n\n".join(combined_text_parts)
    if combined_images:
        data["images"] = combined_images
    if revision_names:
        data["source_files"] = [{"filename": name, "pages": revision_pages or ""} for name in revision_names]

    revision_history = data.get("revision_history", [])
    history_entry = {
        "filenames": revision_names,
        "uploaded_at": record_info["uploaded_at"],
    }
    revision_history.append(history_entry)
    data["revision_history"] = revision_history
    state.TEMP_STORAGE[session_id] = data

    return {
        "status": "success",
        "message": "Popravljena dokumentacija je naložena. Izberite neskladne zahteve in zaženite ponovno analizo.",
        "revision_count": len(revision_history),
        "last_revision": history_entry,
    }


@app.post("/non-compliant/{session_id}/{requirement_id}/upload")
async def upload_non_compliant_revision(
    session_id: str,
    requirement_id: str,
    files: List[UploadFile] = File(...),
    note: Optional[str] = Form(None),
):
    if not files:
        raise HTTPException(status_code=400, detail="Dodajte vsaj eno datoteko.")

    stored_files: List[tuple] = []
    for upload in files:
        content = await upload.read()
        if not content:
            continue
        stored_files.append((upload.filename or "priloga.pdf", content, upload.content_type or "application/pdf"))
    if not stored_files:
        raise HTTPException(status_code=400, detail="Naložene datoteke so prazne.")

    filenames, file_paths, mime_types = save_revision_files(session_id, stored_files, requirement_id)
    record_info = db_manager.record_revision(
        session_id,
        filenames,
        file_paths,
        requirement_id=requirement_id,
        note=note,
        mime_types=mime_types,
    )

    if session_id in state.TEMP_STORAGE:
        history = state.TEMP_STORAGE[session_id].setdefault("requirement_revisions", {})
        history.setdefault(requirement_id, []).append(
            {
                "filenames": filenames,
                "uploaded_at": record_info["uploaded_at"],
                "note": note or "",
            }
        )

    return {
        "status": "success",
        "requirement_id": requirement_id,
        "uploaded": filenames,
        "uploaded_at": record_info["uploaded_at"],
    }


@app.get("/revisions/{session_id}")
async def list_revisions(session_id: str):
    revisions = db_manager.fetch_revisions(session_id)
    return {"revisions": revisions}


@app.post("/analyze-report")
async def analyze_report(
    session_id: str = Form(...),
    final_eup_list: List[str] = Form(None),
    final_raba_list: List[str] = Form(None),
    glavni_objekt: str = Form("Ni podatka v dokumentaciji"),
    vrsta_gradnje: str = Form("Ni podatka v dokumentaciji"),
    klasifikacija_cc_si: str = Form("Ni podatka v dokumentaciji"),
    nezahtevni_objekti: str = Form("Ni podatka v dokumentaciji"),
    enostavni_objekti: str = Form("Ni podatka v dokumentaciji"),
    vzdrzevalna_dela: str = Form("Ni podatka v dokumentaciji"),
    parcela_objekta: str = Form("Ni podatka v dokumentaciji"),
    stevilke_parcel_ko: str = Form("Ni podatka v dokumentaciji"),
    velikost_parcel: str = Form("Ni podatka v dokumentaciji"),
    velikost_obstojecega_objekta: str = Form("Ni podatka v dokumentaciji"),
    tlorisne_dimenzije: str = Form("Ni podatka v dokumentaciji"),
    gabariti_etaznost: str = Form("Ni podatka v dokumentaciji"),
    faktor_zazidanosti_fz: str = Form("Ni podatka v dokumentaciji"),
    faktor_izrabe_fi: str = Form("Ni podatka v dokumentaciji"),
    zelene_povrsine: str = Form("Ni podatka v dokumentaciji"),
    naklon_strehe: str = Form("Ni podatka v dokumentaciji"),
    kritina_barva: str = Form("Ni podatka v dokumentaciji"),
    materiali_gradnje: str = Form("Ni podatka v dokumentaciji"),
    smer_slemena: str = Form("Ni podatka v dokumentaciji"),
    visinske_kote: str = Form("Ni podatka v dokumentaciji"),
    odmiki_parcel: str = Form("Ni podatka v dokumentaciji"),
    komunalni_prikljucki: str = Form("Ni podatka v dokumentaciji"),
    selected_ids_json: Optional[str] = Form(None),
    existing_results_json: Optional[str] = Form(None),
):
    if session_id not in state.TEMP_STORAGE:
        raise HTTPException(status_code=404, detail="Seja je potekla ali podatki niso bili ekstrahirani. Prosim, ponovite Korak 1.")

    data = state.TEMP_STORAGE.get(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Podatki seje niso na voljo. Ponovite Korak 1.")

    selected_ids: List[str] = []
    if selected_ids_json:
        try:
            parsed_ids = json.loads(selected_ids_json)
            if not isinstance(parsed_ids, list):
                raise ValueError
            selected_ids = [str(item).strip() for item in parsed_ids if str(item).strip()]
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Neveljaven seznam izbranih zahtev za ponovno preverjanje.") from exc

    existing_results_map: Dict[str, Dict[str, Any]] = {}
    if existing_results_json:
        try:
            parsed_results = json.loads(existing_results_json)
            if isinstance(parsed_results, dict):
                existing_results_map = {str(k): v for k, v in parsed_results.items() if isinstance(v, dict)}
            else:
                raise ValueError
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Neveljaven JSON rezultatov prejšnje analize.") from exc

    eup_from_form = [e.strip() for e in final_eup_list if e and e.strip()] if final_eup_list is not None else []
    raba_from_form = [r.strip().upper() for r in final_raba_list if r and r.strip()] if final_raba_list is not None else []
    final_eup_list_cleaned = list(dict.fromkeys(eup_from_form))
    final_raba_list_cleaned = list(dict.fromkeys(raba_from_form))

    if not final_raba_list_cleaned:
        raise HTTPException(status_code=404, detail="Namenska raba za analizo manjka. Prosim, vnesite jo ročno.")

    zahteve = build_requirements_from_db(final_eup_list_cleaned, final_raba_list_cleaned, data["project_text"])
    vse_id = {z["id"] for z in zahteve}
    selected_id_set = set(selected_ids)
    if selected_id_set:
        invalid_ids = selected_id_set - vse_id
        if invalid_ids:
            raise HTTPException(status_code=400, detail=f"Izbrane zahteve ne obstajajo: {', '.join(sorted(invalid_ids))}.")
        zahteve_za_analizo = [z for z in zahteve if z["id"] in selected_id_set]
    else:
        zahteve_za_analizo = list(zahteve)

    if not zahteve_za_analizo:
        raise HTTPException(status_code=400, detail="Ni izbranih zahtev za ponovno preverjanje.")

    final_key_data = {
        "glavni_objekt": glavni_objekt,
        "vrsta_gradnje": vrsta_gradnje,
        "klasifikacija_cc_si": klasifikacija_cc_si,
        "nezahtevni_objekti": nezahtevni_objekti,
        "enostavni_objekti": enostavni_objekti,
        "vzdrzevalna_dela": vzdrzevalna_dela,
        "parcela_objekta": parcela_objekta,
        "stevilke_parcel_ko": stevilke_parcel_ko,
        "velikost_parcel": velikost_parcel,
        "velikost_obstojecega_objekta": velikost_obstojecega_objekta,
        "tlorisne_dimenzije": tlorisne_dimenzije,
        "gabariti_etaznost": gabariti_etaznost,
        "faktor_zazidanosti_fz": faktor_zazidanosti_fz,
        "faktor_izrabe_fi": faktor_izrabe_fi,
        "zelene_povrsine": zelene_povrsine,
        "naklon_strehe": naklon_strehe,
        "kritina_barva": kritina_barva,
        "materiali_gradnje": materiali_gradnje,
        "smer_slemena": smer_slemena,
        "visinske_kote": visinske_kote,
        "odmiki_parcel": odmiki_parcel,
        "komunalni_prikljucki": komunalni_prikljucki,
    }

    data["final_key_data"] = final_key_data

    metadata_formatted = "\n".join([f"- {k.replace('_', ' ').capitalize()}: {v}" for k, v in data["metadata"].items()])
    key_data_formatted = "\n".join([f"- {k.replace('_', ' ').capitalize()}: {v}" for k, v in final_key_data.items()])
    modified_project_text = f"""
        --- METAPODATKI PROJEKTA ---
        {metadata_formatted}
        --- KLJUČNI GABARITNI IN LOKACIJSKI PODATKI PROJEKTA (Ekstrahirano in POTRJENO) ---
        {key_data_formatted}
        --- DOKUMENTACIJA (Besedilo in grafike) ---
        {data['project_text']}
        """

    vector_context_text = ""
    vector_rows: List[Dict[str, Any]] = []
    if db_manager.supports_vector_search():
        vector_context_text, vector_rows = search_vector_knowledge(
            db_manager,
            modified_project_text,
            limit=12,
            eup=final_eup_list_cleaned,
            namenske_rabe=final_raba_list_cleaned,
        )
        data["vector_context"] = {
            "text": vector_context_text,
            "rows": vector_rows,
        }

    prompt = build_prompt(
        modified_project_text,
        zahteve_za_analizo,
        IZRAZI_TEXT,
        UREDBA_TEXT,
        vector_context=vector_context_text,
    )
    ai_response = call_gemini(prompt, data["images"])
    results_map = parse_ai_response(ai_response, zahteve_za_analizo)

    id_to_label: Dict[str, str] = {}
    for zahteva in zahteve:
        zid = zahteva.get("id")
        if not zid:
            continue
        clen_label = (zahteva.get("clen") or "").strip()
        naziv_label = (zahteva.get("naziv") or zahteva.get("naslov") or "").strip()
        if clen_label and naziv_label and clen_label.lower() not in naziv_label.lower():
            preferred_label = f"{clen_label} ({naziv_label})"
        else:
            preferred_label = clen_label or naziv_label or zid
        id_to_label[zid] = preferred_label

    replacement_pattern = None
    if id_to_label:
        replacement_pattern = re.compile(r"\\b(" + "|".join(map(re.escape, id_to_label.keys())) + r")\\b")

    def replace_requirement_ids(value: Any) -> Any:
        if not replacement_pattern or not isinstance(value, str) or not value:
            return value
        return replacement_pattern.sub(lambda match: id_to_label.get(match.group(0), match.group(0)), value)

    def normalize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(entry, dict):
            return entry
        for field in ("obrazlozitev", "predlagani_ukrep", "ugotovitve_ai", "evidence"):
            if field in entry:
                entry[field] = replace_requirement_ids(entry[field])
        return entry

    for entry in results_map.values():
        normalize_entry(entry)

    combined_results_map = {k: v for k, v in existing_results_map.items()}
    combined_results_map.update(results_map)

    for entry in combined_results_map.values():
        normalize_entry(entry)

    for z in zahteve:
        if z["id"] not in combined_results_map:
            combined_results_map[z["id"]] = {
                "id": z["id"],
                "obrazlozitev": "Zahteva ni bila analizirana v tem koraku.",
                "evidence": "—",
                "skladnost": "Neznano",
                "predlagani_ukrep": "Ponovna analiza je potrebna.",
            }

    data["results_map"] = combined_results_map
    data["zahteve"] = zahteve
    data["metadata"].update({"zadnja_posodobitev": datetime.utcnow().isoformat()})
    state.TEMP_STORAGE[session_id] = data

    analysis_scope = "partial" if selected_id_set and len(selected_id_set) < len(zahteve) else "full"
    zahteve_summary = [
        {
            "id": z["id"],
            "naslov": z["naslov"],
            "kategorija": z.get("kategorija", "Ostalo"),
            "clen": z.get("clen", ""),
            "naziv": z.get("naziv", z.get("naslov")),
        }
        for z in zahteve
    ]

    non_compliant_ids = [
        item_id
        for item_id, result in combined_results_map.items()
        if isinstance(result, dict) and "nesklad" in (result.get("skladnost") or "").lower()
    ]

    revision_records = db_manager.fetch_revisions(session_id)
    requirement_revisions = {}
    for entry in revision_records:
        rid = entry.get("requirement_id")
        if not rid:
            continue
        requirement_revisions.setdefault(rid, []).append(entry)
    data["requirement_revisions"] = requirement_revisions

    state.LATEST_REPORT_CACHE[session_id] = {
        "zahteve": zahteve,
        "results_map": combined_results_map,
        "metadata": data["metadata"],
        "analysis_scope": analysis_scope,
        "total_analyzed": len(zahteve_za_analizo),
        "total_available": len(zahteve),
        "requirement_revisions": requirement_revisions,
        "final_key_data": final_key_data,
        "source_files": data.get("source_files", []),
        "vector_context": vector_context_text,
        "vector_rows": vector_rows,
    }

    return {
        "status": "success",
        "analysis_scope": analysis_scope,
        "total_analyzed": len(zahteve_za_analizo),
        "total_available": len(zahteve),
        "results_map": combined_results_map,
        "zahteve": zahteve_summary,
        "non_compliant_ids": non_compliant_ids,
        "needs_confirmation": True,
        "revision_history": data.get("revision_history", []),
        "requirement_revisions": requirement_revisions,
    }


@app.post("/confirm-report")
async def confirm_report(payload: ConfirmReportPayload):
    cache = state.LATEST_REPORT_CACHE.get(payload.session_id)
    if not cache:
        raise HTTPException(status_code=404, detail="Analiza za generiranje poročila ni na voljo.")

    excluded_ids = {item for item in (payload.excluded_ids or []) if isinstance(item, str)}
    filtered_zahteve = [
        zahteva for zahteva in cache["zahteve"]
        if zahteva.get("id") not in excluded_ids
    ]
    filtered_results_map = {
        key: value for key, value in cache["results_map"].items()
        if key not in excluded_ids
    }

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    docx_output = reports_dir / f"Porocilo_Skladnosti_{timestamp}.docx"
    metadata_snapshot = dict(cache["metadata"])
    absolute_docx_path = generate_word_report(filtered_zahteve, filtered_results_map, metadata_snapshot, str(docx_output))
    state.LAST_DOCX_PATH = absolute_docx_path

    priloga_output = reports_dir / f"Priloga10A_{timestamp}.xlsx"
    key_data_snapshot = dict(cache.get("final_key_data", {}))
    source_files = list(cache.get("source_files", []))
    absolute_xlsx_path = generate_priloga_10a(
        filtered_zahteve,
        filtered_results_map,
        metadata_snapshot,
        key_data_snapshot,
        source_files,
        str(priloga_output),
    )
    state.LAST_XLSX_PATH = absolute_xlsx_path

    state.LATEST_REPORT_CACHE[payload.session_id]["docx_path"] = absolute_docx_path
    state.LATEST_REPORT_CACHE[payload.session_id]["xlsx_path"] = absolute_xlsx_path
    state.LATEST_REPORT_CACHE[payload.session_id]["excluded_ids"] = list(excluded_ids)
    project_name = infer_project_name({"metadata": metadata_snapshot, "keyData": key_data_snapshot}, fallback="Neimenovan projekt")
    report_summary = compute_session_summary({"zahteve": filtered_zahteve, "resultsMap": filtered_results_map})
    try:
        report_record = db_manager.record_report(
            payload.session_id,
            project_name,
            report_summary,
            metadata_snapshot,
            key_data_snapshot,
            list(excluded_ids),
            cache.get("analysis_scope"),
            cache.get("total_analyzed"),
            cache.get("total_available"),
            str(absolute_docx_path),
            str(absolute_xlsx_path),
        )
    except Exception as exc:  # pragma: no cover - backend specific
        raise HTTPException(status_code=500, detail=f"Shranjevanje poročila ni uspelo: {exc}") from exc

    return {
        "status": "success",
        "docx_path": absolute_docx_path,
        "xlsx_path": absolute_xlsx_path,
        "report_id": report_record.get("id"),
        "created_at": report_record.get("created_at"),
    }


@app.get("/download")
async def download_report():
    if not state.LAST_DOCX_PATH or not Path(state.LAST_DOCX_PATH).exists():
        raise HTTPException(status_code=404, detail="Poročilo ni bilo ustvarjeno ali ne obstaja več.")
    return FileResponse(state.LAST_DOCX_PATH, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/download-priloga10a")
async def download_priloga10a():
    if not state.LAST_XLSX_PATH or not Path(state.LAST_XLSX_PATH).exists():
        raise HTTPException(status_code=404, detail="Excel Priloga 10A ni bila ustvarjena ali ne obstaja več.")
    return FileResponse(
        state.LAST_XLSX_PATH,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


__all__ = ["app"]
