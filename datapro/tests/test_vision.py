"""图片理解（vision）：格式嗅探、降级路径、失败分类、不假装看过。

不调真实视觉模型：所有成功路径都用假 `_call`，避免测试消耗额度并依赖外网。
"""
import pytest

from app.services import vision as vision_mod
from app.services.vision import (VisionError, VisionService, is_image_entry,
                                 sniff_image_mime)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64


def test_sniff_image_mime_by_magic_bytes():
    assert sniff_image_mime(PNG) == "image/png"
    assert sniff_image_mime(JPEG) == "image/jpeg"
    assert sniff_image_mime(GIF) == "image/gif"
    assert sniff_image_mime(WEBP) == "image/webp"
    # 扩展名说图片、内容不是图片：必须判掉，否则上游直接 400
    assert sniff_image_mime(b"%PDF-1.4") is None
    assert sniff_image_mime(b"") is None


def test_is_image_entry_falls_back_to_mime():
    assert is_image_entry({"ext": ".png"})
    assert is_image_entry({"ext": "", "mime": "image/jpeg"})
    assert not is_image_entry({"ext": ".csv", "mime": "text/csv"})


@pytest.fixture
def stub_file(monkeypatch):
    """让 describe 能读到文件内容，同时验证它按 file_id 取完整条目。"""
    data = {"bytes": PNG}

    def fake_get(file_id):
        if not file_id:
            return None
        return {"file_id": file_id, "name": "shot.png", "session_id": "s1",
                "stored_name": "x_shot.png", "ext": ".png", "mime": "image/png"}

    from app.services import file_store as fs_mod

    monkeypatch.setattr(fs_mod.file_store, "get", fake_get)
    monkeypatch.setattr(fs_mod.file_store, "content_of", lambda e: data["bytes"])
    return data


def _public_entry(file_id="f1"):
    # 路由层传给 describe 的是去掉 stored_name 的公开视图
    return {"file_id": file_id, "name": "shot.png", "ext": ".png",
            "size": 72, "origin": "upload"}


async def test_describe_without_config_is_honest(monkeypatch):
    """未配置视觉模型：不调模型，也不能返回「图片没有内容」。"""
    svc = VisionService()
    monkeypatch.setattr(svc, "resolve", lambda: None)
    monkeypatch.setattr(svc, "available", lambda: False)
    res = await svc.describe([_public_entry()], "分析这个图片")
    assert res["text"] == ""
    assert any("未配置" in s["reason"] for s in res["skipped"])


async def test_describe_rejects_non_image_bytes(monkeypatch, stub_file):
    stub_file["bytes"] = b"%PDF-1.4 not an image"
    svc = VisionService()
    monkeypatch.setattr(svc, "resolve", lambda: {"base_url": "x", "api_key": "k", "model": "m"})
    monkeypatch.setattr(svc, "available", lambda: True)
    res = await svc.describe([_public_entry()], "看图")
    assert res["text"] == ""
    assert any("不是有效图片" in s["reason"] for s in res["skipped"])


async def test_describe_skips_oversized(monkeypatch, stub_file):
    monkeypatch.setattr(vision_mod.settings, "vision_max_bytes", 10, raising=False)
    svc = VisionService()
    monkeypatch.setattr(svc, "resolve", lambda: {"base_url": "x", "api_key": "k", "model": "m"})
    monkeypatch.setattr(svc, "available", lambda: True)
    res = await svc.describe([_public_entry()], "看图")
    assert res["text"] == ""
    assert any("大小上限" in s["reason"] for s in res["skipped"])


async def test_describe_success_reads_by_file_id(monkeypatch, stub_file):
    """回归：路由传的是公开视图（无 stored_name），必须按 file_id 重新取完整条目。"""
    calls = {}

    async def fake_call(cfg, blocks, prompt):
        calls["blocks"] = blocks
        calls["prompt"] = prompt
        return "图里是一张订单表，共 3 行。"

    svc = VisionService()
    monkeypatch.setattr(svc, "resolve", lambda: {"base_url": "x", "api_key": "k", "model": "m"})
    monkeypatch.setattr(svc, "available", lambda: True)
    monkeypatch.setattr(svc, "_call", fake_call)

    res = await svc.describe([_public_entry()], "分析这个图片")
    assert res["text"].startswith("图里是一张订单表")
    assert res["used"] == ["shot.png"]
    assert res["skipped"] == []
    assert calls["blocks"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "分析这个图片" in calls["prompt"]


async def test_describe_marks_unsupported_model(monkeypatch, stub_file):
    async def boom(cfg, blocks, prompt):
        raise vision_mod.VisionUnsupported('400 This model does not support image')

    svc = VisionService()
    monkeypatch.setattr(svc, "resolve", lambda: {"base_url": "x", "api_key": "k", "model": "m"})
    monkeypatch.setattr(svc, "available", lambda: True)
    monkeypatch.setattr(svc, "_call", boom)
    res = await svc.describe([_public_entry()], "看图")
    assert res["text"] == ""
    assert any("不支持" in s["reason"] for s in res["skipped"])


async def test_describe_marks_call_failure(monkeypatch, stub_file):
    async def boom(cfg, blocks, prompt):
        raise VisionError("upstream 500")

    svc = VisionService()
    monkeypatch.setattr(svc, "resolve", lambda: {"base_url": "x", "api_key": "k", "model": "m"})
    monkeypatch.setattr(svc, "available", lambda: True)
    monkeypatch.setattr(svc, "_call", boom)
    res = await svc.describe([_public_entry()], "看图")
    assert res["text"] == ""
    assert any("调用失败" in s["reason"] for s in res["skipped"])


def test_image_fallback_text_covers_reasons():
    from app.api.routes_chat import image_fallback_text

    assert "没有配置视觉模型" in image_fallback_text([{"reason": "视觉模型未配置"}])
    assert "不支持读取图片" in image_fallback_text([{"reason": "当前模型不支持图片输入"}])
    assert "大小上限" in image_fallback_text([{"reason": "图片超过大小上限"}])
    assert "不是有效图片" in image_fallback_text([{"reason": "文件内容不是有效图片"}])
    assert "调用失败" in image_fallback_text([{"reason": "视觉模型调用失败"}])
    # 任何情况下都要给出下一步，不能只说「做不到」
    assert "CSV" in image_fallback_text([])


async def test_general_events_skips_model_when_image_unreadable(monkeypatch):
    """图片全部解析失败时直接用固定说明，不让模型再编一遍「我看不到图」。"""
    from app.api import routes_chat

    called = {"n": 0}

    async def spy(*args, **kwargs):
        called["n"] += 1
        return "模型生成的回答", []

    monkeypatch.setattr(routes_chat, "answer_general", spy)
    events = [e async for e in routes_chat.general_events(
        "分析这个图片", None, image_notes="", image_skipped=[{"reason": "视觉模型未配置"}])]
    result = [e for e in events if e.get("type") == "result"][0]
    assert called["n"] == 0
    assert "没有配置视觉模型" in result["summary"]


async def test_general_events_uses_model_when_image_parsed(monkeypatch):
    from app.api import routes_chat

    async def fake_answer(question, history=None, image_notes=""):
        assert image_notes.startswith("图里是一张订单表")
        return "这张图里是 3 行订单数据。", []

    monkeypatch.setattr(routes_chat, "answer_general", fake_answer)
    events = [e async for e in routes_chat.general_events(
        "分析这个图片", None, image_notes="图里是一张订单表", image_skipped=[])]
    result = [e for e in events if e.get("type") == "result"][0]
    assert "3 行订单" in result["summary"]
