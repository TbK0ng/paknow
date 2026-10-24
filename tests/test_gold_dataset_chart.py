"""Chart gold dataset: 10 image items with hashes and a frozen sidecar.

Pure data checks; the chain, DB and Qdrant are never touched. Image files are
deliberately NOT required to exist — the images stay out of git, and the eval
records a missing image as ``skipped`` instead of failing.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

GOLD_DIR = Path(__file__).resolve().parents[1] / "evals" / "gold"
DATASET = GOLD_DIR / "chart_v1.json"
SIDECAR = GOLD_DIR / "chart_v1.sha256"

SOURCES = {"annotation", "swing", "axis", "unknown"}
SWING_KINDS = {"high", "low"}
STATES = {"trend_up", "trend_down", "trading_range", "unclear"}
ITEM_ID = re.compile(r"^chart-\d{2}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIR = "data/market/charts/"  # gitignored; bytes never enter the repo


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _price(value: object, label: str) -> None:
    assert isinstance(value, float) and math.isfinite(value), (label, value)
    assert round(value, 2) == value, (label, value)


def test_dataset_ships_ten_unique_chart_items():
    data = _load(DATASET)

    assert data["version"] == 1
    assert isinstance(data["frozen_at"], str) and data["frozen_at"]
    assert isinstance(data["retrieval_protocol"], str) and data["retrieval_protocol"]
    assert len(data["items"]) == 10
    ids = [item["id"] for item in data["items"]]
    assert len(set(ids)) == 10
    assert all(ITEM_ID.match(item_id) for item_id in ids)


def test_annotation_states_the_model_derived_provenance():
    # shortcut: expectations come from one chart_read@deepseek-flash output per
    # page, so the golden is partially model-derived and needs owner confirmation
    annotation = _load(DATASET)["annotation"]
    assert isinstance(annotation, str)
    assert "chart_read" in annotation
    assert "confirm" in annotation


def test_every_item_references_an_untracked_image_by_path_and_hash():
    for item in _load(DATASET)["items"]:
        image = item["image"]
        assert isinstance(image, str) and image.endswith(".png")
        assert not Path(image).is_absolute()
        assert ".." not in Path(image).parts
        assert image.startswith(IMAGE_DIR), image
        assert SHA256.match(item["image_sha256"]), item["id"]
        assert isinstance(item["question"], str) and item["question"]


def test_expected_levels_carry_a_finite_two_decimal_price_and_provenance():
    for item in _load(DATASET)["items"]:
        levels = item["expected_levels"]
        assert levels, item["id"]
        for level in levels:
            _price(level["price"], f"{item['id']} level price")
            assert level["source"] in SOURCES, level
            assert isinstance(level["strip"], int) and level["strip"] >= 1, level
            assert isinstance(level["basis"], str) and level["basis"], level


def test_expected_swings_and_state_use_the_chart_read_enums():
    for item in _load(DATASET)["items"]:
        for swing in item["expected_swings"]:
            assert swing["kind"] in SWING_KINDS, swing
            _price(swing["price"], f"{item['id']} swing price")
            assert isinstance(swing["strip"], int) and swing["strip"] >= 1, swing
            assert isinstance(swing["basis"], str) and swing["basis"], swing
        assert item["expected_state"] in STATES, item["id"]
        assert all(isinstance(text, str) for text in item["unreadable"]), item["id"]


def test_sidecar_matches_the_dataset_bytes():
    digest = hashlib.sha256(DATASET.read_bytes()).hexdigest()
    sidecar = SIDECAR.read_text(encoding="utf-8").split()

    assert sidecar, "sidecar is empty"
    assert sidecar[0] == digest
    if len(sidecar) > 1:  # sha256sum format: "<digest>  <filename>"
        assert sidecar[1] == DATASET.name
