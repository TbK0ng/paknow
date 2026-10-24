"""End-to-end chart analysis: render a PDF page as an uploaded chart, run the vision flow."""
import io

import fitz
from PIL import Image

from paknow.config import settings
from paknow.db import init_db
from paknow.ingest.manifest import resolve_source
from paknow.workflows.graph import run_query

settings.ensure_dirs()
init_db()

# render 逐根 K 线 图 4.5 (physical page 93) as the "uploaded chart"
doc = fitz.open(str(resolve_source("Reading Price Charts Bar by Bar.pdf")))
pix = doc[92].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

buf = io.BytesIO()
img.save(buf, format="PNG")
png = buf.getvalue()
doc.close()

q = "这张图是什么形态？下一步应该观察什么？"
print("=" * 70, flush=True)
print(f"CHART QUERY: {q}  (image {len(png)} bytes)", flush=True)
result = run_query(q, images=[png])
print(f"intents: {result['intent']}", flush=True)
print(f"routes : {result['routes']}", flush=True)
print(f"hits   : {len(result['hits'])}", flush=True)
ans = result.get("answer") or {}
print("-" * 70, flush=True)
print("ANSWER:\n" + (ans.get("answer") or "<none>")[:1500], flush=True)
print("-" * 70, flush=True)
print("uncertainty:", ans.get("uncertainty"), flush=True)
