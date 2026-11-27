"""配置域关系化（0014）+ 密钥引用化（0017）：LLM API 注册表与角色权限矩阵。

不依赖数据库：用显式 path / use_db=False 覆盖文件分支，
数据库分支由迁移与实跑验证（见 AGENTS.md 记录）。
"""
import json

import pytest

from app.services import llm_registry as llm_module
from app.services.permissions import DEFAULT_USER_CODES, PermissionMatrix


def test_config_tables_registered():
    from app.db.models import Base

    tables = set(Base.metadata.tables)
    assert {"llm_api", "role_permission"} <= tables


def test_permission_matrix_uses_explicit_path(tmp_path):
    path = tmp_path / "roles.json"
    matrix = PermissionMatrix(path)                 # 显式 path ⇒ 纯文件模式

    assert matrix.role_codes("user") == DEFAULT_USER_CODES
    matrix.set_role_codes("user", ["chat.use", "files.upload", "users.manage"])
    # 管理类权限被过滤，且写入指定文件（不碰真实配置）
    assert matrix.role_codes("user") == ["chat.use", "files.upload"]
    assert json.loads(path.read_text(encoding="utf-8"))["roles"]["user"] == \
        ["chat.use", "files.upload"]


def test_permission_matrix_rejects_unknown_role(tmp_path):
    matrix = PermissionMatrix(tmp_path / "roles.json")
    with pytest.raises(ValueError):
        matrix.set_role_codes("admin", ["chat.use"])
    with pytest.raises(ValueError):
        matrix.set_role_codes("guest", ["chat.use"])


def test_llm_registry_file_mode_crud(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_module, "DATA_FILE", tmp_path / "llm_apis.json")
    monkeypatch.setenv("LLM_KEY_A", "sk-aaaa1111")
    monkeypatch.setenv("LLM_KEY_B", "sk-bbbb2222")
    reg = llm_module.LLMRegistry(use_db=False)

    first = reg.add({"name": "A", "base_url": "https://8.8.8.8/v1",
                     "model": "m-a", "api_key_ref": "env:LLM_KEY_A"})
    assert first["is_default"] is True               # 首个自动设默认
    assert first["key_masked"].endswith("1111")
    assert first["key_state"] == "ok"
    second = reg.add({"name": "B", "base_url": "https://8.8.8.8/v2",
                      "model": "m-b", "api_key_ref": "env:LLM_KEY_B"})
    assert second["is_default"] is False

    assert reg.status()["default_model"] == "m-a"
    assert reg.status()["available"] is True
    assert reg.set_default(second["id"]) is True
    assert reg.status()["default_model"] == "m-b"

    assert reg.update(second["id"], {"name": "B2"})["name"] == "B2"
    assert reg.delete(second["id"]) is True
    assert reg.get(second["id"]) is None
    # 删除默认项后顺延剩下的第一个
    assert reg.status()["default_model"] == "m-a"
    assert reg.delete("not-exist") is False


def test_llm_registry_rejects_plaintext_and_bad_refs(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_module, "DATA_FILE", tmp_path / "llm_apis.json")
    monkeypatch.setenv("LLM_KEY_A", "sk-aaaa1111")
    reg = llm_module.LLMRegistry(use_db=False)

    # 明文 Key 被明确拒绝（防止回到明文存储）
    with pytest.raises(ValueError, match="明文"):
        reg.add({"name": "p", "base_url": "https://8.8.8.8/v1",
                 "model": "m", "api_key": "sk-secret"})
    with pytest.raises(ValueError, match="明文"):
        reg.add({"name": "p", "base_url": "https://8.8.8.8/v1",
                 "model": "m", "api_key_ref": "env:LLM_KEY_A", "api_key": "sk-secret"})
    # 引用格式非法
    with pytest.raises(ValueError):
        reg.add({"name": "p", "base_url": "https://8.8.8.8/v1",
                 "model": "m", "api_key_ref": "not-a-ref"})
    # 更新路径同样拒绝明文
    first = reg.add({"name": "A", "base_url": "https://8.8.8.8/v1",
                     "model": "m-a", "api_key_ref": "env:LLM_KEY_A"})
    with pytest.raises(ValueError, match="明文"):
        reg.update(first["id"], {"api_key": "sk-new"})


def test_llm_registry_unresolved_ref_fails_closed(tmp_path, monkeypatch):
    """引用指向不存在的环境变量：状态为 unresolved，status.available=False。"""
    monkeypatch.setattr(llm_module, "DATA_FILE", tmp_path / "llm_apis.json")
    monkeypatch.delenv("LLM_KEY_MISSING", raising=False)
    reg = llm_module.LLMRegistry(use_db=False)

    entry = reg.add({"name": "A", "base_url": "https://8.8.8.8/v1",
                     "model": "m-a", "api_key_ref": "env:LLM_KEY_MISSING"})
    assert entry["key_state"] == "unresolved"
    assert "LLM_KEY_MISSING" in entry["key_detail"]
    assert reg.status()["available"] is False
    assert reg.default_active().get("api_key") is None   # 明文绝不进入调用链


def test_llm_registry_rejects_private_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_module, "DATA_FILE", tmp_path / "llm_apis.json")
    reg = llm_module.LLMRegistry(use_db=False)
    with pytest.raises(ValueError):
        reg.add({"name": "evil", "base_url": "https://127.0.0.1:8100",
                 "model": "x"})
    with pytest.raises(ValueError):
        reg.add({"name": "plain", "base_url": "http://8.8.8.8/v1", "model": "x"})


def test_legacy_file_entry_converted_to_ref(tmp_path, monkeypatch):
    """升级场景：历史 JSON 里是明文 api_key，读取时自动转为密钥文件引用。"""
    import json as _json

    import app.services.secret_ref as sr

    data_file = tmp_path / "llm_apis.json"
    data_file.write_text(_json.dumps([{
        "id": "legacy1", "name": "旧条目", "base_url": "https://8.8.8.8/v1",
        "model": "m-old", "api_key": "sk-legacy9999", "enabled": True,
        "is_default": True, "created_at": "2026-01-01 00:00",
    }], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(llm_module, "DATA_FILE", data_file)
    # 把密钥文件指到临时目录，避免写真实 data/secrets/
    monkeypatch.setattr(sr, "SECRETS_FILE", tmp_path / "secrets" / "llm_apis.env")

    reg = llm_module.LLMRegistry(use_db=False)
    entry = reg.get("legacy1")
    assert entry["api_key_ref"].startswith("file:")
    assert entry["api_key_ref"].endswith("#LLM_KEY_LEGACY1")
    assert entry["key_state"] == "ok"
    assert entry["api_key"] == "sk-legacy9999"
    assert "api_key" not in reg.list()[0]              # 明文不出服务层
    assert (tmp_path / "secrets" / "llm_apis.env").exists()
