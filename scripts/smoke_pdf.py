"""Smoke test: PDF parse a small range; verify page 93 / figure 4.5 example."""
from paknow.config import settings
from paknow.db import init_db
from paknow.ingest.pipeline import ingest_pdf

settings.ensure_dirs()
init_db()

path = "Reading Price Charts Bar by Bar.pdf"
# parse pages 88..96 (1-based 89..96) around the 图 4.5 example (physical p93)
row, result = ingest_pdf(path, page_range=(85, 97))

print(f"doc_id      = {row.doc_id}")
print(f"page_count  = {result.page_count} (total)")
print(f"parsed units = {len(result.evidence_units)}")
print(f"figures      = {len(result.figures)}")
print(f"ocr_pages    = {result.ocr_pages}")
print(f"page_labels (sample) = {dict(list(result.page_labels.items())[:6])}")

print("\n--- units on pdf_page 93 ---")
for u in result.evidence_units:
    if u.locator.pdf_page_1based == 93:
        print(
            f"[{u.block_type}] printed={u.locator.printed_page_label!r} "
            f"chapter={u.locator.chapter_path} "
            f"fig_label={u.locator.figure_label!r} text={u.text[:70]!r}"
        )
