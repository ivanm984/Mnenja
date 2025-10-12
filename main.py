# main.py
from dotenv import load_dotenv
load_dotenv()

import os
import json
import re
import io
import sqlite3
import threading
from typing import List, Optional, Dict, Any, Tuple
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from datetime import datetime

# AI - Gemini
import google.generativeai as genai

# PDF parsing
from pypdf import PdfReader
import fitz  # PyMuPDF
from PIL import Image

# DOCX
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.shared import Inches

# =============================================================================
# KONFIGURACIJA
# =============================================================================

API_KEY = os.environ.get("GEMINI_API_KEY")
@@ -145,50 +147,185 @@ def load_knowledge_base() -> Tuple[Dict, Dict, List, Dict, str, str]:

        try:
            with open("UredbaObjekti.json", "r", encoding="utf-8") as f:
                uredba_data = json.load(f)
            print("✅ Uspešno naložena datoteka: UredbaObjekti.json")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"⚠️ Napaka pri nalaganju UredbaObjekti.json: {e}.")
            uredba_data = {}

        all_eups = [item.get("enota_urejanja", "") for item in priloge.get('priloga2', {}).get('table_entries', [])]
        all_eups.extend([item.get("urejevalna_enota", "") for item in priloge.get('priloga3', {}).get('entries', [])])
        unique_eups = sorted(list(set(filter(None, all_eups))), key=len, reverse=True)

        izrazi_data = priloge.get('Izrazi', {})
        izrazi_text = "\n".join([f"- **{term['term']}**: {term['definition']}" for term in izrazi_data.get("terms", [])])

        uredba_text = format_uredba_summary(uredba_data)

        print("✅ Baza znanja uspešno naložena.")
        return opn_katalog, priloge, unique_eups, clen_data_map, izrazi_text, uredba_text
    except Exception as e:
        raise RuntimeError(f"❌ Kritična napaka pri nalaganju baze znanja: {e}")

OPN_KATALOG, PRILOGE, ALL_EUPS, CLEN_DATA_MAP, IZRAZI_TEXT, UREDBA_TEXT = load_knowledge_base()

# =============================================================================
# LOKALNA SHRANJEVALNA BAZA (SQLite)
# =============================================================================

