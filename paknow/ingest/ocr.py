"""OCR fallback for pages with no/degraded text layer (plan §5.2)."""
from __future__ import annotations

from functools import lru_cache

import numpy as np


@lru_cache(maxsize=1)
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def ocr_image(img: np.ndarray) -> str:
    result, _ = _engine()(img)
    if not result:
        return ""
    return "\n".join(line[1] for line in result)


def ocr_pdf_page(doc, pno: int, *, dpi: int = 200) -> str:
    """Render a PDF page and OCR it."""
    page = doc[pno]
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    return ocr_image(img)
