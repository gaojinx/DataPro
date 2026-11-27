"""上传内容密钥扫描（告警不拦截）：规则命中、片段脱敏、file_store 打标。"""
import pytest

from app.services import file_store as fs_mod
from app.services.secret_scan import describe, scan, scan_text


def test_detects_sk_style_keys():
    hits = scan_text("deepseek key is sk-abc123XYZ456def789GHI in config")
    rules = [h["rule"] for h in hits]
    assert "sk_style_key" in rules
    hit = next(h for h in hits if h["rule"] == "sk_style_key")
    assert hit["count"] == 1


def test_detects_private_key_block_and_aws():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END-----\nAKIAIOSFODNN7EXAMPLE"
    rules = {h["rule"] for h in scan_text(text)}
    assert "private_key" in rules
    assert "aws_key" in rules


def test_detects_credential_assignment():
    text = "my-api_key = 'AbcDef123456789012345'\npassword: hunter2secret"
    hits = scan_text(text)
    assert any(h["rule"] == "credential_assignment" for h in hits)


def test_clean_text_has_no_hits():
    assert scan_text("hello world\nname,qty\napple,3\nthis is a password field") == []


def test_snippets_are_masked_never_full_key():
    text = "token=sk-VeryRealLookingKey1234567890abcdef"
    hits = scan_text(text)
    assert hits
    for h in hits:
        assert "sk-VeryRealLookingKey1234567890abcdef" not in h["snippet"]
        assert len(h["snippet"]) <= 12        # 前 8 字符 + 省略号


def test_scan_bytes_handles_binary_and_empty():
    assert scan(b"") == []
    assert scan(bytes(range(256)) * 64) == []     # 随机二进制不命中也不炸
    assert scan("sk-abcdefgh12345678".encode("gb18030"))[0]["rule"] == "sk_style_key"


def test_describe_summary():
    hits = scan_text("sk-abcdefgh12345678 sk-ijklmnop87654321")
    assert "sk_style_key×2" in describe(hits)


def test_file_store_flags_entry_with_secret_hits(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_mod, "ROOT", tmp_path)
    monkeypatch.setattr(fs_mod, "INDEX", tmp_path / "index.json")
    store = fs_mod.FileStore()

    flagged = store.add(session_id="s1", filename="API.txt",
                        content="key: sk-realkey1234567890abcd".encode("utf-8"))
    assert flagged["secret_hits"]
    assert flagged["secret_hits"][0]["snippet"].endswith("…")

    clean = store.add(session_id="s1", filename="orders.csv", content=b"a,b\n1,2")
    assert clean["secret_hits"] == []


def test_file_store_scan_failure_does_not_block_upload(tmp_path, monkeypatch):
    """扫描内部异常绝不阻断上传（告警是旁路能力）。"""
    monkeypatch.setattr(fs_mod, "ROOT", tmp_path)
    monkeypatch.setattr(fs_mod, "INDEX", tmp_path / "index.json")
    from app.services import secret_scan

    monkeypatch.setattr(secret_scan, "scan", lambda raw: (_ for _ in ()).throw(RuntimeError("boom")))
    store = fs_mod.FileStore()
    entry = store.add(session_id="s1", filename="a.txt", content=b"hello")
    assert entry["file_id"]
    assert entry["secret_hits"] == []
