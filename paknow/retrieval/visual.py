"""Visual observation + page/figurual indexing via Qwen2.5-VL (plan §8).

ColPali/ColQwen2 late-interaction models are unavailable in this environment
(PaliGemma crashes on Blackwell sm_120; ColQwen2 checkpoints require
colpali_engine / transformers 4.x). Qwen2.5-VL is Blackwell-compatible and
transformers 5.x-native, so the visual route is implemented as
*structured observation*: the VLM reads a page/figure, we store the derived
observation text and index it with the same dense+sparse pipeline.

The observation is *derived index material* — it never replaces the original
page/figure as the authoritative source (plan §5.4).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from paknow.config import settings

_OBSERVE_PROMPT = """你是价格行为图表的观察器。只报告图中可见事实，不做预测、不给胜率。输出紧凑中文，含：
- 可见事实：品种/周期(若可读)、趋势/区间候选、明显波段/突破/回调、EMA/均线
- 图中文字：标题、图注、坐标、K线/箭头编号标注
- 不确定处：看不清的小字、无法确认的截点/周期
"""


@lru_cache(maxsize=1)
def _vlm():
    import torch
    from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        settings.visual_model, torch_dtype=torch.bfloat16, device_map="cuda"
    ).eval()
    processor = AutoProcessor.from_pretrained(settings.visual_model)
    return model, processor


def observe_image(image, max_new_tokens: int = 200) -> str:
    """Return a structured observation of one chart/page image (PIL/np)."""
    model, processor = _vlm()
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": _OBSERVE_PROMPT},
            ],
        }
    ]
    text = processor.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = processor(text=[text], images=[image], return_tensors="pt").to(model.device)
    with _no_grad():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens)
    # slice off the input tokens; keep only the generated (assistant) reply
    generated = out[:, inputs.input_ids.shape[1] :]
    return processor.batch_decode(generated, skip_special_tokens=True)[0].strip()


def observe_pdf_page(doc_path: str, page_1based: int, scale: float = 1.2, **kw) -> str:
    """Render a PDF page and observe it."""
    import fitz
    from PIL import Image

    d = fitz.open(doc_path)
    try:
        pix = d[page_1based - 1].get_pixmap(matrix=fitz.Matrix(scale, scale))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        d.close()
    return observe_image(img, **kw)


def _no_grad():
    import torch

    return torch.no_grad()


def search_pages(text: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Visual-route retrieval: reuse hybrid text search over observation units.

    Observations are indexed as evidence units (block_type='caption' derived);
    text search over them surfaces the matching pages/figures.
    """
    from paknow.retrieval.search import hybrid_search, point_to_doc

    return [point_to_doc(p) for p in hybrid_search(text, top_k=top_k)]
