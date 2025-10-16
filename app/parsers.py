"""Utilities for working with PDF sources."""
from __future__ import annotations

import io
from time import perf_counter
from typing import List, Optional

from fastapi import HTTPException
from pypdf import PdfReader

from app.logging_config import get_logger

logger = get_logger(__name__)


def parse_pdf(file_bytes: bytes) -> str:
    start = perf_counter()
    try:
        pdf = PdfReader(io.BytesIO(file_bytes))
        text = "".join(page.extract_text() or "" for page in pdf.pages)
        cleaned = text.strip()
        duration = perf_counter() - start
        logger.info(
            "parse_pdf: extracted %d characters from %d pages in %.3fs",
            len(cleaned),
            len(pdf.pages),
            duration,
        )
        logger.debug("parse_pdf: text preview=%r", cleaned[:500])
        return cleaned
    except Exception as exc:  # pragma: no cover - depends on PDFs
        logger.exception("parse_pdf: failed to read PDF")
        raise HTTPException(status_code=400, detail=f"Napaka pri branju PDF: {exc}") from exc


def parse_page_string(page_str: str) -> List[int]:
    if not page_str:
        return []
    pages = set()
    for part in page_str.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                start, end = map(int, part.split('-'))
            except ValueError:
                continue
            if start > 0 and end >= start:
                pages.update(range(start - 1, end))
        else:
            try:
                page_num = int(part)
            except ValueError:
                continue
            if page_num > 0:
                pages.add(page_num - 1)
    return sorted(list(pages))


def convert_pdf_pages_to_images(pdf_bytes: bytes, pages_to_render_str: Optional[str]):
    import fitz  # type: ignore
    from PIL import Image

    start = perf_counter()
    images = []
    if not pages_to_render_str:
        logger.debug("convert_pdf_pages_to_images: no pages requested")
        return images
    page_numbers = parse_page_string(pages_to_render_str)
    if not page_numbers:
        logger.debug(
            "convert_pdf_pages_to_images: page string '%s' resolved to no pages",
            pages_to_render_str,
        )
        return images

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page_num in page_numbers:
            if 0 <= page_num < len(doc):
                page = doc.load_page(page_num)
                pix = page.get_pixmap(dpi=200)
                img_bytes = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_bytes))
                images.append(img)
        doc.close()
        duration = perf_counter() - start
        logger.info(
            "convert_pdf_pages_to_images: rendered %d/%d pages in %.3fs",
            len(images),
            len(page_numbers),
            duration,
        )
    except Exception as exc:  # pragma: no cover - depends on PDFs
        logger.warning("convert_pdf_pages_to_images: rendering failed: %s", exc)
    return images


__all__ = ["parse_pdf", "convert_pdf_pages_to_images", "parse_page_string"]
