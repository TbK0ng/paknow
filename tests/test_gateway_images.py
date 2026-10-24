"""Upload preparation: what the vision model is actually sent."""
from __future__ import annotations

import base64
import io

from PIL import Image

from paknow.gateway import _MAX_SIDE, _prepare_image, _strip_count


def _png(size: tuple[int, int], mode: str = "RGBA") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (255, 20, 20, 128) if mode == "RGBA" else (255, 20, 20)).save(
        buf, "PNG"
    )
    return buf.getvalue()


def _decode(uri: str) -> Image.Image:
    assert uri.startswith("data:image/jpeg;base64,")
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))


def test_tall_screenshot_becomes_full_resolution_strips() -> None:
    strips = [_decode(u) for u in _prepare_image(_png((400, 4000)))]

    assert len(strips) > 1
    # cut, not shrunk: width keeps its pixels and the strips cover every row
    assert {s.width for s in strips} == {400}
    assert sum(s.height for s in strips) == 4000
    assert all(max(s.size) <= _MAX_SIDE for s in strips)
    assert {s.mode for s in strips} == {"RGB"}  # alpha dropped, JPEG-encodable


def test_chart_screenshot_stays_under_the_provider_image_limit() -> None:
    # the real upload that used to 500: a 1516x5543 RGBA phone capture
    uris = _prepare_image(_png((1516, 5543)))

    assert all(len(u) < 32 * 1024 * 1024 for u in uris)


def test_small_image_is_one_strip() -> None:
    assert len(_prepare_image(_png((800, 600)))) == 1


def test_strip_count_matches_the_uris_without_encoding() -> None:
    # the read prompt needs N before the gateway encodes; the count must agree
    data = _png((400, 4000))
    uris = _prepare_image(data)

    assert _strip_count(data) == len(uris)
    assert sum(s.height for s in map(_decode, uris)) == 4000
    assert {s.width for s in map(_decode, uris)} == {400}
    assert _strip_count(_png((800, 600))) == 1


def test_extreme_tall_strip_count_stays_under_the_provider_cap() -> None:
    data = _png((1516, 9000))
    uris = _prepare_image(data)

    assert _strip_count(data) == len(uris) > 1
    assert all(len(u) < 32 * 1024 * 1024 for u in uris)
    assert sum(s.height for s in map(_decode, uris)) == 9000
