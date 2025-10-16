"""Gemini integration helpers."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from fastapi import HTTPException
from PIL import Image

import google.generativeai as genai

from .config import API_KEY, GEN_CFG, MODEL_NAME, EXTRACTION_MODEL_NAME, EMBEDDING_MODEL

genai.configure(api_key=API_KEY)


def _clean_json_string(text: str) -> str:
    """Odstrani Markdown tripple-backticks, nevidne znake (kot je BOM) in nepotrebni 'json' napis."""
    clean = re.sub(r"```(json)?", "", text, flags=re.IGNORECASE).strip()
    return clean.replace('\ufeff', '') # Odstranitev BOM znaka, če je prisoten

def embed_query(text: str) -> List[float]:
    """
    Generira vektorsko predstavitev (embedding) za podano besedilo
    z uporabo Googlovega modela.
    """
    cleaned_text = (text or "").strip()
    if not cleaned_text:
        return []
    try:
        result = genai.embed_content(
            model=EMBEDDING_MODEL,
            content=cleaned_text,
            task_type="RETRIEVAL_QUERY"
        )
        return result.get("embedding", [])
    except Exception as exc:
        print(f"⚠️ Napaka pri generiranju embeddinga: {exc}")
        return []


def call_gemini_for_initial_extraction(project_text: str, images: List[Image.Image]) -> Dict[str, Any]:
    """
    Izvede en sam klic za ekstrakcijo vseh začetnih podatkov po dogovorjeni strukturi.
    """
    # --- ZAČETEK POPRAVKA ---
    # Ta slovar je sedaj usklajen s končno dogovorjeno strukturo.
    KEY_DATA_PROMPT_MAP = {
        "naziv_gradnje": "Celoten naziv gradnje, ki združuje vrsto gradnje in opis objekta (npr. 'Novogradnja enostanovanjske stavbe').",
        "glavni_objekt": "Kratek, jedrnat opis glavnega objekta.",
        "pomozni_objekti": "Opis vseh pomožnih, nezahtevnih ali enostavnih objektov, ki so del projekta.",
        "parcela_in_ko": "Navedba vseh parcelnih številk in ime katastrske občine.",
        "dimenzije_objektov": "Tlorisne dimenzije glavnega objekta in morebitnih pomožnih objektov.",
        "bruto_etazna_povrsina": "Bruto etažna površina (BTP) glavnega objekta in pomožnih objektov v m².",
        "etaznost": "Etažnost glavnega objekta (npr. K+P+M ali P+1).",
        "visinski_gabariti": "Ključni višinski gabariti: višinska kota pritličja, terena, etaž, višina slemena, višina kapi/venca in višina kolenčnega zidu.",
        "streha_naklon_smer_kritina": "Združen opis strehe: naklon v stopinjah, smer slemena in vrsta ter barva kritine.",
        "barva_fasade": "Opis materialov in barve fasade.",
        "odmiki": "Najpomembnejši odmiki objekta od parcelnih mej ali drugih objektov.",
        "parkirna_mesta": "Navedba števila zagotovljenih ali potrebnih parkirnih mest (PM).",
        "prikljucki_gji": "Podroben opis načina priključitve objekta na gospodarsko javno infrastrukturo (voda, elektrika, kanalizacija, telekomunikacije).",
        "faktorji_in_ozelenitev": "Vrednosti za Faktor Zazidanosti (FZ), Faktor Izrabe (FI), Faktor Zelenih Površin (FZP) ter drugi morebitni faktorji.",
    }
    # --- KONEC POPRAVKA ---

    prompt_items = "\n".join([f"- **{key}**: {desc}" for key, desc in KEY_DATA_PROMPT_MAP.items()])

    prompt = f"""
# VLOGA
Deluješ kot visoko natančen asistent za ekstrakcijo podatkov iz projektne dokumentacije (besedila in slik).

# NALOGA
Analiziraj priloženo dokumentacijo in izlušči VSE zahtevane podatke v en sam JSON objekt. Tvoj odgovor mora biti strukturiran točno po spodnjih navodilih.

# IZHODNI FORMAT (STROGO)
Odgovori SAMO z enim JSON objektom, ki ima natančno tri ključe na najvišjem nivoju: "details", "metadata", in "key_data".

- **details**: Vsebuje seznama (array) za "eup" in "namenska_raba".
- **metadata**: Vsebuje objekt s polji "ime_projekta", "stevilka_projekta", "datum_projekta", "projektant".
- **key_data**: Vsebuje objekt z vsemi tehničnimi podatki o gradnji, kot so zahtevani spodaj.

