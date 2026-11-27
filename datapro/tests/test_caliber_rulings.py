"""口径裁决（D03）单测：裁决读写 + 命中裁决后不再澄清。"""
from app.core.statemachine import PipelineContext
from app.services.caliber_rulings import CaliberRulings, caliber_rulings
from tests.test_statemachine import build


def test_rulings_crud(tmp_path):
    rulings = CaliberRulings(tmp_path / "rulings.json")
    rulings.set("新增用户", "M004", "注册口径为准", "admin")
    assert rulings.resolve(" 新增用户 ")["metric_id"] == "M004"
    assert rulings.remove("新增用户") is True
    assert rulings.resolve("新增用户") is None


async def test_ruled_term_skips_clarify(tmp_path, monkeypatch):
    monkeypatch.setattr(caliber_rulings, "_path", tmp_path / "rulings.json")
    caliber_rulings.set("新增用户", "M004", "注册口径为准", "admin")
    ctx = PipelineContext(question="查一下昨天的新增用户", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert not any(e["type"] == "clarify" and e.get("kind") == "caliber" for e in events)
    assert [c["metric_id"] for c in ctx.recalled_calibers] == ["M004"]
    assert any("裁决" in note for note in ctx.ruling_notes)


async def test_unruled_ambiguity_still_clarifies(tmp_path, monkeypatch):
    monkeypatch.setattr(caliber_rulings, "_path", tmp_path / "empty.json")
    ctx = PipelineContext(question="查一下昨天的新增用户", user_id="t")
    events = [e async for e in build().run(ctx)]
    assert any(e["type"] == "clarify" and e.get("kind") == "caliber" for e in events)
