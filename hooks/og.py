"""Per-page social metadata (og/twitter) without the `social` plugin.

The plugin needs Pillow + cairosvg and renders per-page images from a Latin-only
font, so Chinese titles come out as tofu on CI. A single committed cover plus
accurate text tags is deterministic and offline-safe.
"""
from __future__ import annotations

import html
from pathlib import Path

COVER = "assets/images/og-cover.png"
CARDS = "assets/images/og"


def _card(page, config) -> str | None:
    """``assets/images/og/<stem>.png`` when generated, else ``None``."""
    docs = Path(config.docs_dir)
    if not docs.is_absolute():
        docs = Path(config.config_file_path).parent / docs
    rel = f"{CARDS}/{Path(page.file.src_path).stem}.png"
    return rel if (docs / rel).is_file() else None


def on_post_page(output: str, page, config) -> str:
    meta = page.meta or {}
    base = (config.site_url or "").rstrip("/") + "/"
    if page.is_homepage:
        title = config.site_name
    else:
        title = f"{page.title} - {config.site_name}"
    desc = meta.get("description") or config.site_description or ""
    url = base + (page.url or "")
    image = base + (_card(page, config) or COVER)

    def esc(value: str) -> str:
        return html.escape(str(value), quote=True)

    tags = [
        ("property", "og:type", "website"),
        ("property", "og:site_name", config.site_name),
        ("property", "og:title", title),
        ("property", "og:description", desc),
        ("property", "og:url", url),
        ("property", "og:image", image),
        ("property", "og:image:width", "1200"),
        ("property", "og:image:height", "630"),
        ("name", "twitter:card", "summary_large_image"),
        ("name", "twitter:title", title),
        ("name", "twitter:description", desc),
        ("name", "twitter:image", image),
    ]
    block = "\n".join(
        f'<meta {attr}="{esc(key)}" content="{esc(value)}">'
        for attr, key, value in tags
    )
    if "</head>" not in output:
        return output
    return output.replace("</head>", block + "\n</head>", 1)
