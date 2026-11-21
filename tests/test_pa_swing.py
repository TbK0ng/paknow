"""The swing screen and model defaults never alter an original diagnostic."""
from types import SimpleNamespace

from paknow import gateway as gateway_module
from paknow.pa_swing import screen


def setup(*, direction="long", entry=100.0, stop=98.0, target=104.0, ema=99.6):
    return {"state": direction, "facts": {"ema20": ema, "atr14": 1.0},
            direction: {"levels": {"reference": entry, "invalidation": stop, "target": target}}}


def test_local_entry_geometry_not_fixed_ema_or_rr_thresholds():
    assert screen(setup())["status"] == "pending"
    short = setup(direction="short", stop=102, target=96, ema=100.4)
    assert screen(short)["status"] == "pending"
    assert screen(setup(ema=98.5))["status"] == "pending"
    assert screen(setup(target=103))["net_rr"] < 1.5
    assert screen(setup(target=103))["status"] == "pending"
    assert screen(setup(stop=101))["status"] == "blocked"


def test_micu_uses_high_reasoning_effort_for_structured_review(monkeypatch):
    config = SimpleNamespace(deepseek_base_url="https://www.micuapi.ai/v1", deepseek_model="deepseek-v4.1-flash",
                             deepseek_effort="high")
    monkeypatch.setattr(gateway_module, "settings", config)
    model = gateway_module.ModelGateway()
    calls = []
    def reply(**kw):
        calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))])

    from pydantic import BaseModel
    class Result(BaseModel):
        ok: bool

    monkeypatch.setattr(model, "_create", reply)
    assert model.chat_json(Result, [], attempts=1).ok
    assert len(calls) == 1
    assert calls[0]["model"] == "deepseek-v4.1-flash"
    assert calls[0]["extra_body"] == {"reasoning_effort": "high"}
    assert calls[0]["attempts"] == 1
