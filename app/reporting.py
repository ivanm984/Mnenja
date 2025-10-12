"""Utilities for creating the Word compliance report."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.shared import Inches


def generate_word_report(
    zahteve: List[Dict[str, Any]],
    results_map: Dict[str, Dict[str, Any]],
    metadata: Dict[str, str],
    output_path: str,
) -> str:
    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    new_width, new_height = section.page_height, section.page_width
    section.page_width = new_width
    section.page_height = new_height
    margin = Inches(0.7)
    section.top_margin = margin
    section.bottom_margin = margin
    section.left_margin = margin
    section.right_margin = margin

    doc.add_paragraph(f"Datum analize: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    doc.add_heading("Poročilo o skladnosti z občinskimi prostorskimi akti", level=1)

    neskladja = [
        zahteva.get("naslov", "Neznan pogoj")
        for zahteva in zahteve
        if results_map.get(zahteva["id"], {}).get("skladnost") == "Neskladno"
    ]
    sklepni_status = "NESKLADNA" if neskladja else "SKLADNA"

    doc.add_heading("Sklepna ugotovitev", level=2)
    p = doc.add_paragraph()
    p.add_run("Gradnja po projektu '")
    p.add_run(metadata.get("ime_projekta", "Ni podatka")).italic = True
    p.add_run(
        f"', s številko projekta '{metadata.get('stevilka_projekta', 'Ni podatka')}', "
        f"datumom '{metadata.get('datum_projekta', 'Ni podatka')}' in projektantom "
        f"'{metadata.get('projektant', 'Ni podatka')}', je glede na predloženo dokumentacijo ocenjena kot "
    )
    p.add_run(f"{sklepni_status}").bold = True
    p.add_run(" s prostorskim aktom.")

    if neskladja:
        p = doc.add_paragraph("Ugotovljena so bila neskladja v naslednjih točkah oziroma členih:")
        for tocka in neskladja:
            doc.add_paragraph(tocka, style="List Bullet")
    doc.add_paragraph()

    kategorije = {}
    for zahteva in zahteve:
        kategorija = zahteva.get("kategorija", "Ostalo")
        kategorije.setdefault(kategorija, []).append(zahteva)

    preferred_order = [
        "Splošni prostorski izvedbeni pogoji (PIP)",
        "Podrobni prostorski izvedbeni pogoji (PIP NRP)",
        "Podrobni prostorski izvedbeni pogoji (PIP NRP) - Napotilo",
        "Posebni prostorski izvedbeni pogoji (PIP EUP)",
        "Skladnost z Prilogo 1 (Enostavni/Nezahtevni objekti)",
    ]
    final_order = [cat for cat in preferred_order if cat in kategorije]
    final_order.extend([cat for cat in kategorije if cat not in final_order])

    for kategorija in final_order:
        doc.add_heading(kategorija, level=2)
        table = doc.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = "Pogoj"
        hdr_cells[1].text = "Ugotovitve, obrazložitev in dokazila"
        hdr_cells[2].text = "Skladnost in ukrepi"
        for cell in hdr_cells:
            cell.paragraphs[0].runs[0].font.bold = True

        for zahteva in kategorije[kategorija]:
            row_cells = table.add_row().cells
            result = results_map.get(zahteva["id"], {})

            pogoj_p = row_cells[0].paragraphs[0]
            pogoj_p.add_run(zahteva.get("naslov", "Brez naslova")).bold = True
            pogoj_p.add_run(f"\n\n{zahteva.get('besedilo', 'Brez besedila')}")

            obrazlozitev_p = row_cells[1].paragraphs[0]
            obrazlozitev_p.add_run("Obrazložitev:\n").bold = True
            obrazlozitev_p.add_run(result.get("obrazlozitev", "—"))
            obrazlozitev_p.add_run("\n\nDokazilo v dokumentaciji:\n").bold = True
            obrazlozitev_p.add_run(result.get("evidence", "—"))

            skladnost_p = row_cells[2].paragraphs[0]
            skladnost_p.add_run("Skladnost:\n").bold = True
            skladnost_p.add_run(result.get("skladnost", "Neznano"))
            ukrep_text = result.get("predlagani_ukrep", "—")
            if ukrep_text and ukrep_text != "—":
                skladnost_p.add_run("\n\nPredlagani ukrepi:\n").bold = True
                skladnost_p.add_run(ukrep_text)

    doc.save(output_path)
    return str(Path(output_path).resolve())


__all__ = ["generate_word_report"]
