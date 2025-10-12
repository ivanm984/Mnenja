"""Gemini integration helpers."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from PIL import Image

import google.generativeai as genai

from .config import API_KEY, GEN_CFG, MODEL_NAME


genai.configure(api_key=API_KEY)


def call_gemini_for_details(project_text: str, images: List[Image.Image]) -> Dict[str, Optional[List[str]]]:
    prompt = f"""
    Analiziraj spodnje besedilo iz projektne dokumentacije. Če tega ne najdeš poglej slike. Tvoja naloga je najti dve informacij:
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
        response = model.generate_content(content_parts)
        clean_response = re.sub(r"```(json)?", "", response.text, flags=re.IGNORECASE).strip()
        details = json.loads(clean_response)
        eup_list = [str(e) for e in details.get("eup", []) if e]
        raba_list = [str(r).upper() for r in details.get("namenska_raba", []) if r]
        return {"eup": eup_list, "namenska_raba": raba_list}
    except Exception as exc:  # pragma: no cover - external API
        print(f"⚠️ Napaka pri AI Detektivu: {exc}.")
        return {"eup": [], "namenska_raba": []}


def call_gemini_for_metadata(project_text: str) -> Dict[str, str]:
    prompt = f"""
    Analiziraj priloženo besedilo projektne dokumentacije in izlušči naslednje podatke:
    1.  **ime_projekta**: Polno ime ali naziv projekta (npr. "Novogradnja enostanovanjske stavbe").
    2.  **stevilka_projekta**: Identifikacijska številka projekta.
    3.  **datum_projekta**: Datum izdelave dokumentacije.
    4.  **projektant**: Ime podjetja ali odgovornega projektanta.

    Odgovori SAMO v JSON formatu. Če katerega od podatkov ne najdeš, za vrednost uporabi "Ni podatka".
    Primer odgovora:
    {{
        "ime_projekta": "Prizidek in rekonstrukcija objekta",
        "stevilka_projekta": "P-123/2024",
        "datum_projekta": "Maj 2024",
        "projektant": "Projekt d.o.o."
    }}

    Besedilo dokumentacije:
    ---
    {project_text[:20000]}
    ---
    """
    try:
        model = genai.GenerativeModel(MODEL_NAME, generation_config={"response_mime_type": "application/json"})
        response = model.generate_content(prompt)
        clean_response = re.sub(r"```(json)?", "", response.text, flags=re.IGNORECASE).strip()
        metadata = json.loads(clean_response)
        return metadata
    except Exception as exc:  # pragma: no cover
        print(f"⚠️ Napaka pri AI Arhivistu: {exc}.")
        return {
            "ime_projekta": "Ni podatka",
            "stevilka_projekta": "Ni podatka",
            "datum_projekta": "Ni podatka",
            "projektant": "Ni podatka",
        }


def call_gemini_for_key_data(project_text: str, images: List[Image.Image]) -> Dict[str, Any]:
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
    prompt_items = "\n".join([f"{i+1}. **{key}**: {desc}" for i, (key, desc) in enumerate(KEY_DATA_PROMPT_MAP.items())])
    prompt = f"""
    Iz priložene projektne dokumentacije (besedila in slik - grafičnega dela) natančno izlušči naslednje ključne podatke projekta. Iščite numerične vrednosti in dimenzije!

    **Posebej bodite pozorni na informacije, ki se običajno nahajajo samo v grafikah/situaciji (odmiki, kote, dimenzije).**

    Odgovori SAMO v JSON formatu, pri čemer so vsi podatki nizi (string). Če podatka ni mogoče najti (ne v besedilu ne na slikah), uporabi vrednost "Ni podatka v dokumentaciji".

    ZAHTEVANI PODATKI:
    {prompt_items}

    Besedilo dokumentacije: --- {project_text[:40000]} ---
    """
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        content_parts = [prompt]
        if images:
            content_parts.extend(images)
        response = model.generate_content(content_parts)
        clean_response = re.sub(r"```(json)?", "", response.text, flags=re.IGNORECASE).strip()
        key_data = json.loads(clean_response)
        final_data = {key: key_data.get(key, "Ni podatka v dokumentaciji") for key in KEY_DATA_PROMPT_MAP.keys()}
        return final_data
    except Exception as exc:  # pragma: no cover
        print(f"⚠️ Napaka pri AI Ekstraktorju: {exc}.")
        return {key: "Napaka pri ekstrakciji" for key in KEY_DATA_PROMPT_MAP.keys()}


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
    "call_gemini_for_details",
    "call_gemini_for_metadata",
    "call_gemini_for_key_data",
    "call_gemini",
    "parse_ai_response",
]
