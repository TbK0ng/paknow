"""Gold dataset v2: 60 stratified questions with evidence anchors and answer points.

These tests are pure data checks; they never open the database or Qdrant. The
answer-freeze guarantee is that the twenty v1 items survive field-for-field, so
results from the retrieval round stay comparable.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

GOLD_DIR = Path(__file__).resolve().parents[1] / "evals" / "gold"
V2_DATASET = GOLD_DIR / "graph_expansion_v2.json"
V2_SIDECAR = GOLD_DIR / "graph_expansion_v2.sha256"
V1_DATASET = GOLD_DIR / "graph_expansion.json"

FROZEN_FIELDS = ("question", "expanded_queries", "expected_evidence", "expected_relations")
STRATA = {
    "encyclopedia": 20,
    "mechanism": 20,
    "exception": 10,
    "conditioned_claim": 10,
}
HEX_ID = re.compile(r"^[0-9a-f]+$")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_dataset_ships_sixty_items_in_four_strata():
    data = _load(V2_DATASET)

    assert data["version"] == 2
    assert data["queries_per_item"] == 6
    assert len(data["items"]) == 60

    counts: dict[str, int] = {}
    for item in data["items"]:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    assert counts == STRATA


def test_original_twenty_items_survive_field_for_field():
    v1 = {item["id"]: item for item in _load(V1_DATASET)["items"]}
    v2 = {item["id"]: item for item in _load(V2_DATASET)["items"]}

    assert len(v1) == 20
    for item_id, old in v1.items():
        assert item_id in v2, item_id
        for field in FROZEN_FIELDS:
            assert v2[item_id][field] == old[field], (item_id, field)


def test_every_item_has_a_unique_id_and_six_frozen_queries():
    items = _load(V2_DATASET)["items"]

    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids))
    for item in items:
        assert item["id"]
        assert item["question"]
        assert len(item["expanded_queries"]) == 6
        assert item["expanded_queries"][0] == item["question"]
        assert 1 <= len(item["expected_evidence"]) <= 4


def test_evidence_ids_are_hex_strings():
    for item in _load(V2_DATASET)["items"]:
        for evidence_id in item["expected_evidence"]:
            assert isinstance(evidence_id, str)
            assert HEX_ID.match(evidence_id), (item["id"], evidence_id)
            assert len(evidence_id) == 32


def test_new_items_have_authoring_anchors_and_bound_answer_points():
    v1_ids = {item["id"] for item in _load(V1_DATASET)["items"]}
    items = [item for item in _load(V2_DATASET)["items"] if item["id"] not in v1_ids]

    assert len(items) == 40
    for item in items:
        anchors = item.get("anchors")
        assert anchors, item["id"]
        assert all(HEX_ID.match(a) for a in anchors)
        assert set(item["expected_evidence"]) <= set(anchors)

        points = item.get("expected_points")
        assert points and 2 <= len(points) <= 5, item["id"]
        for point in points:
            assert point["text"].strip(), item["id"]
            assert point["evidence_id"] in item["expected_evidence"], (item["id"], point)


def test_conditioned_claims_carry_condition_and_modality_points():
    items = [i for i in _load(V2_DATASET)["items"] if i["category"] == "conditioned_claim"]
    condition_markers = ("如果", "条件", "当", "时", "之后", "末端", "位置")
    modality_markers = ("必须", "不要", "不应", "通常", "可能", "很可能", "应该", "应", "无法", "只能", "可以")

    assert len(items) == 10
    for item in items:
        points = item["expected_points"]
        assert points, item["id"]
        assert item["expected_evidence"], item["id"]
        assert any(m in p["text"] for p in points for m in condition_markers), item["id"]
        assert any(m in p["text"] for p in points for m in modality_markers), item["id"]


def test_sidecar_hash_matches_the_dataset_bytes():
    assert V2_SIDECAR.exists()
    digest = hashlib.sha256(V2_DATASET.read_bytes()).hexdigest()
    tokens = V2_SIDECAR.read_text(encoding="utf-8").split()

    assert tokens, "sidecar is empty"
    assert len(tokens[0]) == 64
    assert tokens[0].lower() == digest