Če katerega podatka ne najdeš, uporabi ustrezne privzete vrednosti (prazen seznam `[]` za EUP/rabo, "Ni podatka" za metapodatke, "Ni podatka v dokumentaciji" za tehnične podatke).

---
# ZAHTEVANI PODATKI

## 1. Details (EUP in Namenska Raba)
- **eup**: Seznam VSEH oznak Enot Urejanja Prostora.
- **namenska_raba**: Seznam VSEH kratic podrobnejših namenskih rab (npr. SSe, IG).

## 2. Metadata (Osnovni Podatki Projekta)
- **ime_projekta**: Polno ime projekta.
- **stevilka_projekta**: Številka projekta.
- **datum_projekta**: Datum izdelave.
- **projektant**: Ime projektanta.

## 3. Key Data (Podatki o Gradnji)
{prompt_items}
---

# PRIMER IZHODNEGA FORMATA
{{
  "details": {{
    "eup": ["LI-08 SSe*"],
    "namenska_raba": ["SSe"]
  }},
  "metadata": {{
    "ime_projekta": "Novogradnja enostanovanjske stavbe",
    "stevilka_projekta": "P-123/2024",
    "datum_projekta": "Oktober 2025",
    "projektant": "Projekt d.o.o."
  }},
  "key_data": {{
    "naziv_gradnje": "Novogradnja enostanovanjske stavbe",
    "glavni_objekt": "Enostanovanjska hiša etažnosti P+M",
    "bruto_etazna_povrsina": "180 m²",
    "faktorji_in_ozelenitev": "FZ = 0.35, FI = 0.7, FZP = 45%",
    "...": "..."
  }}
}}
---

# DOKUMENTACIJA ZA ANALIZO
{project_text[:40000]}
"""
    try:
        model = genai.GenerativeModel(EXTRACTION_MODEL_NAME, generation_config={"response_mime_type": "application/json"})
        content_parts = [prompt]
        if images:
            content_parts.extend(images)

        response = model.generate_content(content_parts)
        clean_response = _clean_json_string(response.text)
        result = json.loads(clean_response)

        final_result = {
            "details": result.get("details", {"eup": [], "namenska_raba": []}),
            "metadata": result.get("metadata", {}),
            "key_data": result.get("key_data", {}),
        }
        
        for key in ["ime_projekta", "stevilka_projekta", "datum_projekta", "projektant"]:
            final_result["metadata"][key] = str(final_result["metadata"].get(key) or "Ni podatka")
            
        for key in KEY_DATA_PROMPT_MAP.keys():
            final_result["key_data"][key] = str(final_result["key_data"].get(key) or "Ni podatka v dokumentaciji")

        return final_result

    except Exception as exc:
        print(f"⚠️ Napaka pri združeni AI ekstrakciji: {exc}.")
        return {
            "details": {"eup": [], "namenska_raba": []},
            "metadata": {"ime_projekta": "NAPAKA", "stevilka_projekta": "NAPAKA", "datum_projekta": "NAPAKA", "projektant": "NAPAKA"},
            "key_data": {key: "Napaka pri ekstrakciji" for key in KEY_DATA_PROMPT_MAP.keys()},
        }


def call_gemini(prompt: str, images: List[Image.Image]) -> str:
    try:
        model = genai.GenerativeModel(MODEL_NAME, generation_config=GEN_CFG)
        content_parts = [prompt]
        content_parts.extend(images)
        response = model.generate_content(content_parts)
        if not response.parts:
            reason = response.candidates[0].finish_reason if response.candidates else "NEZNAN"
            raise RuntimeError(f"Gemini ni vrnil veljavnega odgovora. Razlog: {reason}")
        text = "".join(part.text for part in response.parts)
        return text
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Gemini napaka (Analitik): {exc}") from exc


def parse_ai_response(response_text: str, expected_zahteve: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    clean = re.sub(r"```(json)?", "", response_text, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(clean)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Neveljaven JSON iz AI: {exc}\n\nOdgovor:\n{response_text[:500]}") from exc

    if not isinstance(data, list):
        raise HTTPException(status_code=500, detail="AI ni vrnil seznama objektov v JSON formatu.")

    results_map: Dict[str, Dict[str, Any]] = {}
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            results_map[item["id"]] = item

    for z in expected_zahteve:
        if z["id"] not in results_map:
            results_map[z["id"]] = {
                "id": z["id"],
                "obrazlozitev": "AI ni uspel generirati odgovora.",
                "evidence": "—",
                "skladnost": "Neznano",
                "predlagani_ukrep": "Ročno preverjanje.",
            }
    return results_map


__all__ = [
    "call_gemini_for_initial_extraction",
    "call_gemini",
    "parse_ai_response",
    "embed_query",
]
