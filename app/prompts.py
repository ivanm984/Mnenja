"""Prompt builders for Gemini interactions."""
from __future__ import annotations

from typing import Any, Dict, List


def build_prompt(project_text: str, zahteve: List[Dict[str, Any]], izrazi_text: str, uredba_text: str) -> str:
    zahteve_text = "".join(
        f"\nID: {z['id']}\nZahteva: {z['naslov']}\nBesedilo zahteve: {z['besedilo']}\n---" for z in zahteve
    )
    return f"""
Ti si strokovnjak za preverjanje skladnosti projektne dokumentacije z občinskimi prostorskimi akti (OPN/OPPN/PIP). Tvoja naloga je, da natančno in sistematično preveriš skladnost priloženega projekta s prostorskim aktom.

**NALOGA:**
Za vsako od spodnjih zahtev preveri skladnost priložene projektne dokumentacije. Delaj po naslednjem dvostopenjskem postopku:

**1. KORAK: ANALIZA BESEDILA**
Najprej poskusi odgovoriti na čim več zahtev z uporabo **samo tekstovnega dela** projektne dokumentacije. Poišči eksplicitne navedbe, kot so površine, tlorisne dimenzije in mere, faktorji, etažnost, število parkirnih mest itd.

**2. KORAK: CILJANA ANALIZA GRAFIK OZ. SLIK**
Ko končaš z analizo besedila, uporabi priložene slike oz. grafične priloge za dva namena:
    a) **Iskanje MANJKAJOČIH podatkov:** Za vse zahteve, kjer v besedilu nisi našel odgovora, natančno preglej grafike. Posebej pozoren bodi na:
        - **Odmike od parcelnih mej:** Te so skoraj vedno samo na situaciji.
        - **Višinske kote (terena, objekta, slemena):** Te so običajno prikazane na prerezih.
        - **Naklon strehe, višina kolenčnega zidu:** Prav tako na prerezih.
        - **Faktor zazidanosti (FZ) in faktor izrabe (FI):** Preveri, ali so na grafikah tabele s temi izračuni.
    b) **Preverjanje NESKLADIJ:** Če si v besedilu našel podatek (npr. "odmik od meje je 4.0 m"), preveri na grafiki (situaciji), ali je ta podatek skladen z vrisanim stanjem. Če odkriješ neskladje, to jasno navedi v obrazložitvi.

**RAZLAGA IZRAZOV (OPN):**
{izrazi_text or "Ni dodatnih izrazov."}

**UREDBA O RAZVRŠČANJU OBJEKTOV (KLJUČNE INFORMACIJE):**
{uredba_text or "Podatki niso na voljo."}
---
**ZAHTEVE:**
{zahteve_text}
---
**NAVODILA ZA ODGOVOR:**
Ko analiziraš zahteve in skladnost, pri podrobnih zahtevah (npr. členi 105, 106 itd...), v polje obrazložitev NUJNO kot prvo točko vnesi tudi tlorisne dimenzije stavbe oz. zunanje mere, višino in druge ključne značilnosti gradnje.
*Primer dobrega povzetka:* "Na podlagi dokumentacije je razvidno, da so tlorisne dimenzije predmetne stanovanjske hiše 10,0 x 8,0 m. Vertikalni gabarit objekta je Pritličje + Nadstropje (P+N) z višino kolenčnega zidu 1,20 m. Streha je načrtovana kot simetrična dvokapnica z naklonom 40 stopinj, krita z opečno kritino v rdeči barvi. Fasada je predvidena v svetli, beli barvi."
1.  Odgovori v obliki seznama (array) JSON objektov, brez kakršnegakoli drugega besedila ali markdown oznak (```json ... ```).
2.  Za VSAKO zahtevo ustvari en JSON objekt z naslednjimi polji:
    -   `"id"`: (string) ID zahteve (npr. "Z_0").
    -   `"obrazlozitev"`: (string) **IZJEMNO PODROBEN** opis ugotovitev, ki temelji na tvoji dvostopenjski analizi. Jasno loči, katere podatke si našel v besedilu in katere na grafiki. Če najdeš neskladje, ga poudari.
    -   `"evidence"`: (string) Natančna navedba vira: "Tehnično poročilo, stran X" ali "Grafika: Priloga C.2 - Situacija". Če si podatek potrdil iz obeh virov, navedi oba.
    -   `"skladnost"`: (string) Ena izmed trzech vrednosti: "Skladno", "Neskladno", ali "Ni relevantno".
    -   `"predlagani_ukrep"`: (string) Če je "Neskladno", opiši, kaj mora projektant storiti. Če je podatek manjkajoč, navedi, da ga je treba dodati. Če ukrep ni potreben, vrni "—".

3.  **POMEMBNO:** Če podatka ni ne v besedilu ne na grafikah, oceni kot "Neskladno" in v `predlagani_ukrep` zahtevaj dopolnitev dokumentacije.

4.  **!!! POSEBNO PRAVILO ZA SOGLASJA IN MNENJA ter ODMIKE !!!**
    Če zahteva omenja potrebo po pridobitvi soglasja (npr. soseda, mnenjedajalca), tvoja naloga NI preverjati, ali je bilo soglasje že pridobljeno. V takih primerih:
    -   V polje `"skladnost"` vedno vpiši **"Skladno"**.
    -   V polje `"predlagani_ukrep"` jasno navedi, katero soglasje je potrebno pridobiti.
    -   Pri navajanju odmikov v obrazložitev vnesi vse citirane odmike v dokumentaciji, tudi če so večji od 4m.

**Projektna dokumentacija (tekst):**
{project_text[:300000]}
---
**Projektna dokumentacija (grafične priloge):**
[Grafike so priložene in jih uporabi v drugem koraku analize za iskanje manjkajočih podatkov in preverjanje neskladij.]
""".strip()
