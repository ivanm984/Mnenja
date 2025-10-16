"""Gemini integration helpers."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from fastapi import HTTPException
from PIL import Image

import google.generativeai as genai

from .config import API_KEY, GEN_CFG, MODEL_NAME, EXTRACTION_MODEL_NAME

genai.configure(api_key=API_KEY)


def _clean_json_string(text: str) -> str:
    """Odstrani Markdown tripple-backticks, nevidne znake (kot je BOM) in nepotrebni 'json' napis."""
    clean = re.sub(r"```(json)?", "", text, flags=re.IGNORECASE).strip()
    return clean.replace('\ufeff', '') # Odstranitev BOM znaka, če je prisoten

def call_gemini_for_initial_extraction(project_text: str, images: List[Image.Image]) -> Dict[str, Any]:
    """
    Izvede en sam klic za ekstrakcijo vseh začetnih podatkov:
    detajlov (EUP, raba), metapodatkov in ključnih tehničnih podatkov.
    """
    KEY_DATA_PROMPT_MAP = {
        "glavni_objekt": "Natančen opis glavnega objekta (npr. enostanovanjska hiša, gospodarski objekt, opiši funkcijo).",
        "vrsta_gradnje": "Vrsta gradnje (npr. novogradnja, dozidava, nadzidava, rekonstrukcija, sprememba namembnosti).",
        "klasifikacija_cc_si": "CC-SI oziroma druga uradna klasifikacija objekta, če je navedena.",
        "nezahtevni_objekti": "Ali projekt vključuje nezahtevne objekte (navedi katere in njihove dimenzije).",
        "enostavni_objekti": "Ali projekt vključuje enostavne objekte (navedi katere in njihove dimenzije).",
        "vzdrzevalna_dela": "Opiši načrtovana vzdrževalna dela ali manjše rekonstrukcije, če so predvidene.",
        "parcela_objekta": "Številka gradbene/osnovne parcele (npr. 123/5).",
        "stevilke_parcel_ko": "Vse parcele in katastrska občina, ki so del projekta (npr. 123/5, 124/6, k.o. Litija).",
        "velikost_parcel": "Skupna velikost vseh parcel (npr. 1500 m2).",
        "velikost_obstojecega_objekta": "Velikost in etažnost obstoječih objektov na parceli (npr. hiša 10x8m P+1N, pomožni objekt 5x4m).",
        "tlorisne_dimenzije": "Zunanje tlorisne dimenzije NOVEGA glavnega objekta (npr. 12.0 m x 8.5 m).",
        "gabariti_etaznost": "Navedi etažnost in vertikalni gabarit NOVEGA objekta (npr. K+P+1N+M, višina kolenčnega zidu 1.5 m).",
        "faktor_zazidanosti_fz": "Vrednost faktorja zazidanosti (npr. 0.35 ali FZ=35%).",
        "faktor_izrabe_fi": "Vrednost faktorja izrabe (npr. 0.70 ali FI=0.7).",
        "zelene_povrsine": "Velikost in/ali faktor zelenih površin (npr. 700 m2, FZP=0.47).",
        "naklon_strehe": "Naklon strehe v stopinjah in tip (npr. 40° ali simetrična dvokapnica, 40 stopinj).",
        "kritina_barva": "Material in barva strešne kritine (npr. opečna kritina, temno rdeča).",
        "materiali_gradnje": "Tipični materiali (npr. masivna lesena hiša ali opeka, klasična gradnja).",
        "smer_slemena": "Orientacija slemena glede na plastnice (npr. vzporedno s cesto/vrstnim redom gradnje).",
        "visinske_kote": "Pomembne kote (k.n.t., k.p. pritličja, k. slemena) (npr. k.p. = 345.50 m n.m.).",
        "odmiki_parcel": "Najmanjši in najpomembnejši navedeni odmiki od sosednjih parcelnih meja (npr. Južna meja: 4.5 m; Severna meja: 8.0 m).",
        "komunalni_prikljucki": "Opis priključitve na javno komunalno omrežje (elektrika, vodovod, kanalizacija itd.).",
    }
    prompt_items = "\n".join([f"- **{key}**: {desc}" for key, desc in KEY_DATA_PROMPT_MAP.items()])

    prompt = f"""
# VLOGA
Deluješ kot visoko natančen asistent za ekstrakcijo podatkov iz projektne dokumentacije (besedila in slik).

# NALOGA
Analiziraj priloženo dokumentacijo in izlušči VSE zahtevane podatke v en sam JSON objekt.

# IZHODNI FORMAT (STROGO)
Odgovori SAMO z enim JSON objektom, ki ima natančno tri ključe na najvišjem nivoju: "details", "metadata", in "key_data".

- **details**: Vsebuje seznama (array) za "eup" in "namenska_raba".
- **metadata**: Vsebuje objekt s polji "ime_projekta", "stevilka_projekta", "datum_projekta", "projektant".
- **key_data**: Vsebuje objekt z vsemi tehničnimi podatki.

Če katerega podatka ne najdeš, uporabi prazne vrednosti (prazen seznam `[]` za EUP/rabo, "Ni podatka" za metapodatke, "Ni podatka v dokumentaciji" za tehnične podatke).

---
# ZAHTEVANI PODATKI

## 1. Details (EUP in Namenska Raba)
- **eup**: Seznam VSEH oznak Enot Urejanja Prostora.
- **namenska_raba**: Seznam VSEH kratic podrobnejših namenskih rab (npr. SSe, IG).

## 2. Metadata (Osnovni Podatki)
- **ime_projekta**: Polno ime projekta.
- **stevilka_projekta**: Številka projekta.
- **datum_projekta**: Datum izdelave.
- **projektant**: Ime projektanta.

## 3. Key Data (Tehnični Podatki)
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
    "glavni_objekt": "Enostanovanjska hiša",
    "vrsta_gradnje": "Novogradnja",
    "faktor_zazidanosti_fz": "0.35",
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
        
        # Ključna sprememba: Uporaba robustnega čiščenja JSON niza
        clean_response = _clean_json_string(response.text)
        result = json.loads(clean_response)

        # Ključna sprememba: Normalizacija in zagotavljanje vseh ključev
        final_result = {
            "details": result.get("details", {"eup": [], "namenska_raba": []}),
            "metadata": result.get("metadata", {}),
            "key_data": result.get("key_data", {}),
        }
        
        # Zagotavljanje stringov za metapodatke
        for key in ["ime_projekta", "stevilka_projekta", "datum_projekta", "projektant"]:
            final_result["metadata"][key] = str(final_result["metadata"].get(key) or "Ni podatka")
            
        # Zagotavljanje stringov za vse key_data ključe
        for key in KEY_DATA_PROMPT_MAP.keys():
            final_result["key_data"][key] = str(final_result["key_data"].get(key) or "Ni podatka v dokumentaciji")


        return final_result

    except Exception as exc:
        print(f"⚠️ Napaka pri združeni AI ekstrakciji: {exc}.")
        # Povratni odgovor ob napaki
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
]