DB_PATH = os.path.join(os.path.dirname(__file__), "local_sessions.db")
DB_LOCK = threading.Lock()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_sessions (
                    session_id TEXT PRIMARY KEY,
                    project_name TEXT,
                    summary TEXT,
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def upsert_session(session_id: str, project_name: str, summary: str, data: Dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    timestamp = datetime.utcnow().isoformat()
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO saved_sessions (session_id, project_name, summary, data_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    project_name=excluded.project_name,
                    summary=excluded.summary,
                    data_json=excluded.data_json,
                    updated_at=excluded.updated_at
                """,
                (session_id, project_name, summary, payload, timestamp),
            )
            conn.commit()
        finally:
            conn.close()


def delete_session(session_id: str) -> None:
    with DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("DELETE FROM saved_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()


def fetch_sessions() -> List[sqlite3.Row]:
    with DB_LOCK:
        conn = get_db_connection()
        try:
            cursor = conn.execute(
                "SELECT session_id, project_name, summary, updated_at FROM saved_sessions ORDER BY updated_at DESC"
            )
            return cursor.fetchall()
        finally:
            conn.close()


def fetch_session(session_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK:
        conn = get_db_connection()
        try:
            cursor = conn.execute(
                "SELECT session_id, project_name, summary, data_json, updated_at FROM saved_sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            data = json.loads(row["data_json"])
            return {
                "session_id": row["session_id"],
                "project_name": row["project_name"],
                "summary": row["summary"],
                "updated_at": row["updated_at"],
                "data": data,
            }
        finally:
            conn.close()


init_db()


def compute_session_summary(data: Dict[str, Any]) -> str:
    try:
        zahteve = data.get("zahteve") or []
        if not isinstance(zahteve, list):
            return ""
        total = len(zahteve)
        results_map = data.get("resultsMap") or {}
        if not isinstance(results_map, dict):
            results_map = {}
        non_compliant = 0
        for item in zahteve:
            if not isinstance(item, dict):
                continue
            result = results_map.get(item.get("id"), {})
            status_text = (result.get("skladnost") or "").lower()
            if "nesklad" in status_text:
                non_compliant += 1
        if total == 0:
            return "Ni zahtev" if results_map else ""
        return f"{total} zahtev, {non_compliant} neskladnih"
    except Exception:
        return ""


class SaveSessionPayload(BaseModel):
    session_id: str = Field(..., min_length=1)
    data: Dict[str, Any]
    project_name: Optional[str] = None
    summary: Optional[str] = None

# =============================================================================
# INTELIGENTNA ANALIZA IN SESTAVA ZAHTEV
# =============================================================================

def call_gemini_for_details(project_text: str, images: List[Image.Image]) -> Dict[str, Optional[List[str]]]:
    """1. AI Klic (Detektiv): Prvotno iz besedila in po potrebi še slik projekta izlušči EUP in namensko rabo."""
    print("🤖 1. Klic AI (Detektiv): Iščem EUP in namensko rabo (analiza besedila in po potrebi slik)...")
    prompt = f"""
    Analiziraj spodnje besedilo iz projektne dokumentacije. Če tega ne najdeš poglej slike. Tvoja naloga je najti dve informaciji:
    1.  **Enota Urejanja Prostora (EUP)**: To so oznake lokacij. Poišči VSE ustrezne oznake, ker lahko projekt (npr. vodovod) poteka preko več EUP. Bodi ekstremno natančen. Ne vračaj splošnih oznak, če so na voljo bolj specifične.
    2.  **Podrobnejša namenska raba**: To so kratice, npr. 'SSe', 'SK' ali 'A'. Poišči VSE, saj lahko projekt poteka preko več namenskih rab.

    Odgovori SAMO v JSON formatu, pri čemer sta vrednosti seznama (array). Primeri:
    {{ "eup": ["KE6-A6 549*", "KE6-A7 550*"], "namenska_raba": ["A", "SSe"] }}
    
    Če katerega od podatkov ne najdeš, vrni prazen seznam (array) za to polje.

    Besedilo dokumentacije: --- {project_text[:40000]} ---
    """
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        
        content_parts = [prompt]
        if images:
            content_parts.extend(images)
@@ -805,51 +942,51 @@ def frontend():
        :root {
            --bg-gradient: linear-gradient(135deg, #eef2ff 0%, #f7f9ff 45%, #ffffff 100%);
            --card-bg: #ffffff;
            --border-color: #e3e8f1;
            --primary: #2563eb;
            --primary-dark: #1d4ed8;
            --success: #22a06b;
            --danger: #dc3545;
            --text-color: #1f2937;
            --muted: #6b7280;
            --shadow: 0 20px 40px rgba(15, 23, 42, 0.12);
            --radius-lg: 18px;
            --radius-md: 12px;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-gradient);
            color: var(--text-color);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: stretch;
            align-items: stretch; /* raztegni glavno vsebino po celotni višini */
            padding: 40px 16px;
        }

        .page {
            width: min(1040px, 100%);
            display: flex;
            flex-direction: column;
            gap: 28px;
        }

        .hero {
            background: radial-gradient(circle at top left, rgba(37, 99, 235, 0.16), transparent 55%),
                        radial-gradient(circle at bottom right, rgba(34, 160, 107, 0.14), transparent 60%),
                        var(--card-bg);
            border-radius: var(--radius-lg);
            padding: 32px 36px;
            box-shadow: var(--shadow);
            display: grid;
            gap: 20px;
        }

        .hero h1 {
            font-size: clamp(2.1rem, 2vw + 1.5rem, 2.6rem);
            margin: 0;
            color: #0f172a;
@@ -891,133 +1028,203 @@ def frontend():

        .step-card span {
            display: block;
            color: var(--text-color);
            font-weight: 600;
            margin-bottom: 4px;
        }

        .step-card p {
            margin: 0;
            color: var(--muted);
            font-size: 0.92rem;
            line-height: 1.5;
        }

        .main-card {
            background: var(--card-bg);
            border-radius: var(--radius-lg);
            padding: 32px 36px;
            box-shadow: var(--shadow);
            display: flex;
            flex-direction: column;
            gap: 28px;
        }

        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            flex-wrap: wrap;
        }

        .section-header .section-title {
            margin: 0;
        }

        .section-title {
            font-size: 1.15rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #0f172a;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .section-title .step-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 34px;
            height: 34px;
            border-radius: 10px;
            background: rgba(37, 99, 235, 0.12);
            color: var(--primary);
            font-weight: 600;
        }

        .btn-inline {
            width: auto;
            padding: 12px 20px;
        }

        .upload-section {
            border: 2px dashed rgba(37, 99, 235, 0.25);
            border-radius: var(--radius-md);
            padding: 22px 24px;
            transition: all 0.25s ease;
            background: rgba(248, 250, 255, 0.8);
            display: grid;
            gap: 18px;
        }

        .upload-intro {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .selected-files {
            display: flex;
            flex-direction: column;
            gap: 14px;
        }

        .selected-files .file-item {
            background: rgba(255, 255, 255, 0.9);
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: var(--radius-md);
            padding: 14px 16px;
            box-shadow: 0 12px 24px rgba(15, 23, 42, 0.06);
        }

        .selected-files .file-head {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 12px;
            margin-bottom: 10px;
        }

        .selected-files .file-name {
            font-weight: 600;
            color: var(--text-color);
            word-break: break-word;
        }

        .selected-files .file-meta {
            color: var(--muted);
            font-size: 0.85rem;
        }

        .selected-files .file-pages label {
            font-weight: 500;
            font-size: 0.9rem;
            color: var(--muted);
        }

        .selected-files .file-pages input {
            margin-top: 6px;
        }

        .upload-section:hover,
        .upload-section.drag-active {
            border-color: var(--primary);
            background: rgba(248, 250, 255, 1);
            box-shadow: 0 16px 30px rgba(37, 99, 235, 0.12);
        }

        label {
            font-weight: 600;
            color: var(--text-color);
            display: block;
        }

        input[type=file],
        input[type=text],
        textarea {
            width: 100%;
            padding: 12px 14px;
            margin-top: 8px;
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            background: #f9fbff;
            font-size: 0.98rem;
            transition: border 0.2s ease, box-shadow 0.2s ease;
        }

        input[type=file] {
            padding: 12px;
            background: #fff;
        }

        input[type=text]:focus,
        textarea:focus {
            border-color: rgba(37, 99, 235, 0.65);
            box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.12);
            outline: none;
        }

        textarea {
            resize: vertical;
            min-height: 70px;
            line-height: 1.5;
        }

        .subtitle {
            color: var(--muted);
            margin: 4px 0 0 0;
            line-height: 1.55;
        }

        .subtitle.muted {
            color: rgba(15, 23, 42, 0.55);
        }

        .btn {
            width: 100%;
            padding: 16px;
            border-radius: var(--radius-md);
            border: none;
            font-size: 1.05rem;
            font-weight: 600;
            color: #fff;
            background: var(--primary);
            cursor: pointer;
            transition: transform 0.2s ease, box-shadow 0.2s ease, background 0.2s ease;
        }

        .btn:hover {
            background: var(--primary-dark);
            transform: translateY(-1px);
            box-shadow: 0 12px 24px rgba(37, 99, 235, 0.2);
        }

        .btn:disabled {
            background: #a6b2d7;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
@@ -1137,182 +1344,1122 @@ def frontend():
            margin-top: 6px;
            font-size: 1rem;
            color: #0f172a;
            font-weight: 700;
            word-break: break-word;
        }

        .key-summary .summary-item.empty strong {
            color: rgba(15, 23, 42, 0.55);
            font-style: italic;
            font-weight: 500;
        }

        .key-data-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 18px;
        }

        .key-data-grid .data-item label {
            font-weight: 600;
            font-size: 0.95rem;
            margin-bottom: 6px;
        }

        .results-section {
            margin-top: 28px;
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: var(--radius-lg);
            background: rgba(255, 255, 255, 0.95);
            padding: 24px 26px;
            box-shadow: 0 16px 32px rgba(15, 23, 42, 0.12);
        }

        .results-section + .revision-upload {
            margin-top: 18px;
        }

        .results-header h3 {
            margin: 0 0 6px 0;
            font-size: 1.35rem;
            color: #0f172a;
        }

        .results-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 18px;
        }

        .results-table thead {
            background: rgba(37, 99, 235, 0.08);
        }

        .results-table th,
        .results-table td {
            padding: 14px 16px;
            border-bottom: 1px solid rgba(15, 23, 42, 0.08);
            vertical-align: top;
            text-align: left;
        }

        .results-table tbody tr:hover {
            background: rgba(37, 99, 235, 0.06);
        }

        .results-table tbody tr.status-neskladno {
            background: rgba(220, 53, 69, 0.08);
        }

        .results-table tbody tr.status-neskladno:hover {
            background: rgba(220, 53, 69, 0.12);
        }

        .results-table td strong {
            display: block;
            margin-bottom: 6px;
        }

        .results-table td .category-tag {
            display: inline-flex;
            align-items: center;
            padding: 4px 8px;
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.08);
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: rgba(15, 23, 42, 0.6);
        }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 999px;
            font-weight: 600;
            font-size: 0.9rem;
        }

        .status-pill::before {
            content: '';
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }

        .status-pill.skladno {
            background: rgba(34, 160, 107, 0.15);
            color: var(--success);
        }

        .status-pill.skladno::before { background: var(--success); }

        .status-pill.neskladno {
            background: rgba(220, 53, 69, 0.18);
            color: var(--danger);
        }

        .status-pill.neskladno::before { background: var(--danger); }

        .status-pill.ni-relevantno {
            background: rgba(37, 99, 235, 0.14);
            color: var(--primary);
        }

        .status-pill.ni-relevantno::before { background: var(--primary); }

        .status-pill.neznano {
            background: rgba(15, 23, 42, 0.12);
            color: rgba(15, 23, 42, 0.8);
        }

        .status-pill.neznano::before { background: rgba(15, 23, 42, 0.7); }

        .result-note {
            color: rgba(15, 23, 42, 0.72);
            font-size: 0.92rem;
            line-height: 1.45;
        }

        .results-actions {
            margin-top: 20px;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            justify-content: flex-start;
        }

        .btn-tertiary {
            background: rgba(37, 99, 235, 0.08);
            color: var(--primary);
        }

        .btn-tertiary:hover {
            background: rgba(37, 99, 235, 0.14);
        }

        .info-banner {
            display: none;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            padding: 18px 22px;
            border-radius: var(--radius-md);
            border: 1px solid rgba(37, 99, 235, 0.18);
            background: rgba(37, 99, 235, 0.1);
            color: #0f172a;
        }

        .info-banner .banner-text {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .info-banner .banner-text span {
            color: var(--muted);
            font-size: 0.9rem;
        }

        .info-banner .banner-text .project-name {
            color: #0f172a;
            font-weight: 600;
        }

        .info-banner .banner-actions {
            display: flex;
            gap: 10px;
            flex-shrink: 0;
        }

        .saved-modal {
            position: fixed;
            inset: 0;
            background: rgba(15, 23, 42, 0.35);
            display: none;
            align-items: center;
            justify-content: center;
            padding: 24px;
            z-index: 50;
        }

        .saved-modal.active {
            display: flex;
        }

        .saved-modal .modal-card {
            background: #fff;
            border-radius: var(--radius-lg);
            padding: 24px;
            width: min(520px, 100%);
            box-shadow: 0 24px 48px rgba(15, 23, 42, 0.24);
            display: flex;
            flex-direction: column;
            gap: 18px;
        }

        .saved-modal .modal-card h4 {
            margin: 0;
            font-size: 1.2rem;
            color: #0f172a;
        }

        .saved-modal .saved-list {
            display: flex;
            flex-direction: column;
            gap: 12px;
            max-height: 320px;
            overflow-y: auto;
        }

        .saved-modal .saved-item {
            border: 1px solid rgba(37, 99, 235, 0.18);
            border-radius: var(--radius-md);
            padding: 14px 16px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            background: rgba(37, 99, 235, 0.05);
        }

        .saved-modal .saved-item strong {
            color: #0f172a;
            font-size: 1rem;
        }

        .saved-modal .saved-item span {
            color: var(--muted);
            font-size: 0.9rem;
        }

        .saved-modal .saved-item button {
            align-self: flex-start;
            margin-top: 6px;
            padding: 10px 16px;
        }

        .saved-modal .modal-actions {
            display: flex;
            justify-content: flex-end;
        }

        .revision-upload {
            border: 1px dashed rgba(37, 99, 235, 0.3);
            border-radius: var(--radius-lg);
            padding: 24px 26px;
            background: rgba(37, 99, 235, 0.05);
            display: none;
            flex-direction: column;
            gap: 16px;
        }

        .revision-upload h4 {
            margin: 0;
            font-size: 1.15rem;
            color: #0f172a;
        }

        .revision-upload .revision-fields {
            display: grid;
            gap: 8px;
        }

        .revision-upload label {
            font-weight: 600;
            color: #0f172a;
        }

        .revision-upload input[type="file"],
        .revision-upload input[type="text"] {
            padding: 10px 12px;
            border: 1px solid rgba(15, 23, 42, 0.12);
            border-radius: var(--radius-md);
            font-size: 1rem;
            background: #fff;
        }

        .revision-actions {
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }

        .revision-note {
            font-size: 0.9rem;
            color: var(--muted);
        }

        .btn-secondary {
            background: rgba(15, 23, 42, 0.08);
            color: var(--text-color);
        }

        .btn-secondary:hover {
            background: rgba(15, 23, 42, 0.14);
            color: var(--text-color);
        }

        footer {
            text-align: center;
            color: var(--muted);
            font-size: 0.9rem;
            padding-bottom: 12px;
        }

        @media (max-width: 720px) {
            body {
                padding: 24px 12px 32px;
            }

            .hero,
            .main-card {
                padding: 24px;
            }

            .manual-input-pair {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="page">
        <header class="hero">
            <div class="hero-content">
                <h1>Avtomatsko preverjanje skladnosti</h1>
                <p>Digitalizirajte preverjanje projektne dokumentacije. Sistem analizira naložene PDF dokumente, prepozna EUP in
                   namenske rabe ter pripravi ključne gabaritne podatke za končni pregled.</p>
            </div>
            <div class="hero-steps">
                <div class="step-card">
                    <strong>1</strong>
                    <span>Naloži dokumente</span>
                    <p>Dodajte projektno dokumentacijo in, če je potrebno, označite strani z grafikami.</p>
                </div>
                <div class="step-card">
                    <strong>2</strong>
                    <span>Preglej osnutek</span>
                    <p>AI predlog EUP/rab in ključnih podatkov je pripravljen za ročni popravek.</p>
                </div>
                <div class="step-card">
                    <strong>3</strong>
                    <span>Potrdi in analiziraj</span>
                    <p>Potrjeni podatki se uporabijo za podrobno poročilo skladnosti.</p>
                </div>
            </div>
        </header>

        <div id="restoreBanner" class="info-banner">
            <div class="banner-text">
                <strong>Na voljo je shranjena analiza za nadaljnjo obdelavo.</strong>
                <span id="restoreProjectName" class="project-name"></span>
                <span id="restoreTimestamp"></span>
            </div>
            <div class="banner-actions">
                <button type="button" class="btn btn-analyze" id="restoreSessionBtn">Nadaljuj z dopolnitvijo</button>
                <button type="button" class="btn btn-secondary" id="discardSessionBtn">Izbriši shranjeno</button>
            </div>
        </div>

        <main class="main-card">
            <div>
                <div class="section-title"><span class="step-badge">1</span> Priprava dokumentacije</div>
                <div class="section-header">
                    <div class="section-title"><span class="step-badge">1</span> Priprava dokumentacije</div>
                    <button type="button" class="btn btn-tertiary btn-inline" id="openSavedBtn">Odpri shranjeno analizo</button>
                </div>
                <form id="uploadForm">
                    <div class="upload-section" id="dropZone">
                        <div>
                            <label for="pdfFile">Izberi projektno dokumentacijo (PDF):</label>
                            <input type="file" id="pdfFile" accept=".pdf" required>
                        <div class="upload-intro">
                            <label for="pdfFiles">Izberi projektno dokumentacijo (PDF datoteke):</label>
                            <input type="file" id="pdfFiles" accept=".pdf" multiple required>
                            <p class="subtitle">Dodajte vse tekstualne in grafične priloge projekta. Spodaj lahko za vsako datoteko določite strani za grafični pregled.</p>
                        </div>
                        <div>
                            <label for="pages">Strani z grafikami (neobvezno, za boljšo ekstrakcijo):</label>
                            <input type="text" id="pages" placeholder="Npr: 16-25, 30, 32">
                            <p class="subtitle">Namig: Za več EUP lahko dodate več razponov ločenih z vejico.</p>
                        <div id="selectedFilesList" class="selected-files">
                            <p class="subtitle muted">Po dodajanju datotek lahko pri posamezni prilogi vnesete strani (npr. 2, 4-6), ki naj se pretvorijo v slike za vizualno analizo.</p>
                        </div>
                    </div>
                    <button type="submit" class="btn" id="submitBtn">Ekstrahiraj ključne podatke in EUP/Rabe</button>
                </form>
                <div id="status"></div>
            </div>

            <div>
                <div class="section-title"><span class="step-badge">2</span> Pregled in potrditev podatkov</div>
                <form id="analyzeForm" style="display:none;">
                    <input type="hidden" name="session_id" id="sessionId">
                    <input type="hidden" name="existing_results_json" id="existingResults">

                    <div class="input-group">
                        <div>
                            <label>A. EUP in Namenska raba</label>
                            <p class="subtitle">AI predlog lahko prilagodite. Vsaka vrstica predstavlja par EUP in pripadajočo namensko rabo.</p>
                        </div>
                        <div id="manualInputs"></div>
                        <button type="button" id="addEupRabaBtn" class="add-btn">+ Dodaj EUP/Rab (popravek)</button>
                        <p class="subtitle" style="margin-top:-8px;">Potrjene vrednosti bodo uporabljene v naslednjem koraku analize.</p>
                    </div>

                    <div class="input-group">
                        <div>
                            <label>B. Ključni gabaritni podatki</label>
                            <p class="subtitle">Preglejte in po potrebi popravite podatke, ki jih je sistem zaznal iz dokumentacije.</p>
                        </div>
                        <div class="key-data-grid" id="keyDataFields"></div>
                    </div>

                    <button type="submit" class="btn btn-analyze" id="analyzeBtn">Izvedi podrobno analizo in pripravi poročilo</button>
                </form>
            </div>
        </main>

        <footer>© YEAR_PLACEHOLDER Avtomatsko preverjanje skladnosti — razvojna različica</footer>
                <div id="resultsSection" class="results-section" style="display:none;">
                    <div class="results-header">
                        <h3>Rezultati analize</h3>
                        <p class="subtitle">Preglejte spodnji povzetek. Izberite zahteve, ki so bile prej neskladne ali jih želite ponovno preveriti po popravkih, nato zaženite ponovno analizo samo zanje.</p>
                    </div>
                    <div id="resultsTable"></div>
                    <div class="results-actions">
                        <button type="button" class="btn btn-tertiary btn-inline" id="saveProgressBtn">Shrani napredek</button>
                        <button type="button" class="btn btn-secondary btn-inline" id="resetSelectionBtn">Počisti izbor</button>
                    </div>
                </div>
                <div id="revisionSection" class="revision-upload">
                    <h4>Dodaj popravljeno projektno dokumentacijo</h4>
                    <p class="subtitle">Ko prejmete dopolnjeno dokumentacijo od projektanta, jo naložite tukaj. Sistem ohrani prejšnjo analizo in ponovno preveri samo izbrane točke.</p>
                    <div class="revision-fields">
                        <label for="revisionFile">Popravljena dokumentacija (PDF datoteke)</label>
                        <input type="file" id="revisionFile" accept="application/pdf" multiple>
                    </div>
                    <div class="revision-fields">
                        <label for="revisionPages">Strani za podrobno analizo (neobvezno)</label>
                        <input type="text" id="revisionPages" placeholder="npr. 2, 4-6" autocomplete="off">
                    </div>
                    <div class="revision-actions">
                        <button type="button" class="btn btn-secondary btn-inline" id="uploadRevisionBtn">Naloži popravek</button>
                        <button type="button" class="btn btn-analyze btn-inline" id="rerunSelectedBtn">Ponovna presoja izbranih zahtev</button>
                        <span class="revision-note" id="revisionInfo"></span>
                    </div>
                </div>
            </div>
        </main>

        <div id="savedSessionsModal" class="saved-modal" role="dialog" aria-modal="true" aria-labelledby="savedSessionsTitle">
            <div class="modal-card">
                <h4 id="savedSessionsTitle">Izberite shranjeno analizo</h4>
                <div id="savedSessionsList" class="saved-list"></div>
                <div class="modal-actions">
                    <button type="button" class="btn btn-secondary btn-inline" id="closeSavedModalBtn">Zapri</button>
                </div>
            </div>
        </div>

        <footer>© YEAR_PLACEHOLDER Avtomatsko preverjanje skladnosti — razvojna različica</footer>
    </div>
<script>
    const uploadForm = document.getElementById("uploadForm"), 
          analyzeForm = document.getElementById("analyzeForm"),
          status = document.getElementById("status"), 
          submitBtn = document.getElementById("submitBtn"), 
          manualInputs = document.getElementById("manualInputs"), 
          addEupRabaBtn = document.getElementById("addEupRabaBtn"),
          keyDataFields = document.getElementById("keyDataFields");
    const uploadForm = document.getElementById("uploadForm");
    const pdfFilesInput = document.getElementById("pdfFiles");
    const selectedFilesList = document.getElementById("selectedFilesList");
    const analyzeForm = document.getElementById("analyzeForm");
    const status = document.getElementById("status");
    const submitBtn = document.getElementById("submitBtn");
    const manualInputs = document.getElementById("manualInputs");
    const addEupRabaBtn = document.getElementById("addEupRabaBtn");
    const keyDataFields = document.getElementById("keyDataFields");
    const resultsSection = document.getElementById("resultsSection");
    const resultsTable = document.getElementById("resultsTable");
    const rerunSelectedBtn = document.getElementById("rerunSelectedBtn");
    const resetSelectionBtn = document.getElementById("resetSelectionBtn");
    const existingResultsInput = document.getElementById("existingResults");
    const analyzeBtn = document.getElementById("analyzeBtn");
    const revisionSection = document.getElementById("revisionSection");
    const revisionFileInput = document.getElementById("revisionFile");
    const revisionPagesInput = document.getElementById("revisionPages");
    const uploadRevisionBtn = document.getElementById("uploadRevisionBtn");
    const revisionInfo = document.getElementById("revisionInfo");
    const saveProgressBtn = document.getElementById("saveProgressBtn");
    const openSavedBtn = document.getElementById("openSavedBtn");
    const restoreBanner = document.getElementById("restoreBanner");
    const restoreSessionBtn = document.getElementById("restoreSessionBtn");
    const discardSessionBtn = document.getElementById("discardSessionBtn");
    const restoreTimestamp = document.getElementById("restoreTimestamp");
    const restoreProjectName = document.getElementById("restoreProjectName");
    const savedSessionsModal = document.getElementById("savedSessionsModal");
    const savedSessionsList = document.getElementById("savedSessionsList");
    const closeSavedModalBtn = document.getElementById("closeSavedModalBtn");
    const sessionIdInput = document.getElementById("sessionId");

    let currentZahteve = [];
    let currentResultsMap = {};
    const STORAGE_KEY = "mnenjaSavedState";
    let savedSessionsCache = null;
    let highlightedSavedSession = null;
    let highlightedSource = null; // "remote" ali "local"
    let fetchingSavedSessions = false;

    // KLJUČNI SLOVAR PODATKOV ZA DINAMIČNO GENERIRANJE POLJ
    const keyLabels = {
        'glavni_objekt': 'OBJEKT (opis funkcije)', 'vrsta_gradnje': 'VRSTA GRADNJE',
        'klasifikacija_cc_si': 'CC-SI klasifikacija', 'nezahtevni_objekti': 'Nezahtevni objekti v projektu',
        'enostavni_objekti': 'Enostavni objekti v projektu', 'vzdrzevalna_dela': 'Vzdrževalna dela / manjša rekonstrukcija',
        'parcela_objekta': 'Gradbena parcela (št.)', 'stevilke_parcel_ko': 'Vse parcele in k.o.',
        'velikost_parcel': 'Skupna velikost parcel', 'velikost_obstojecega_objekta': 'Velikost obstoječega objekta',
        'tlorisne_dimenzije': 'Nove tlorisne dimenzije', 'gabariti_etaznost': 'Novi gabarit/Etažnost',
        'faktor_zazidanosti_fz': 'Faktor zazidanosti (FZ)', 'faktor_izrabe_fi': 'Faktor izrabe (FI)',
        'zelene_povrsine': 'Zelene površine (FZP/m²)', 'naklon_strehe': 'Naklon strehe',
        'kritina_barva': 'Kritina/Barva', 'materiali_gradnje': 'Materiali gradnje (npr. les)',
        'smer_slemena': 'Smer slemena', 'visinske_kote': 'Višinske kote (k.p., k.s.)',
        'odmiki_parcel': 'Odmiki od parcelnih mej', 'komunalni_prikljucki': 'Komunalni priključki/Oskrba'
    };

    const summaryLabels = {
        'glavni_objekt': 'OBJEKT',
        'vrsta_gradnje': 'VRSTA GRADNJE',
        'klasifikacija_cc_si': 'CC-SI',
        'nezahtevni_objekti': 'NEZAHTEVNI OBJEKTI',
        'enostavni_objekti': 'ENOSTAVNI OBJEKTI',
        'vzdrzevalna_dela': 'VZDRŽEVALNA DELA'
    };

    const metadataKeys = ['ime_projekta', 'stevilka_projekta'];

    function loadSavedState() {
        if (typeof localStorage === 'undefined') { return null; }
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (!raw) { return null; }
            return JSON.parse(raw);
        } catch (err) {
            console.warn('Shranjene seje ni mogoče prebrati:', err);
            return null;
        }
    }

    function saveStateToLocal(state) {
        if (typeof localStorage === 'undefined' || !state) { return; }
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        } catch (err) {
            console.warn('Lokalno shranjevanje analize ni uspelo:', err);
        }
    }

    function clearLocalState() {
        if (typeof localStorage === 'undefined') { return; }
        try {
            localStorage.removeItem(STORAGE_KEY);
        } catch (err) {
            console.warn('Brisanje lokalne analize ni uspelo:', err);
        }
    }

    function formatTimestamp(value) {
        if (!value) { return ''; }
        const ts = new Date(value);
        if (Number.isNaN(ts.getTime())) { return ''; }
        return ts.toLocaleString('sl-SI');
    }

    function formatTimestampLabel(value, prefix = 'Zadnja shranitev') {
        const formatted = formatTimestamp(value);
        return formatted ? `${prefix}: ${formatted}` : '';
    }

    async function fetchSavedSessionsList(force = false) {
        if (!force && Array.isArray(savedSessionsCache)) {
            return savedSessionsCache;
        }
        if (fetchingSavedSessions) {
            return savedSessionsCache || [];
        }
        fetchingSavedSessions = true;
        try {
            const response = await fetch('/saved-sessions');
            if (!response.ok) {
                throw new Error('Ni mogoče pridobiti shranjenih analiz.');
            }
            const data = await response.json();
            const sessions = Array.isArray(data.sessions) ? data.sessions : [];
            savedSessionsCache = sessions;
            return sessions;
        } catch (error) {
            console.warn('Pridobivanje shranjenih analiz ni uspelo:', error);
            return [];
        } finally {
            fetchingSavedSessions = false;
        }
    }

    async function updateRestoreBanner(force = false) {
        if (!restoreBanner) { return; }

        let remoteHandled = false;
        try {
            const sessions = await fetchSavedSessionsList(force);
            if (sessions.length > 0) {
                const session = sessions[0];
                highlightedSavedSession = session;
                highlightedSource = 'remote';
                if (restoreProjectName) {
                    restoreProjectName.textContent = session.project_name ? `Projekt: ${session.project_name}` : '';
                }
                if (restoreTimestamp) {
                    restoreTimestamp.textContent = formatTimestampLabel(session.updated_at, 'Zadnja shranitev');
                }
                restoreBanner.style.display = 'flex';
                remoteHandled = true;
            }
        } catch (error) {
            console.warn('Posodobitev pasice ni uspela:', error);
        }

        if (remoteHandled) { return; }

        const local = loadSavedState();
        if (local && local.sessionId) {
            highlightedSavedSession = local;
            highlightedSource = 'local';
            if (restoreProjectName) {
                const name = (local.metadata && local.metadata.ime_projekta) ||
                             (local.keyData && local.keyData.ime_projekta) || '';
                restoreProjectName.textContent = name ? `Projekt: ${name}` : '';
            }
            if (restoreTimestamp) {
                const ts = local.timestamp ? new Date(local.timestamp) : null;
                if (ts && !Number.isNaN(ts.getTime())) {
                    restoreTimestamp.textContent = `Lokalno shranjeno: ${ts.toLocaleString('sl-SI')}`;
                } else {
                    restoreTimestamp.textContent = 'Lokalno shranjeno';
                }
            }
            restoreBanner.style.display = 'flex';
            return;
        }

        highlightedSavedSession = null;
        highlightedSource = null;
        if (restoreProjectName) { restoreProjectName.textContent = ''; }
        if (restoreTimestamp) { restoreTimestamp.textContent = ''; }
        restoreBanner.style.display = 'none';
    }

    function hideSavedSessionsModal() {
        if (savedSessionsModal) {
            savedSessionsModal.classList.remove('active');
        }
    }

    function showSavedSessionsModal(sessions) {
        if (!savedSessionsModal || !savedSessionsList) { return; }

        savedSessionsList.innerHTML = '';

        if (!Array.isArray(sessions) || sessions.length === 0) {
            const empty = document.createElement('p');
            empty.className = 'subtitle';
            empty.textContent = 'Ni shranjenih analiz na strežniku.';
            savedSessionsList.appendChild(empty);
        } else {
            sessions.forEach(session => {
                const item = document.createElement('div');
                item.className = 'saved-item';

                const title = document.createElement('strong');
                title.textContent = session.project_name || 'Neimenovan projekt';
                item.appendChild(title);

                if (session.summary) {
                    const summary = document.createElement('span');
                    summary.textContent = session.summary;
                    item.appendChild(summary);
                }

                const tsLabel = document.createElement('span');
                const formattedTs = formatTimestampLabel(session.updated_at, 'Zadnja shranitev');
                tsLabel.textContent = formattedTs || 'Zadnja shranitev ni znana';
                item.appendChild(tsLabel);

                const openBtn = document.createElement('button');
                openBtn.type = 'button';
                openBtn.className = 'btn btn-tertiary btn-inline';
                openBtn.textContent = 'Odpri analizo';
                openBtn.addEventListener('click', async () => {
                    openBtn.disabled = true;
                    try {
                        await loadSessionFromServer(session.session_id);
                        hideSavedSessionsModal();
                    } finally {
                        openBtn.disabled = false;
                    }
                });

                item.appendChild(openBtn);
                savedSessionsList.appendChild(item);
            });
        }

        savedSessionsModal.classList.add('active');
    }

    async function loadSessionFromServer(sessionId) {
        if (!sessionId) {
            showStatus('ID shranjene analize ni na voljo.', 'error');
            return false;
        }

        showStatus('Nalagam shranjeno analizo...', 'loading');
        try {
            const response = await fetch(`/saved-sessions/${encodeURIComponent(sessionId)}`);
            if (!response.ok) {
                let detail = 'Shranjene analize ni mogoče odpreti.';
                try {
                    const error = await response.json();
                    detail = error.detail || detail;
                } catch (_) { /* ignore */ }
                throw new Error(detail);
            }

            const record = await response.json();
            const state = record.data;
            if (!state) {
                throw new Error('Shranjena analiza ne vsebuje podatkov.');
            }

            applySavedState(state);
            saveStateToLocal(state);
            savedSessionsCache = null;
            await updateRestoreBanner(true);
            showStatus('Shranjena analiza je naložena. Nadaljujte s pregledom ali ponovno presojo.', 'success');
            return true;
        } catch (error) {
            showStatus(error.message || 'Napaka pri nalaganju shranjene analize.', 'error');
            return false;
        }
    }

    function formatFileSize(bytes) {
        if (typeof bytes !== 'number' || Number.isNaN(bytes)) { return ''; }
        if (bytes === 0) { return '0 B'; }
        const units = ['B', 'KB', 'MB', 'GB'];
        let size = bytes;
        let unitIndex = 0;
        while (size >= 1024 && unitIndex < units.length - 1) {
            size /= 1024;
            unitIndex += 1;
        }
        const precision = unitIndex === 0 ? 0 : (size >= 10 ? 1 : 2);
        return `${size.toFixed(precision)} ${units[unitIndex]}`;
    }

    function renderSelectedFilesList() {
        if (!selectedFilesList) { return; }

        const files = pdfFilesInput && pdfFilesInput.files ? Array.from(pdfFilesInput.files) : [];

        const previousValues = {};
        selectedFilesList.querySelectorAll('.file-item').forEach(item => {
            const filename = item.dataset.filename;
            const input = item.querySelector('.file-pages-input');
            if (filename && input) {
                previousValues[filename] = input.value || '';
            }
        });

        selectedFilesList.innerHTML = '';

        if (!files.length) {
            const info = document.createElement('p');
            info.className = 'subtitle muted';
            info.textContent = 'Po dodajanju datotek lahko pri posamezni prilogi vnesete strani (npr. 2, 4-6), ki naj se pretvorijo v slike za vizualno analizo.';
            selectedFilesList.appendChild(info);
            return;
        }

        files.forEach((file, index) => {
            const displayName = file.name || `Dokument_${index + 1}`;

            const item = document.createElement('div');
            item.className = 'file-item';
            item.dataset.index = String(index);
            item.dataset.filename = displayName;

            const head = document.createElement('div');
            head.className = 'file-head';

            const nameSpan = document.createElement('span');
            nameSpan.className = 'file-name';
            nameSpan.textContent = file.name || `Dokument ${index + 1}`;
            head.appendChild(nameSpan);

            if (typeof file.size === 'number') {
                const metaSpan = document.createElement('span');
                metaSpan.className = 'file-meta';
                metaSpan.textContent = formatFileSize(file.size);
                head.appendChild(metaSpan);
            }

            const pagesWrapper = document.createElement('div');
            pagesWrapper.className = 'file-pages';

            const label = document.createElement('label');
            const inputId = `filePages_${index}`;
            label.setAttribute('for', inputId);
            label.textContent = 'Strani za pretvorbo v slike (neobvezno)';

            const input = document.createElement('input');
            input.type = 'text';
            input.id = inputId;
            input.className = 'file-pages-input';
            input.placeholder = 'npr. 2, 4-6';
            input.dataset.filename = displayName;
            if (Object.prototype.hasOwnProperty.call(previousValues, displayName)) {
                input.value = previousValues[displayName];
            }

            pagesWrapper.appendChild(label);
            pagesWrapper.appendChild(input);

            item.appendChild(head);
            item.appendChild(pagesWrapper);

            selectedFilesList.appendChild(item);
        });
    }

    function collectCurrentState() {
        if (!sessionIdInput || !sessionIdInput.value) { return null; }
        const eupInputs = manualInputs ? Array.from(manualInputs.querySelectorAll('input[name="final_eup_list"]')) : [];
        const rabaInputs = manualInputs ? Array.from(manualInputs.querySelectorAll('input[name="final_raba_list"]')) : [];

        const eupList = eupInputs.map(input => input.value || '');
        const rabaList = rabaInputs.map(input => input.value || '');

        const keyData = {};
        Object.keys(keyLabels).forEach(key => {
            const field = analyzeForm ? analyzeForm.querySelector(`[name="${key}"]`) : null;
            if (field) { keyData[key] = field.value || ''; }
        });

        const metadata = {};
        metadataKeys.forEach(key => {
            const field = analyzeForm ? analyzeForm.querySelector(`[name="${key}"]`) : null;
            if (field) { metadata[key] = field.value || ''; }
        });

        const projectName = ((metadata && metadata.ime_projekta) || (keyData && keyData.ime_projekta) || '').toString().trim();

        let resultsMapToStore = currentResultsMap || {};
        if (existingResultsInput && existingResultsInput.value) {
            try {
                resultsMapToStore = JSON.parse(existingResultsInput.value);
            } catch (err) {
                console.warn('Ne morem razbrati shranjenih rezultatov, uporabim trenutno stanje.', err);
            }
        }

        return {
            sessionId: sessionIdInput.value,
            timestamp: new Date().toISOString(),
            eupList,
            rabaList,
            keyData,
            metadata,
            projectName,
            resultsMap: resultsMapToStore,
            zahteve: currentZahteve,
            existingResults: existingResultsInput ? existingResultsInput.value : null
        };
    }

    async function persistState(auto = false) {
        const state = collectCurrentState();
        if (!state) {
            if (!auto) { showStatus('Ni podatkov za shranjevanje (manjka ID seje).', 'error'); }
            return null;
        }

        saveStateToLocal(state);

        if (auto) {
            updateRestoreBanner();
            return state;
        }

        showStatus('Shranjujem analizo...', 'loading');

        try {
            const payload = {
                session_id: state.sessionId,
                data: state,
                project_name: state.projectName || undefined
            };
            const response = await fetch('/save-session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                let detail = 'Shranjevanje analize ni uspelo.';
                try {
                    const error = await response.json();
                    detail = error.detail || detail;
                } catch (_) { /* ignore */ }
                throw new Error(detail);
            }

            const result = await response.json();
            savedSessionsCache = null;
            await updateRestoreBanner(true);
            showStatus(result.message || 'Analiza je shranjena za kasnejšo dopolnitev.', 'success');
            return state;
        } catch (error) {
            console.error('Shranjevanje analize ni uspelo:', error);
            showStatus(error.message || 'Napaka pri shranjevanju analize.', 'error');
            return null;
        }
    }

    function applySavedState(state) {
        if (!state) { return; }
        if (!sessionIdInput) { return; }

        sessionIdInput.value = state.sessionId || '';

        clearManualInputs();

        const pairCount = Math.max(state.eupList ? state.eupList.length : 0, state.rabaList ? state.rabaList.length : 0);
        if (pairCount === 0) {
            addInputPair('', '');
        } else {
            for (let i = 0; i < pairCount; i++) {
                const eup = state.eupList && state.eupList[i] ? state.eupList[i] : '';
                const raba = state.rabaList && state.rabaList[i] ? state.rabaList[i] : '';
                addInputPair(eup, raba);
            }
        }

        const combinedData = Object.assign({}, state.metadata || {}, state.keyData || {});
        renderKeyDataFields(combinedData);

        Object.entries(state.keyData || {}).forEach(([key, value]) => {
            const field = analyzeForm ? analyzeForm.querySelector(`[name="${key}"]`) : null;
            if (field) { field.value = value || ''; }
        });
        Object.entries(state.metadata || {}).forEach(([key, value]) => {
            const field = analyzeForm ? analyzeForm.querySelector(`[name="${key}"]`) : null;
            if (field) { field.value = value || ''; }
        });

        analyzeForm.style.display = 'block';

        const serializedResults = state.existingResults || (state.resultsMap ? JSON.stringify(state.resultsMap) : '');
        if (existingResultsInput) {
            existingResultsInput.value = serializedResults || '';
        }

        renderResults(state.zahteve || [], state.resultsMap || {});
        showStatus('Shranjena analiza je naložena. Po potrebi naložite popravljeno dokumentacijo in izberite zahteve za ponovni pregled.', 'success');
    }

    async function discardSavedState(options = {}) {
        const source = options.source || highlightedSource;
        const session = options.session || highlightedSavedSession;

        if (source === 'remote' && session && session.session_id) {
            try {
                const response = await fetch(`/saved-sessions/${encodeURIComponent(session.session_id)}`, { method: 'DELETE' });
                if (!response.ok) {
                    let detail = 'Brisanje shranjene analize ni uspelo.';
                    try {
                        const error = await response.json();
                        detail = error.detail || detail;
                    } catch (_) { /* ignore */ }
                    throw new Error(detail);
                }
            } catch (error) {
                showStatus(error.message || 'Napaka pri brisanju shranjene analize.', 'error');
                return false;
            }
        }

        clearLocalState();
        savedSessionsCache = null;
        await updateRestoreBanner(true);
        showStatus('Shranjena analiza je odstranjena.', 'success');
        return true;
    }

    function escapeHtml(value) {
        if (typeof value !== 'string') { return ''; }
        const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
        return value.replace(/[&<>"']/g, ch => map[ch]);
    }

    function statusToClass(text) {
        if (!text) return 'neznano';
        const normalized = text.toLowerCase().trim();
        if (normalized.includes('neskladno')) return 'neskladno';
        if (normalized.includes('skladno')) return 'skladno';
        if (normalized.includes('ni relevantno')) return 'ni-relevantno';
        return 'neznano';
    }

    function truncateText(text, maxLength = 220) {
        if (!text) return '—';
        if (text.length <= maxLength) return text;
        return text.slice(0, maxLength).trimEnd() + '…';
    }

    function renderResults(zahteve, resultsMap) {
        currentZahteve = Array.isArray(zahteve) ? zahteve : [];
        currentResultsMap = resultsMap && typeof resultsMap === 'object' ? resultsMap : {};

        if (!currentZahteve.length) {
            if (resultsSection) {
                resultsSection.style.display = 'none';
            }
            if (resultsTable) {
                resultsTable.innerHTML = '';
            }
            if (revisionSection) {
                revisionSection.style.display = 'none';
            }
            if (revisionInfo) {
                revisionInfo.textContent = '';
            }
            return;
        }

        let tableHtml = `<table class="results-table"><thead><tr><th style="width:60px;">Izberi</th><th>Zahteva</th><th style="width:180px;">Status</th><th>Obrazložitev / ukrep</th></tr></thead><tbody>`;

        currentZahteve.forEach(z => {
            const result = currentResultsMap[z.id] || {};
            const statusText = result.skladnost || 'Neznano';
            const statusClass = statusToClass(statusText);
            const noteRaw = result.predlagani_ukrep && result.predlagani_ukrep !== '—'
                ? result.predlagani_ukrep
                : truncateText(result.obrazlozitev || '—');
            const note = truncateText(noteRaw, 280);
            const escapedTitle = escapeHtml(z.naslov);
            const escapedCategory = escapeHtml(z.kategorija);
            const escapedStatus = escapeHtml(statusText);
            const escapedNote = escapeHtml(note);
            const normalizedStatus = (statusText || '').toLowerCase();
            const normalizedNote = (result.predlagani_ukrep || '').toLowerCase();
            const shouldCheck = normalizedStatus.includes('neskladno') || normalizedNote.includes('ponovna analiza');
            const checked = shouldCheck ? 'checked' : '';
            tableHtml += `
                <tr class="status-${statusClass}">
                    <td><input type="checkbox" value="${z.id}" ${checked}></td>
                    <td>
                        <strong>${escapedTitle}</strong>
                        <span class="category-tag">${escapedCategory}</span>
                    </td>
                    <td><span class="status-pill ${statusClass}">${escapedStatus}</span></td>
                    <td><div class="result-note">${escapedNote}</div></td>
                </tr>`;
        });

        tableHtml += '</tbody></table>';

        if (resultsTable) {
            resultsTable.innerHTML = tableHtml;
        }
        if (resultsSection) {
            resultsSection.style.display = 'block';
        }
        if (revisionSection) {
            revisionSection.style.display = 'flex';
        }
    }

    function getSelectedRequirementIds() {
        if (!resultsTable) return [];
        return Array.from(resultsTable.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
    }
    
    function renderKeyDataFields(data) {
        keyDataFields.innerHTML = '';
        
        // Polji metapodatkov (samo za prikaz, ne za urejanje)
        data.ime_projekta = data.ime_projekta || 'Ni podatka';
        data.stevilka_projekta = data.stevilka_projekta || 'Ni podatka';
        
        const metadataLabels = {
             'ime_projekta': 'Ime projekta', 'stevilka_projekta': 'Številka projekta',
        };

        // Prikaz metapodatkov
        let metaHtml = '';
        for (const key in metadataLabels) {
            metaHtml += `<div class="data-item"><label>${metadataLabels[key]}:</label><input type="text" name="${key}" id="${key}" value="${data[key]}" readonly style="background:#eee;"></div>`;
        }
        
        // Vstavitev metapodatkov pred keyDataFields (polja A in B)
        const metaDiv = document.createElement('div');
        metaDiv.className = "key-data-grid";
        metaDiv.style.gridTemplateColumns = '1fr';
        metaDiv.innerHTML = metaHtml;

        const existingMetaDiv = analyzeForm.querySelector('.key-data-grid[style*="1fr"]');
@@ -1380,273 +2527,809 @@ def frontend():
            }
            keyDataFields.appendChild(div);
        }
    }

    function addInputPair(e = "", t = "") {
        const n = document.createElement("div");
        n.className = "manual-input-pair";
        n.innerHTML = `<input type="text" name="final_eup_list" placeholder="EUP (npr. LI-08)" value="${e}"><input type="text" name="final_raba_list" placeholder="Namenska raba (npr. SSe)" value="${t}"><button type="button" class="remove-btn">X</button>`;
        n.querySelector(".remove-btn").addEventListener("click", () => { n.remove() });
        manualInputs.appendChild(n);
    }
    addEupRabaBtn.addEventListener("click", () => addInputPair());

    function clearManualInputs() {
        manualInputs.innerHTML = '';
        keyDataFields.innerHTML = ''; // Počisti tudi polja za ključne podatke
        const existingMetaDiv = analyzeForm.querySelector('.key-data-grid[style*="1fr"]');
        if (existingMetaDiv) {
            existingMetaDiv.remove();
        }
        const existingSummaryDiv = analyzeForm.querySelector('.key-summary');
        if (existingSummaryDiv) {
            existingSummaryDiv.remove();
        }
        if (resultsSection) {
            resultsSection.style.display = 'none';
        }
        if (resultsTable) {
            resultsTable.innerHTML = '';
        }
        if (revisionSection) {
            revisionSection.style.display = 'none';
        }
        if (revisionInfo) {
            revisionInfo.textContent = '';
        }
        if (revisionFileInput) {
            revisionFileInput.value = '';
        }
        if (revisionPagesInput) {
            revisionPagesInput.value = '';
        }
        existingResultsInput.value = '';
        currentZahteve = [];
        currentResultsMap = {};
    }

    async function showStatus(e, t) {
        status.innerHTML = e;
        status.className = `status-${t}`;
        status.style.display = "block";
    }
    
    // --- KORAK 1: Ekstrakcija podatkov ---
    if (pdfFilesInput) {
        pdfFilesInput.addEventListener("change", renderSelectedFilesList);
        renderSelectedFilesList();
    }

    uploadForm.addEventListener("submit", async e => {
        e.preventDefault();
        
        const pdfFile = document.getElementById("pdfFile").files[0];
        if (!pdfFile) { showStatus("Prosim naloži PDF datoteko!", "error"); return; }
        

        const pdfFiles = pdfFilesInput && pdfFilesInput.files ? Array.from(pdfFilesInput.files) : [];
        if (!pdfFiles.length) { showStatus("Prosim naloži vsaj eno PDF datoteko!", "error"); return; }

        const formData = new FormData();
        formData.append('pdf_file', pdfFile);
        formData.append('pages_to_render', document.getElementById('pages').value.trim());
        const filesMeta = [];

        pdfFiles.forEach((file, index) => {
            formData.append('pdf_files', file);
            let pagesValue = '';
            if (selectedFilesList) {
                const row = selectedFilesList.querySelector(`.file-item[data-index="${index}"]`);
                if (row) {
                    const input = row.querySelector('.file-pages-input');
                    if (input && typeof input.value === 'string' && input.value.trim()) {
                        pagesValue = input.value.trim();
                    }
                }
            }
            filesMeta.push({
                name: file.name || `Dokument_${index + 1}`,
                pages: pagesValue
            });
        });

        try {
            formData.append('files_meta_json', JSON.stringify(filesMeta));
        } catch (err) {
            console.warn('Ne morem pripraviti meta podatkov o datotekah:', err);
        }

        showStatus("Analiziram dokumente in ekstrahiram ključne podatke...", "loading");
        submitBtn.disabled = true;
        

        try {
            const response = await fetch("/extract-data", { method: "POST", body: formData });
            
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || "Napaka pri ekstrahiranju podatkov.");
            }
            
            const data = await response.json();
            
            document.getElementById('sessionId').value = data.session_id;

            // 1. Prikaz AI zaznave EUP/Raba za lažji popravek
            clearManualInputs();
            const maxLength = Math.max(data.eup.length, data.namenska_raba.length);
            for (let i = 0; i < maxLength; i++) {
                const eup = data.eup[i] || '';
                const raba = data.namenska_raba[i] || '';
                addInputPair(eup, raba);
            }
            if (maxLength === 0) {
                 addInputPair('', ''); // Dodaj vsaj eno prazno polje, če AI nič ne najde
            }

            // 2. Prikaz/renderiranje urejevalnih polj za ključne podatke
            renderKeyDataFields(data);
            
            analyzeForm.style.display = 'block';
            showStatus("Ekstrakcija podatkov uspešna. Prosim, preglejte in po potrebi POPRAVITE podatke.", "success");
            
        } catch (error) {
            showStatus(`Napaka pri ekstrakciji: ${error.message}`, "error");
            analyzeForm.style.display = 'none';
        } finally {
            submitBtn.disabled = false;
        }
    });

    // --- KORAK 2: Izvedba analize ---
    analyzeForm.addEventListener("submit", async e => {
        e.preventDefault();

    async function runAnalysis(extraPayload = {}, options = {}) {
        const { isRerun = false } = options;
        const sessionId = document.getElementById('sessionId').value;
        if (!sessionId) {
            showStatus("Seja ni aktivna. Prosim, ponovite Korak 1.", "error");
            return;
        }
        
        // Zberemo VSE končne podatke iz analize forme
        const finalFormData = new FormData(analyzeForm); 
        
        showStatus("Izvajam podrobno analizo in generiram poročilo...", "loading");
        document.getElementById('analyzeBtn').disabled = true;

        const formData = new FormData(analyzeForm);

        if (Array.isArray(extraPayload.selectedIds) && extraPayload.selectedIds.length) {
            formData.append('selected_ids_json', JSON.stringify(extraPayload.selectedIds));
        }

        if (existingResultsInput) {
            const existingValue = existingResultsInput.value || '';
            if (typeof formData.set === 'function') {
                formData.set('existing_results_json', existingValue);
            } else if (existingValue) {
                formData.append('existing_results_json', existingValue);
            }
        }

        showStatus(isRerun ? "Ponovno preverjam izbrane zahteve..." : "Izvajam podrobno analizo in generiram poročilo...", "loading");
        analyzeBtn.disabled = true;
        submitBtn.disabled = true;
        if (rerunSelectedBtn) { rerunSelectedBtn.disabled = true; }

        try {
            const response = await fetch("/analyze-report", { method: "POST", body: finalFormData });
            const response = await fetch("/analyze-report", { method: "POST", body: formData });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || "Napaka pri analizi in generiranju poročila.");
            }
            

            const result = await response.json();
            showStatus(`Poročilo uspešno ustvarjeno! Analizirano ${result.total} zahtev. <br><a href="/download" style="font-weight:bold;">Prenesi poročilo (.docx)</a>`, "success");
            analyzeForm.style.display = 'none';
            const analyzed = result.total_analyzed ?? result.total ?? 0;
            const totalAvailable = result.total_available ?? result.total ?? analyzed;
            const scopeLabel = result.analysis_scope === "partial" ? "Ponovno analiziranih" : "Analiziranih";

            showStatus(`Poročilo uspešno ustvarjeno! ${scopeLabel} ${analyzed} od ${totalAvailable} zahtev. <br><a href="/download" style="font-weight:bold;">Prenesi poročilo (.docx)</a>`, "success");

            if (result.results_map) {
                try {
                    existingResultsInput.value = JSON.stringify(result.results_map);
                } catch (err) {
                    console.warn('Ne morem serializirati rezultatov:', err);
                    existingResultsInput.value = '';
                }
            } else {
                existingResultsInput.value = '';
            }

            renderResults(result.zahteve || [], result.results_map || {});
            persistState(true);
        } catch (error) {
            showStatus(`Kritična napaka pri analizi: ${error.message}`, "error");
        } finally {
            document.getElementById('analyzeBtn').disabled = false;
            analyzeBtn.disabled = false;
            submitBtn.disabled = false;
            if (rerunSelectedBtn) { rerunSelectedBtn.disabled = false; }
        }
    }

    // --- KORAK 2: Izvedba analize ---
    analyzeForm.addEventListener("submit", async e => {
        e.preventDefault();
        await runAnalysis();
    });

    if (rerunSelectedBtn) {
        rerunSelectedBtn.addEventListener("click", async () => {
            if (!existingResultsInput.value) {
                showStatus("Za ponovno preverjanje potrebujemo rezultate zadnje analize.", "error");
                return;
            }

            const selected = getSelectedRequirementIds();
            if (!selected.length) {
                showStatus("Izberite vsaj eno zahtevo za ponovno preverjanje.", "error");
                return;
            }

            await runAnalysis({ selectedIds: selected }, { isRerun: true });
        });
    }

    if (resetSelectionBtn) {
        resetSelectionBtn.addEventListener("click", () => {
            if (!resultsTable) return;
            resultsTable.querySelectorAll('input[type="checkbox"]').forEach(cb => { cb.checked = false; });
        });
    }

    if (saveProgressBtn) {
        saveProgressBtn.addEventListener("click", async () => {
            if (saveProgressBtn.disabled) { return; }
            saveProgressBtn.disabled = true;
            try {
                await persistState(false);
            } finally {
                saveProgressBtn.disabled = false;
            }
        });
    }

    if (openSavedBtn) {
        openSavedBtn.addEventListener("click", async () => {
            const sessions = await fetchSavedSessionsList(true);
            if (sessions.length > 0) {
                showSavedSessionsModal(sessions);
                return;
            }

            const local = loadSavedState();
            if (local && local.sessionId) {
                applySavedState(local);
                showStatus('Lokalno shranjena analiza je naložena.', 'success');
                updateRestoreBanner();
                if (analyzeForm && typeof analyzeForm.scrollIntoView === 'function') {
                    analyzeForm.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            } else {
                showStatus('Ni shranjenih analiz za odprtje.', 'error');
            }
        });
    }

    if (restoreSessionBtn) {
        restoreSessionBtn.addEventListener("click", async () => {
            if (highlightedSource === 'remote' && highlightedSavedSession) {
                const loaded = await loadSessionFromServer(highlightedSavedSession.session_id);
                if (loaded && analyzeForm && typeof analyzeForm.scrollIntoView === 'function') {
                    analyzeForm.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
                return;
            }

            const local = loadSavedState();
            if (local && local.sessionId) {
                applySavedState(local);
                showStatus('Lokalno shranjena analiza je naložena.', 'success');
                updateRestoreBanner();
                if (analyzeForm && typeof analyzeForm.scrollIntoView === 'function') {
                    analyzeForm.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            } else {
                showStatus('Ni shranjene analize za obnovitev.', 'error');
            }
        });
    }

    if (discardSessionBtn) {
        discardSessionBtn.addEventListener("click", async () => {
            await discardSavedState({});
        });
    }

    if (closeSavedModalBtn) {
        closeSavedModalBtn.addEventListener("click", hideSavedSessionsModal);
    }

    if (savedSessionsModal) {
        savedSessionsModal.addEventListener("click", event => {
            if (event.target === savedSessionsModal) {
                hideSavedSessionsModal();
            }
        });
    }

    document.addEventListener("keydown", event => {
        if (event.key === 'Escape' && savedSessionsModal && savedSessionsModal.classList.contains('active')) {
            hideSavedSessionsModal();
        }
    });

    if (uploadRevisionBtn) {
        uploadRevisionBtn.addEventListener("click", async () => {
            if (!sessionIdInput || !sessionIdInput.value) {
                showStatus('Aktivna seja ni na voljo. Ponovite prvi korak.', 'error');
                return;
            }

            const files = revisionFileInput && revisionFileInput.files ? Array.from(revisionFileInput.files) : [];
            if (!files.length) {
                showStatus('Izberite popravljeno projektno dokumentacijo v PDF formatu.', 'error');
                return;
            }

            const formData = new FormData();
            formData.append('session_id', sessionIdInput.value);
            files.forEach(file => formData.append('revision_files', file));
            if (revisionPagesInput && revisionPagesInput.value.trim()) {
                formData.append('revision_pages', revisionPagesInput.value.trim());
            }

            showStatus('Nalaganje popravljenega dokumenta in osveževanje podatkov...', 'loading');
            uploadRevisionBtn.disabled = true;

            try {
                const response = await fetch('/upload-revision', { method: 'POST', body: formData });

                if (!response.ok) {
                    let detail = 'Napaka pri nalaganju popravka.';
                    try {
                        const error = await response.json();
                        detail = error.detail || detail;
                    } catch (_) { /* ignore parsing napake */ }
                    throw new Error(detail);
                }

                const result = await response.json();
                const infoMessage = result.message || 'Popravek je uspešno naložen. Izberite neskladne zahteve in zaženite ponovno analizo.';
                showStatus(infoMessage, 'success');

                if (revisionInfo) {
                    if (result.last_revision) {
                        const ts = result.last_revision.uploaded_at ? new Date(result.last_revision.uploaded_at) : null;
                        const formatted = ts && !Number.isNaN(ts.getTime()) ? ts.toLocaleString('sl-SI') : '';
                        const uploadedNames = Array.isArray(result.last_revision.filenames) ? result.last_revision.filenames : [];
                        let label = '';
                        if (uploadedNames.length === 1) {
                            label = uploadedNames[0];
                        } else if (uploadedNames.length > 1) {
                            label = `${uploadedNames.length} datotek`;
                        } else if (files.length === 1) {
                            label = files[0].name;
                        } else if (files.length > 1) {
                            label = `${files.length} datotek`;
                        } else {
                            label = 'PDF';
                        }
                        revisionInfo.textContent = `Zadnji popravek: ${label}${formatted ? ` (${formatted})` : ''}`;
                    } else {
                        revisionInfo.textContent = 'Popravljena dokumentacija je pripravljena. Izberite zahteve in ponovno zaženite analizo.';
                    }
                }

                if (revisionFileInput) { revisionFileInput.value = ''; }
                persistState(true);
            } catch (error) {
                showStatus(error.message || 'Napaka pri nalaganju popravka.', 'error');
            } finally {
                uploadRevisionBtn.disabled = false;
            }
        });
    }

    window.addEventListener("load", () => {
        updateRestoreBanner();
    });
</script>
</body></html>"""
    return html.replace("YEAR_PLACEHOLDER", str(datetime.now().year))


def infer_project_name(data: Dict[str, Any], fallback: str = "Neimenovan projekt") -> str:
    candidates = []
    for key in ("metadata", "keyData"):
        section = data.get(key)
        if isinstance(section, dict):
            name = section.get("ime_projekta") or section.get("ime_projekta_original")
            if name:
                candidates.append(str(name))
    direct_name = data.get("projectName") or data.get("project_name")
    if direct_name:
        candidates.insert(0, str(direct_name))
    for value in candidates:
        clean = value.strip()
        if clean:
            return clean
    return fallback


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
        upsert_session(session_id, project_name, summary, data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Shranjevanje analize ni uspelo: {exc}") from exc

    return {
        "message": "Analiza je shranjena.",
        "session_id": session_id,
        "project_name": project_name,
        "summary": summary,
    }


@app.get("/saved-sessions")
async def list_saved_sessions():
    rows = fetch_sessions()
    sessions = []
    for row in rows:
        sessions.append(
            {
                "session_id": row["session_id"],
                "project_name": row["project_name"] or "Neimenovan projekt",
                "summary": row["summary"] or "",
                "updated_at": row["updated_at"],
            }
        )
    return {"sessions": sessions}


@app.get("/saved-sessions/{session_id}")
async def get_saved_session(session_id: str):
    record = fetch_session(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Shranjena analiza ne obstaja.")
    return record


@app.delete("/saved-sessions/{session_id}")
async def remove_saved_session(session_id: str):
    record = fetch_session(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Shranjena analiza ne obstaja.")
    delete_session(session_id)
    return {"message": "Shranjena analiza je izbrisana.", "session_id": session_id}


@app.post("/extract-data")
async def extract_data(
    pdf_file: UploadFile = File(...),
    pages_to_render: Optional[str] = Form(None),
    pdf_files: List[UploadFile] = File(...),
    files_meta_json: Optional[str] = Form(None),
):
    """Prvi korak: Ekstrahira ključne podatke in jih shrani za kasnejšo analizo."""
    try:
        session_id = str(datetime.now().timestamp())
        

        print(f"\n{'='*60}\n📤 Korak 1: Ekstrakcija podatkov (ID: {session_id})\n{'='*60}\n")
        pdf_bytes = await pdf_file.read()
        
        project_text = parse_pdf(pdf_bytes)
        images = convert_pdf_pages_to_images(pdf_bytes, pages_to_render)
        
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
            except (json.JSONDecodeError, ValueError):
                raise HTTPException(status_code=400, detail="Neveljavni podatki o straneh za grafični pregled.")

        combined_text_parts: List[str] = []
        all_images: List[Image.Image] = []
        files_manifest: List[Dict[str, Any]] = []

        for index, upload in enumerate(pdf_files):
            pdf_bytes = await upload.read()
            if not pdf_bytes:
                continue

            file_label = upload.filename or f"Dokument_{index + 1}.pdf"
            print(f"🔄 Obdelujem datoteko: {file_label} ({len(pdf_bytes)} bajtov)")

            text = parse_pdf(pdf_bytes)
            if text:
                combined_text_parts.append(f"=== VIR: {file_label} ===\n{text}")

            page_hint = page_overrides.get(upload.filename) or page_overrides.get(file_label)
            if page_hint:
                images_for_file = convert_pdf_pages_to_images(pdf_bytes, page_hint)
                if images_for_file:
                    all_images.extend(images_for_file)

            files_manifest.append({
                "filename": file_label,
                "pages": page_hint or "",
                "size": len(pdf_bytes),
            })

        if not combined_text_parts:
            raise HTTPException(status_code=400, detail="Iz naloženih datotek ni bilo mogoče prebrati besedila.")

        project_text = "\n\n".join(combined_text_parts)
        images = all_images

        # 1. AI Detektiv (EUP/Raba)
        ai_details = call_gemini_for_details(project_text, images)
        

        # 2. AI Arhivar (Metapodatki)
        metadata = call_gemini_for_metadata(project_text) 
        
        # 3. AI Ekstraktor (Ključni podatki) - RAZŠIRJENO
        key_data = call_gemini_for_key_data(project_text, images)
        
        # 4. Shranjevanje vseh podatkov za drugi korak
        TEMP_STORAGE[session_id] = {
            "project_text": project_text,
            "images": images,
            "metadata": metadata,
            "ai_details": ai_details,
            "key_data": key_data,
            "source_files": files_manifest,
        }
        

        # 5. Priprava povratnega JSON za frontend
        response_data = {
            "session_id": session_id,
            "eup": ai_details.get("eup", []), 
            "eup": ai_details.get("eup", []),
            "namenska_raba": ai_details.get("namenska_raba", []),
            **metadata,
            **key_data, # Vključeni vsi razširjeni podatki
            "uploaded_files": files_manifest,
        }
        
        return response_data
    except Exception as e:
        import traceback
        traceback.print_exc()
        if isinstance(e, HTTPException): raise
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload-revision")
async def upload_revision(
    session_id: str = Form(...),
    revision_files: List[UploadFile] = File(...),
    revision_pages: Optional[str] = Form(None),
):
    """Posodobi besedilo in slike seje z novo (popravljeno) dokumentacijo."""

    if session_id not in TEMP_STORAGE:
        raise HTTPException(status_code=404, detail="Seja ni aktivna ali je potekla. Ponovite prvi korak nalaganja dokumentacije.")

    try:
        if not revision_files:
            raise HTTPException(status_code=400, detail="Dodajte popravljeno projektno dokumentacijo.")

        combined_text_parts: List[str] = []
        combined_images: List[Image.Image] = []
        revision_names: List[str] = []

        for index, revision_file in enumerate(revision_files):
            pdf_bytes = await revision_file.read()
            if not pdf_bytes:
                continue

            file_label = revision_file.filename or f"popravek_{index + 1}.pdf"
            revision_names.append(file_label)

            text = parse_pdf(pdf_bytes)
            if text:
                combined_text_parts.append(f"=== POPRAVEK: {file_label} ===\n{text}")

            if revision_pages and revision_pages.strip():
                new_images = convert_pdf_pages_to_images(pdf_bytes, revision_pages)
                if new_images:
                    combined_images.extend(new_images)

        if not combined_text_parts and not combined_images:
            raise HTTPException(status_code=400, detail="Popravljena dokumentacija ne vsebuje uporabnih podatkov za analizo.")

        data = TEMP_STORAGE.get(session_id, {})
        if not data:
            raise HTTPException(status_code=404, detail="Podatki shranjene seje niso na voljo. Ponovite Korak 1.")

        if combined_text_parts:
            data["project_text"] = "\n\n".join(combined_text_parts)
        if combined_images:
            data["images"] = combined_images
        if revision_names:
            data["source_files"] = [{"filename": name, "pages": revision_pages or ""} for name in revision_names]

        history_entry = {
            "filenames": revision_names,
            "uploaded_at": datetime.now().isoformat(),
        }
        revision_history = data.get("revision_history", [])
        revision_history.append(history_entry)
        data["revision_history"] = revision_history

        TEMP_STORAGE[session_id] = data

        return {
            "status": "success",
            "message": "Popravljena dokumentacija je naložena. Izberite neskladne zahteve in zaženite ponovno analizo.",
            "revision_count": len(revision_history),
            "last_revision": history_entry,
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze-report")
async def analyze_report(
    session_id: str = Form(...),
    # Podatki A (EUP/Raba)
    final_eup_list: List[str] = Form(None),
    final_raba_list: List[str] = Form(None),
    # Podatki B (Ključni podatki) - VSE
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
    komunalni_prikljucki: str = Form("Ni podatka v dokumentaciji")
    komunalni_prikljucki: str = Form("Ni podatka v dokumentaciji"),
    selected_ids_json: Optional[str] = Form(None),
    existing_results_json: Optional[str] = Form(None)
):
    """Drugi korak: Izvede glavno analizo s potrjenimi/popravljenimi podatki."""
    global LAST_DOCX_PATH
    
    if session_id not in TEMP_STORAGE:
        raise HTTPException(status_code=404, detail="Seja je potekla ali podatki niso bili ekstrahirani. Prosim, ponovite Korak 1.")
        
    data = TEMP_STORAGE.pop(session_id) # Odstrani podatke iz začasnega pomnilnika po uporabi
    

    data = TEMP_STORAGE.get(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Podatki seje niso na voljo. Ponovite Korak 1.")

    try:
        print(f"\n{'='*60}\n⚙️ Korak 2: Izvajam analizo (ID: {session_id})\n{'='*60}\n")
        

        selected_ids: List[str] = []
        if selected_ids_json:
            try:
                parsed_ids = json.loads(selected_ids_json)
                if not isinstance(parsed_ids, list):
                    raise ValueError
                selected_ids = [str(item).strip() for item in parsed_ids if str(item).strip()]
            except (json.JSONDecodeError, ValueError):
                raise HTTPException(status_code=400, detail="Neveljaven seznam izbranih zahtev za ponovno preverjanje.")

        existing_results_map: Dict[str, Dict[str, Any]] = {}
        if existing_results_json:
            try:
                parsed_results = json.loads(existing_results_json)
                if isinstance(parsed_results, dict):
                    existing_results_map = {str(k): v for k, v in parsed_results.items() if isinstance(v, dict)}
                else:
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                raise HTTPException(status_code=400, detail="Neveljaven JSON rezultatov prejšnje analize.")

        # 1. Čiščenje in konsolidacija VSEH končnih EUP in Raba
        eup_from_form = [e.strip() for e in final_eup_list if e and e.strip()] if final_eup_list is not None else []
        raba_from_form = [r.strip().upper() for r in final_raba_list if r and r.strip()] if final_raba_list is not None else []

        final_eup_list_cleaned = list(dict.fromkeys(eup_from_form))
        final_raba_list_cleaned = list(dict.fromkeys(raba_from_form))

        if not final_raba_list_cleaned:
            raise HTTPException(status_code=404, detail="Namenska raba za analizo manjka. Prosim, vnesite jo ročno.")
            
        print(f"🔥 Finalna konsolidacija za analizo: EUP={final_eup_list_cleaned}, Raba={final_raba_list_cleaned}")
        
        # 2. Sestava zahtev
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

        # 3. Priprava prompta za AI analizo - Vključitev VSEH končnih (potrjenih/popravljenih) ključnih podatkov
        final_key_data = {
            "glavni_objekt": glavni_objekt, "vrsta_gradnje": vrsta_gradnje,
            "klasifikacija_cc_si": klasifikacija_cc_si, "nezahtevni_objekti": nezahtevni_objekti,
            "enostavni_objekti": enostavni_objekti, "vzdrzevalna_dela": vzdrzevalna_dela,
            "parcela_objekta": parcela_objekta, "stevilke_parcel_ko": stevilke_parcel_ko,
            "velikost_parcel": velikost_parcel, "velikost_obstojecega_objekta": velikost_obstojecega_objekta,
            "tlorisne_dimenzije": tlorisne_dimenzije, "gabariti_etaznost": gabariti_etaznost,
            "faktor_zazidanosti_fz": faktor_zazidanosti_fz, "faktor_izrabe_fi": faktor_izrabe_fi,
            "zelene_povrsine": zelene_povrsine, "naklon_strehe": naklon_strehe,
            "kritina_barva": kritina_barva, "materiali_gradnje": materiali_gradnje,
            "smer_slemena": smer_slemena, "visinske_kote": visinske_kote,
            "odmiki_parcel": odmiki_parcel, "komunalni_prikljucki": komunalni_prikljucki
        }
        
        # Za uvodni povzetek dodamo tudi metapodatke
        metadata_formatted = "\n".join([f"- {k.replace('_', ' ').capitalize()}: {v}" for k, v in data["metadata"].items()])
        key_data_formatted = "\n".join([f"- {k.replace('_', ' ').capitalize()}: {v}" for k, v in final_key_data.items()])
        
        modified_project_text = f"""
        --- METAPODATKI PROJEKTA ---
        {metadata_formatted}
        --- KLJUČNI GABARITNI IN LOKACIJSKI PODATKI PROJEKTA (Ekstrahirano in POTRJENO) ---
        {key_data_formatted}
        --- DOKUMENTACIJA (Besedilo in grafike) ---
        {data["project_text"]}
        """
        
        # Pravilen klic funkcije: Uporabimo le 3 zahtevane argumente.
        prompt = build_prompt(modified_project_text, zahteve, IZRAZI_TEXT, UREDBA_TEXT)
        prompt = build_prompt(modified_project_text, zahteve_za_analizo, IZRAZI_TEXT, UREDBA_TEXT)
        ai_response = call_gemini(prompt, data["images"])
        results_map = parse_ai_response(ai_response, zahteve)
        
        results_map = parse_ai_response(ai_response, zahteve_za_analizo)

        combined_results_map = {k: v for k, v in existing_results_map.items()}
        combined_results_map.update(results_map)

        for z in zahteve:
            if z["id"] not in combined_results_map:
                combined_results_map[z["id"]] = {
                    "id": z["id"],
                    "obrazlozitev": "Zahteva ni bila analizirana v tem koraku.",
                    "evidence": "—",
                    "skladnost": "Neznano",
                    "predlagani_ukrep": "Ponovna analiza je potrebna."
                }

        # 4. Generiranje poročila
        output_path = f"./Porocilo_Skladnosti_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
        LAST_DOCX_PATH = generate_word_report(zahteve, results_map, data["metadata"], output_path)
        
        return {"status": "success", "docx_path": LAST_DOCX_PATH, "total": len(zahteve)}
        
        LAST_DOCX_PATH = generate_word_report(zahteve, combined_results_map, data["metadata"], output_path)

        TEMP_STORAGE[session_id] = data

        analysis_scope = "partial" if selected_id_set and len(selected_id_set) < len(zahteve) else "full"
        zahteve_summary = [{"id": z["id"], "naslov": z["naslov"], "kategorija": z.get("kategorija", "Ostalo")} for z in zahteve]

        return {
            "status": "success",
            "docx_path": LAST_DOCX_PATH,
            "total_analyzed": len(zahteve_za_analizo),
            "total_available": len(zahteve),
            "analysis_scope": analysis_scope,
            "results_map": combined_results_map,
            "zahteve": zahteve_summary
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        # Pri kritični napaki podatke seje začasno ponovno shrani
        TEMP_STORAGE[session_id] = data
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(e))
