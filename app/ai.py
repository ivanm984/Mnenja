"""Gemini integration helpers."""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any, Dict, List

from fastapi import HTTPException
from PIL import Image

import google.generativeai as genai

from app.logging_config import get_logger

from .config import API_KEY, GEN_CFG, MODEL_NAME, EXTRACTION_MODEL_NAME

logger = get_logger(__name__)

genai.configure(api_key=API_KEY)


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
    start = perf_counter()
    logger.info(
        "call_gemini_for_initial_extraction: starting (chars=%d, images=%d)",
        len(project_text or ""),
        len(images or []),
    )
    logger.debug(
        "call_gemini_for_initial_extraction: prompt preview=%r",
        prompt[:500],
    )

    try:
        model = genai.GenerativeModel(
            EXTRACTION_MODEL_NAME,
            generation_config={"response_mime_type": "application/json"},
        )
        content_parts = [prompt]
        if images:
            content_parts.extend(images)

        response = model.generate_content(content_parts)
        clean_response = re.sub(r"```(json)?", "", response.text, flags=re.IGNORECASE).strip()
        logger.debug(
            "call_gemini_for_initial_extraction: raw response preview=%s",
            clean_response[:500],
        )
        result = json.loads(clean_response)

        final_result = {
            "details": result.get("details", {"eup": [], "namenska_raba": []}),
            "metadata": result.get("metadata", {}),
            "key_data": result.get("key_data", {}),
        }
        duration = perf_counter() - start
        logger.info(
            "call_gemini_for_initial_extraction: completed in %.2fs (eup=%d, namenska=%d, key_fields=%d)",
            duration,
            len(final_result["details"].get("eup", [])),
            len(final_result["details"].get("namenska_raba", [])),
            len(final_result["key_data"]),
        )
        return final_result

    except Exception as exc:
        logger.exception("call_gemini_for_initial_extraction: failed")
        return {
            "details": {"eup": [], "namenska_raba": []},
            "metadata": {
                "ime_projekta": "Napaka",
                "stevilka_projekta": "Napaka",
                "datum_projekta": "Napaka",
                "projektant": "Napaka",
            },
            "key_data": {key: "Napaka pri ekstrakciji" for key in KEY_DATA_PROMPT_MAP.keys()},
        }


def call_gemini(prompt: str, images: List[Image.Image]) -> str:
    start = perf_counter()
    logger.info(
        "call_gemini: starting (prompt_chars=%d, images=%d)",
        len(prompt or ""),
        len(images or []),
    )
    logger.debug("call_gemini: prompt preview=%r", prompt[:500])
    try:
        model = genai.GenerativeModel(MODEL_NAME, generation_config=GEN_CFG)
        content_parts = [prompt]
        content_parts.extend(images)
        response = model.generate_content(content_parts)
        if not response.parts:
            reason = response.candidates[0].finish_reason if response.candidates else "NEZNAN"
            raise RuntimeError(f"Gemini ni vrnil veljavnega odgovora. Razlog: {reason}")
        text = "".join(part.text for part in response.parts)
        duration = perf_counter() - start
        logger.info(
            "call_gemini: completed in %.2fs (response_chars=%d)",
            duration,
            len(text),
        )
        logger.debug("call_gemini: response preview=%r", text[:500])
        return text
    except Exception as exc:  # pragma: no cover
        logger.exception("call_gemini: failed")
        raise HTTPException(status_code=500, detail=f"Gemini napaka (Analitik): {exc}") from exc


def parse_ai_response(response_text: str, expected_zahteve: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    clean = re.sub(r"```(json)?", "", response_text, flags=re.IGNORECASE).strip()
    logger.debug("parse_ai_response: raw response preview=%r", clean[:500])
    try:
        data = json.loads(clean)
    except json.JSONDecodeError as exc:
        logger.exception("parse_ai_response: invalid JSON")
        raise HTTPException(
            status_code=500,
            detail=f"Neveljaven JSON iz AI: {exc}\n\nOdgovor:\n{response_text[:500]}",
        ) from exc

    if not isinstance(data, list):
        logger.error("parse_ai_response: JSON root is not a list")
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

    logger.info(
        "parse_ai_response: mapped %d results (expected=%d)",
        len(results_map),
        len(expected_zahteve),
    )
    return results_map


__all__ = [
    "call_gemini_for_initial_extraction",
    "call_gemini",
    "parse_ai_response",
]
