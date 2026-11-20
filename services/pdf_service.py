"""PDF validation and page rendering."""
from __future__ import annotations

import pymupdf
from PIL import Image


def pdf_to_images(data: bytes, max_pages: int = 40) -> list[Image.Image]:
    """Validate a PDF and render its pages for Gemini image understanding."""
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
        if document.is_encrypted or document.page_count == 0:
            raise ValueError("The PDF is encrypted or contains no pages.")
        if document.page_count > max_pages:
            raise ValueError(f"PDF has {document.page_count} pages; limit is {max_pages}.")
        images = []
        for page in document:
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            images.append(Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples))
        return images
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise ValueError("This file is not a readable PDF.") from exc


def page_count(data: bytes) -> int:
    """Return the number of pages in a readable PDF."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.is_encrypted or document.page_count == 0:
                raise ValueError("The PDF is encrypted or contains no pages.")
            return document.page_count
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise ValueError("This file is not a readable PDF.") from exc


def render_page(data: bytes, page_number: int) -> Image.Image:
    """Render one 1-based PDF page as an image for the Streamlit viewer."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if page_number < 1 or page_number > document.page_count:
                raise ValueError("Page number is outside this PDF.")
            page = document[page_number - 1]
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
            return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise ValueError("This file is not a readable PDF.") from exc
