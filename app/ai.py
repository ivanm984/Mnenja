def call_gemini_for_initial_extraction(project_text: str, images: List[Image.Image]) -> Dict[str, Any]:
    """
    Izvede en sam klic za ekstrakcijo vseh začetnih podatkov po dogovorjeni strukturi.
    """
    # --- SPREMEMBA: Posodobljen slovar po dogovoru ---
    KEY_DATA_PROMPT_MAP = {
        "naziv_gradnje": "Celoten naziv gradnje, ki združuje vrsto gradnje in opis objekta (npr. 'Novogradnja enostanovanjske stavbe').",
        "glavni_objekt": "Kratek, jedrnat opis glavnega objekta.",
        "pomozni_objekti": "Opis vseh pomožnih, nezahtevnih ali enostavnih objektov, ki so del projekta.",
        "parcela_in_ko": "Navedba vseh parcelnih številk in ime katastrske občine.",
        "dimenzije_objektov": "Tlorisne dimenzije glavnega objekta in morebitnih pomožnih objektov.",
        "etaznost": "Etažnost glavnega objekta (npr. K+P+M ali P+1).",
        "visinski_gabariti": "Ključni višinski gabariti: višina slemena, višina kapi/venca in višina kolenčnega zidu.",
        "streha_naklon_smer_kritina": "Združen opis strehe: naklon v stopinjah, smer slemena in vrsta ter barva kritine.",
        "barva_fasade": "Opis materialov in barve fasade.",
        "odmiki": "Najpomembnejši odmiki objekta od parcelnih mej ali drugih objektov.",
        "parkirna_mesta": "Navedba števila zagotovljenih ali potrebnih parkirnih mest (PM).",
        "prikljucki_gji": "Podroben opis načina priključitve objekta na gospodarsko javno infrastrukturo (voda, elektrika, kanalizacija, telekomunikacije).",
        "bruto_etazna_povrsina": "Vrednost bruto etažne površine (BEP) v m².",
        "faktorji_in_ozelenitev": "Vrednosti za Faktor Zazidanosti (FZ), Faktor Izrabe (FI) in Faktor Zelenih Površin (FZP).",
    }
    # --- KONEC SPREMEMBE ---

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
