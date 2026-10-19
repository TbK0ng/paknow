from __future__ import annotations

from paknow_mcp.formatting import clip, display_answer, render_ask
from paknow_mcp.models import AskOutput, Citation, FigureRef


def test_display_answer_shifts_known_indices() -> None:
    assert display_answer("a[0] b[1] c[9]", 2) == "a[1] b[2] c[9]"


def test_display_answer_leaves_non_numeric_markdown() -> None:
    text = "[link](https://example.com) and [0]"
    assert display_answer(text, 1) == "[link](https://example.com) and [1]"


def test_clip_adds_ellipsis() -> None:
    assert clip("abcdef", 3) == "abc…"
    assert clip("abc", 3) == "abc"


def test_render_ask_keeps_uncertainty_and_locator() -> None:
    output = AskOutput(
        run_id="r1",
        answer_raw="x[0]",
        answer_display="x[1]",
        uncertainty=["阈值未说明"],
        citations=[
            Citation(
                index=0,
                display_index=1,
                evidence_id="ev-1",
                citation="《书》 → 第1章 → PDF 第1页",
                text="quote",
            )
        ],
        figures=[
            FigureRef(
                figure_id="fig-1",
                figure_label="1.1",
                image_url="http://127.0.0.1:8000/figures/fig-1/image",
            )
        ],
    )
    rendered = render_ask(output, 10000)
    assert "x[1]" in rendered
    assert "阈值未说明" in rendered
    assert "evidence_id=ev-1" in rendered
    assert "图 1.1" in rendered
    assert "citation_index_base=0" in rendered
