"""密钥引用解析（0017）：env/file/kms 三种 provider 与 fail-closed 行为。"""
import pytest

from app.services import secret_ref as sr
from app.services.secret_ref import SecretRefError, extract_plaintext, health, resolve, validate_ref


# ---------- 引用校验 ----------

def test_validate_ref_accepts_known_schemes_and_rejects_plaintext():
    assert validate_ref("") == ""                        # 空 = 未配置，合法
    assert validate_ref("env:LLM_API_KEY") == "env:LLM_API_KEY"
    assert validate_ref(" file:data/secrets.env#K ") == "file:data/secrets.env#K"
    assert validate_ref("kms://prod/llm/key#value") == "kms://prod/llm/key#value"
    assert validate_ref("kms:prod/llm/key") == "kms:prod/llm/key"

    with pytest.raises(SecretRefError, match="明文"):
        validate_ref("sk-real-looking-key-1234567890")   # 明文形态被拒绝
    with pytest.raises(SecretRefError):
        validate_ref("s3://bucket/x")                    # 未知协议
    with pytest.raises(SecretRefError):
        validate_ref("env:BAD NAME")                     # 变量名非法
    with pytest.raises(SecretRefError):
        validate_ref("env:" + "A" * 300)                 # 过长


# ---------- env provider ----------

def test_env_provider(monkeypatch):
    monkeypatch.setenv("LLM_KEY_T1", "sk-env-value")
    assert resolve("env:LLM_KEY_T1") == "sk-env-value"

    monkeypatch.delenv("LLM_KEY_T2", raising=False)
    with pytest.raises(SecretRefError, match="LLM_KEY_T2"):
        resolve("env:LLM_KEY_T2")


def test_env_provider_falls_back_to_dotenv(monkeypatch, tmp_path):
    """pydantic-settings 不会把 .env 注入 os.environ；env: 引用需兜底读项目 .env。"""
    monkeypatch.delenv("LLM_KEY_DOTENV", raising=False)
    monkeypatch.setattr(sr, "DATAPRO_DIR", tmp_path)
    sr._envfile_cache = None
    (tmp_path / ".env").write_text('LLM_KEY_DOTENV="sk-from-dotenv"\n', encoding="utf-8")
    assert resolve("env:LLM_KEY_DOTENV") == "sk-from-dotenv"


# ---------- file provider ----------

def test_file_provider_with_var(tmp_path):
    secret_file = tmp_path / "llm_apis.env"
    secret_file.write_text("LLM_KEY_A=sk-file-a\nLLM_KEY_B=sk-file-b\n", encoding="utf-8")
    assert resolve(f"file:{secret_file.as_posix()}#LLM_KEY_A") == "sk-file-a"
    assert resolve(f"file:{secret_file.as_posix()}#LLM_KEY_B") == "sk-file-b"
    with pytest.raises(SecretRefError, match="缺少变量"):
        resolve(f"file:{secret_file.as_posix()}#LLM_KEY_C")


def test_file_provider_whole_content_and_missing(tmp_path):
    secret_file = tmp_path / "single.key"
    secret_file.write_text("sk-whole\n", encoding="utf-8")
    assert resolve(f"file:{secret_file.as_posix()}") == "sk-whole"

    with pytest.raises(SecretRefError, match="不可读"):
        resolve(f"file:{(tmp_path / 'nope.key').as_posix()}")


# ---------- kms provider ----------

def test_kms_provider_without_config_fails_closed(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "secret_kms_url", "", raising=False)
    with pytest.raises(SecretRefError, match="SECRET_KMS_URL"):
        resolve("kms://prod/llm/key#value")


def test_kms_provider_uses_registered_backend(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "secret_kms_url", "https://vault.example.com", raising=False)

    calls = []

    def fake_backend(path, field):
        calls.append((path, field))
        return "sk-from-kms"

    sr.register_kms_backend(fake_backend)
    try:
        assert resolve("kms://prod/llm/key#api_key") == "sk-from-kms"
        assert calls == [("prod/llm/key", "api_key")]
    finally:
        sr.register_kms_backend(sr._resolve_kms_vault)   # 还原默认后端


# ---------- 解析缓存（TTL 内不重复读源） ----------

def test_resolve_cache_hits_within_ttl(monkeypatch):
    counter = {"n": 0}
    real = sr._resolve_uncached

    def counting(scheme, body):
        counter["n"] += 1
        return real(scheme, body)

    monkeypatch.setenv("LLM_KEY_CACHED", "sk-cached")
    monkeypatch.setattr(sr, "_resolve_uncached", counting)

    assert resolve("env:LLM_KEY_CACHED") == "sk-cached"
    assert resolve("env:LLM_KEY_CACHED") == "sk-cached"
    assert counter["n"] == 1                              # 第二次走缓存
    sr.invalidate()
    assert resolve("env:LLM_KEY_CACHED") == "sk-cached"
    assert counter["n"] == 2                              # 失效后重读


# ---------- 明文迁移辅助与健康状态 ----------

def test_extract_plaintext_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "SECRETS_FILE", tmp_path / "secrets" / "llm_apis.env")
    ref = extract_plaintext("sk-roundtrip-1234", "LLM_KEY_R1")
    assert ref.startswith("file:")
    assert ref.endswith("#LLM_KEY_R1")
    assert resolve(ref) == "sk-roundtrip-1234"
    # 同名变量覆盖而非追加
    ref2 = extract_plaintext("sk-rotated", "LLM_KEY_R1")
    content = (tmp_path / "secrets" / "llm_apis.env").read_text(encoding="utf-8")
    assert content.count("LLM_KEY_R1=") == 1
    sr.invalidate()                       # 同名变量被覆盖，需失效缓存后重读
    assert resolve(ref2) == "sk-rotated"


def test_health_reports_states(monkeypatch):
    monkeypatch.setenv("LLM_KEY_OK", "sk-ok")
    monkeypatch.delenv("LLM_KEY_GONE", raising=False)
    assert health("")["state"] == "missing"
    assert health("env:LLM_KEY_OK")["state"] == "ok"
    bad = health("env:LLM_KEY_GONE")
    assert bad["state"] == "unresolved" and "LLM_KEY_GONE" in bad["detail"]
