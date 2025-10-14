"""Prompt builders for compliance checking of project documentation (OPN/OP).
Improved for clarity, determinism, and strict JSON output.
"""
from __future__ import annotations
from typing import Any, Dict, List

def build_prompt(
    project_text: str,
    zahteve: List[Dict[str, Any]],
    izrazi_text: str,
    uredba_text: str
) -> str:
    """
    Zgradi navodila za LLM, da preveri skladnost projektne dokumentacije
    z zahtevami prostorskega akta po dvofaznem postopku (besedilo -> grafike)
    in vrne STROGO validen JSON array objektov.
    """
    zahteve_text = "".join(
        f"\nID: {z['id']}\nZahteva: {z['naslov']}\nBesedilo zahteve: {z['besedilo']}\n---"
        for z in zahteve
    )

    return f"""\

# VLOGA IN CILJ
Deluješ kot **nepristranski prostorski strokovnjak** za preverjanje skladnosti projektne dokumentacije
z lokalnim prostorskim aktom (OPN/OP ipd.), v skladu s slovensko zakonodajo in prakso. Tvoja naloga je, da **za vsako zahtevo** natančno
pridobiš ustrezne podatke, presodiš skladnost z zahtevo, navedeš **dokaze** (kjer si podatek našel) in podaš **jasen ukrep**,
če je prisotno neskladje ali manjkajoč podatek.

# KLJUČNA PRAVILA
- **Brez ugibanja**: če podatka ni v besedilu *niti* na grafičnih prilogah, obravnavaj kot **"Neskladno"**
  in v **predlagani_ukrep** zahtevaj *dopolnitev dokumentacije* (povej točno kaj).
- **En zaključek na zahtevo**: "Skladno", "Neskladno" ali "Ni relevantno" (samo če je jasno, da zahteva
  ne velja za obravnavani primer).
- **Dokazi (evidence)**: navedi konkretna mesta (npr. "Tehnično poročilo, str. 12" ali
  "Grafika G2 – Situacija"). Če uporabiš oba vira (tekst in grafiko), navedi oba.
- **Natančnost pred kratkostjo**: obrazložitev naj bo kvantitativna in specifična. Vedno navajaj zahtevane vrednosti iz prostorskega akta in projektirane vrednosti iz dokumentacije ter jih primerjaj (npr. 'Zahtevan FZ je največ 0.4, projektiran FZ je 0.38.')."
- **Doslednost izrazov**: uporabi terminologijo iz OPN/OP in razlage izrazov (glej spodaj).
- **Format izhoda**: izpiši **izključno** JSON array brez kakršnegakoli dodatnega besedila ali oznak.
- **Konflikt med viri**: Če pride do neskladja med podatki v besedilu in na grafičnih prilogah (npr. drugačen odmik, višina ali FZ), imajo **podatki na grafičnih prilogah prednost**, 
   saj veljajo za natančnejši prikaz dejanskega stanja. V obrazložitvi jasno navedi obe vrednosti in pojasni, katero si uporabil za presojo.

# DVOFAZNI POSTOPEK
**1) Analiza besedila**
Najprej izčrpno preglej projektno dokumentacijo (tekst) in poskušaj odgovoriti na čim več zahtev.
V obrazložitvi vedno zapiši *katera* dejstva so bila najdena v besedilu (citiraj povzetke z merami/parametri).

**2) Ciljana analiza grafik**
Za vse, kjer podatki manjkajo ali so dvomljivi, preglej grafične priloge:
- **Odmiki od parcelnih mej** (pogosto le na situaciji),
- **Višinske kote (terena, objekta, slemena)** (prerezi),
- **Naklon strehe, višina kolenčnega zidu** (prerezi),
- **FZ, FI** (tabele in bilance na načrtih).
Če grafika potrdi ali ovrže besedilo, to izrecno zapiši. Odkrita neskladja jasno označi.

# POSEBNA PRAVILA (jih ne razkrivaš v odgovoru)
- Če zahteva govori o **potrebi po soglasju/mnenju** za poseg na varovanih območjih, varovalnih pasov, odmikih od parcelne meje oz. meje soseda (brez preverjanja ali je že pridobljeno):
  - `"skladnost"` = **"Skladno"**,
  - `"predlagani_ukrep"` = navedi *katero soglasje/mnenje* je treba pridobiti.
- Pri **odmikih** v obrazložitvi navedi **vse citirane odmike** iz dokumentacije, tudi če presegajo 4 m (ne filtriraj vrednosti).
- Če podrobni prostorski pogoji govorijo o samo določenem faktorju (zazidanosti, izrabe itd.), lahko projekt upošteva samo ta navedeni faktor, četudi je v splošnih določiih navedenih več faktorjev. Zahteva je skladna, če je zahtevani faktor v podrobnih določilih skladen.
- Zahteve za požarno varnost so splošne in lahko projekt vsebuje samo določilo, da se bodo obravnavali v projektu za izvedbo, zahteva je skladna.
- Če se pojavijo različni podatki v projektu in je vseeno možno oceniti skladnost zahteve jo izvedi, pri čemer uporabi tisto vrednost, ki je večja (recimo da je po enem izračunu FZ=0,4, po drugem pa 0,38, vzami za presojo višjo vrednost).

# DEFINICIJE IN PRAVNI OKVIR
**Razlaga izrazov (OPN):**
{izrazi_text or "Ni dodatnih izrazov."}

**Uredba o razvrščanju objektov (ključne informacije):**
{uredba_text or "Podatki niso na voljo."}

# ZAHTEVE (vsaka mora biti obravnavana natanko enkrat)
{zahteve_text}

# VHODNI PODATKI
**Projektna dokumentacija – BESEDILO (do 300.000 znakov):**
{project_text[:300000]}

**Projektna dokumentacija – GRAFIČNE PRILOGE:**
[Grafike so priložene. Uporabi jih v 2. koraku za manjkajoče podatke in preverjanje neskladij.]

# IZPIS (STROGO) – JSON array objektov
Za **vsako** zahtevo vrni en JSON objekt z natančno temi polji:

- "id": string — ID zahteve (npr. "Z_0").
- "obrazlozitev": string — **zelo podrobna** obrazložitev. Sledi strukturi:
  1. **Povzetek zahteve:** Na kratko povzemi, kaj zahteva preverja.
  2. **Najdbe v besedilu:** Citiraj relevantne vrednosti in navedbe iz besedilnega dela dokumentacije.
  3. **Najdbe na grafikah:** Citiraj relevantne vrednosti, pridobljene iz grafičnih prilog.
  4. **Zaključek presoje:** Jasna primerjava med zahtevo in najdenimi vrednostmi, ki vodi do zaključka o skladnosti."
- "evidence": string — natančna navedba virov (npr. "Tehnično poročilo, str. 12; G2 – Situacija").
- "skladnost": string — **ena** od vrednosti: "Skladno" | "Neskladno" | "Ni relevantno".
- "predlagani_ukrep": string — če je "Neskladno", opiši *konkreten* dopolnitveni/korektivni ukrep; sicer "—".

# PRAVILA ZA NEPOPOLNE PODATKE
- Če zahteva *zahteva* specifične podatke (npr. odmiki, višinske kote, FZ/FI) in teh podatkov ni v besedilu
  **niti** na grafikah, nastavi:
  - "skladnost" = "Neskladno"
  - "predlagani_ukrep" = npr. "Dopolniti dokumentacijo z [manjkajoči podatek] in ustrezno grafično prilogo."
  - V "obrazlozitev" jasno zapiši, kje si poskušal najti podatek in da ga ni.

# SAMOPREGLED FORMATOV (pred oddajo)
- Preveri, da:
  1) so zajeti **vsi** `id` iz seznama zahtev in **noben** dodatni,
  2) je "skladnost" pri vseh **ena** izmed dovoljenih treh vrednosti,
  3) je "evidence" **neprazno** in smiselno,
  4) je "predlagani_ukrep" = "—" kadar ni neskladja.

# PRIMER ENE POSTAVKE (zgolj kot vzorec strukture, NE kopiraj vsebine):
[
  {{
    "id": "Z_0",
    "obrazlozitev": "Na str. 12 tehničnega poročila je navedeno ... Na prerezu P2 je vidna višinska kota slemena ...",
    "evidence": "Tehnično poročilo, str. 12; P2 – Prerez; G2 – Situacija",
    "skladnost": "Skladno",
    "predlagani_ukrep": "—"
  }}
]

# KONČNI IZPIS
Vrni **IZKLJUČNO** JSON array (brez uvodnega ali zaključnega besedila, brez markdown oznak).
""".strip()
