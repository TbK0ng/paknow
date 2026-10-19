"""Extract bilingual term pairs from chapter titles (中文（English）对齐格式).

Deterministic (no LLM): chapter paths in this corpus carry aligned bilingual
titles. New pairs not covered by ``configs/terms.yaml`` are appended with an
``# auto: 双语标题提取`` marker for review.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import EvidenceUnitRow

_PAIR_RE = re.compile(r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9·、，/（()）\- ]{1,40}?)[（(]([A-Za-z][A-Za-z0-9 ,&'\-/]{3,60})[)）]")

_TERMS_PATH = Path(__file__).resolve().parent.parent / "configs" / "terms.yaml"


def main() -> int:
    settings.ensure_dirs()
    init_db()
    with SessionLocal() as s:
        paths = [row[0] for row in s.query(EvidenceUnitRow.chapter_path).distinct().all()]
    titles: set[str] = set()
    for p in paths:
        for t in p or []:
            if t:
                titles.add(t)
    data = yaml.safe_load(_TERMS_PATH.read_text(encoding="utf-8")) or []
    known = {a.lower() for t in data for a in t.get("aliases", [])}
    known |= {t["canonical"].lower() for t in data}

    new_pairs: list[tuple[str, str, str]] = []
    for title in titles:
        for m in _PAIR_RE.finditer(title):
            zh, en = m.group(1).strip(" ·-—"), m.group(2).strip()
            zh = zh.lstrip("与和及/")
            if len(zh) < 2 or len(zh) > 24 or len(en) < 3:
                continue
            if zh.lower() in known and en.lower() in known:
                continue
            new_pairs.append((zh, en, title))

    # keep the shortest clean pair per (zh, en)
    dedup: dict[tuple[str, str], str] = {}
    for zh, en, title in new_pairs:
        key = (zh, en)
        if key not in dedup or len(title) < len(dedup[key]):
            dedup[key] = title

    if not dedup:
        print("no new pairs", flush=True)
        return 0
    print(f"new pairs: {len(dedup)}", flush=True)
    with _TERMS_PATH.open("a", encoding="utf-8") as f:
        f.write("\n# ---- auto: 双语标题提取 (scripts/extract_bilingual_terms.py) ----\n")
        for (zh, en), title in dedup.items():
            en_id = "auto_" + re.sub(r"[^a-z0-9]+", "_", en.lower()).strip("_")
            title_q = title.replace('"', "「").replace('"', "」")
            f.write(f"- canonical: {en_id}\n")
            f.write(f"  aliases: [\"{zh}\", \"{en}\"]\n")
            f.write(f"  context: \"出自章节: {title_q}\"\n")
    print(f"appended {len(dedup)} terms to terms.yaml", flush=True)
    return len(dedup)


if __name__ == "__main__":
    sys.exit(0 if main() else 0)
