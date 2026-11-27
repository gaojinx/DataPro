"""密钥引用解析（0017）：数据库只存引用（env:/file:/kms://），运行时解析为明文。

设计原则：
- **fail-closed**：引用解析失败按「未配置 Key」处理并给出明确原因，绝不静默回退到
  任何明文存储，也绝不把明文写进日志或 API 响应；
- 引用是**指针不是密钥**，可以落库、进审计、展示给管理员；
- 支持三种内置 provider：
    env:VAR_NAME            环境变量（含项目 .env 文件兜底，pydantic 不会把 .env 注入 os.environ）
    file:PATH[#VAR]         dotenv 风格密钥文件（相对路径以项目根为基准）
    kms://PATH[#FIELD]      Vault KV（v2 默认）HTTP 后端，SECRET_KMS_URL 配置后启用；
                            可通过 register_kms_backend() 接入其它 KMS
- 解析结果带 TTL 缓存：轮换密钥最多 60 秒生效，无需重启。
"""
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from app.config import settings

logger = logging.getLogger("datapro.secret")

DATAPRO_DIR = Path(__file__).resolve().parent.parent.parent
SECRETS_FILE = DATAPRO_DIR / "data" / "secrets" / "llm_apis.env"

_REF_RE = re.compile(r"^(env|file|kms):(?://)?(?P<body>\S+)$")


class SecretRefError(ValueError):
    """密钥引用无法解析（含原因，供管理端展示；不含密钥本身）。

    继承 ValueError：路由层的 except ValueError 统一把非法引用转 400。
    """


# ---------- .env 文件兜底（env provider 用；pydantic-settings 不注入 os.environ） ----------
_envfile_lock = threading.Lock()
_envfile_cache: dict[str, str] | None = None


def _dotenv_entries() -> dict[str, str]:
    global _envfile_cache
    with _envfile_lock:
        if _envfile_cache is not None:
            return _envfile_cache
        entries: dict[str, str] = {}
        path = DATAPRO_DIR / ".env"
        try:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    entries[k.strip()] = v.strip().strip("'\"")
        except OSError as exc:
            logger.warning("读取 .env 失败（env: 引用将只查环境变量）：%s", exc)
        _envfile_cache = entries
        return entries


# ---------- KMS 后端（默认 Vault KV over HTTP；可注册替换） ----------
def _resolve_kms_vault(path: str, field: str) -> str:
    if not settings.secret_kms_url:
        raise SecretRefError("kms 引用需要配置 SECRET_KMS_URL（如 https://vault.example.com:8200）")
    token = os.environ.get(settings.secret_kms_token_env, "")
    if not token:
        raise SecretRefError(f"kms 引用需要环境变量 {settings.secret_kms_token_env} 提供 Vault Token")
    import httpx

    if settings.secret_kms_kv_version == 2:
        url = f"{settings.secret_kms_url.rstrip('/')}/v1/{settings.secret_kms_mount.strip('/')}/data/{path.strip('/')}"
    else:
        url = f"{settings.secret_kms_url.rstrip('/')}/v1/{settings.secret_kms_mount.strip('/')}/{path.strip('/')}"
    try:
        resp = httpx.get(url, headers={"X-Vault-Token": token}, timeout=5.0)
    except Exception as exc:  # noqa: BLE001 —— 网络错误统一转解析失败
        raise SecretRefError(f"kms 读取失败：{type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise SecretRefError(f"kms 读取失败 HTTP {resp.status_code}（路径或权限不对）")
    data = resp.json().get("data") or {}
    payload = (data.get("data") or {}) if settings.secret_kms_kv_version == 2 else data
    value = payload.get(field or "value")
    if not value:
        raise SecretRefError(f"kms 密文中缺少字段 {field or 'value'}")
    return str(value)


_kms_lock = threading.Lock()
_kms_backend = _resolve_kms_vault


def register_kms_backend(fn) -> None:
    """替换默认 KMS 后端（如接入云厂商 SDK）。签名：fn(path, field) -> str。"""
    global _kms_backend
    with _kms_lock:
        _kms_backend = fn


# ---------- 解析 ----------
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}      # ref -> (expire_at, value)


def _resolve_uncached(scheme: str, body: str) -> str:
    if scheme == "env":
        var = body.strip()
        value = os.environ.get(var) or _dotenv_entries().get(var)
        if not value:
            raise SecretRefError(f"环境变量 {var} 未设置")
        return value
    if scheme == "file":
        location, _, var = body.partition("#")
        path = Path(location.strip())
        if not path.is_absolute():
            path = DATAPRO_DIR / path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SecretRefError(f"密钥文件不可读：{path.name}（{exc.__class__.__name__}）") from exc
        if var:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith(f"{var}="):
                    return line[len(var) + 1:].strip().strip("'\"")
            raise SecretRefError(f"密钥文件 {path.name} 中缺少变量 {var}")
        value = text.strip()
        if not value:
            raise SecretRefError(f"密钥文件 {path.name} 内容为空")
        return value
    if scheme == "kms":
        path, _, field = body.partition("#")
        with _kms_lock:
            backend = _kms_backend
        return backend(path.strip("/"), field.strip())
    raise SecretRefError(f"不支持的密钥引用类型 {scheme}:")


def resolve(ref: str) -> str:
    """解析引用为明文 Key。失败抛 SecretRefError；成功结果缓存 TTL 秒（轮换自动生效）。"""
    scheme, body = parse_ref(ref)
    with _cache_lock:
        hit = _cache.get(ref)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    value = _resolve_uncached(scheme, body)
    with _cache_lock:
        _cache[ref] = (time.monotonic() + max(settings.secret_ref_cache_ttl, 0.0), value)
    return value


def invalidate(ref: str = "") -> None:
    """清空解析缓存（轮换密钥后想立即生效时调用）。"""
    with _cache_lock:
        if ref:
            _cache.pop(ref, None)
        else:
            _cache.clear()


# ---------- 引用校验与规范化 ----------
def parse_ref(ref: str) -> tuple[str, str]:
    text = (ref or "").strip()
    m = _REF_RE.match(text)
    if not m:
        raise SecretRefError(
            "密钥引用格式非法，应为 env:变量名 / file:路径[#变量] / kms://路径[#字段]，"
            "不接受明文 Key")
    return m.group(1), m.group("body")


def validate_ref(ref: str) -> str:
    """规范化并校验引用；返回可入库的字符串。空值合法（=未配置 Key）。"""
    text = (ref or "").strip()
    if not text:
        return ""
    if len(text) > 255:
        raise SecretRefError("密钥引用过长（上限 255 字符）")
    scheme, _ = parse_ref(text)          # 非法/明文形态在这里被拒绝
    if any(ch.isspace() for ch in text):
        raise SecretRefError("密钥引用不能包含空白字符")
    if scheme == "env":
        var = text.split(":", 1)[1]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
            raise SecretRefError(f"env 引用的变量名非法：{var}")
    return text


def looks_like_plaintext(value: str) -> bool:
    """启发式：像明文 Key 而不像引用的值（用于把旧字段拦在门外）。"""
    text = (value or "").strip()
    return bool(text) and not _REF_RE.match(text)


# ---------- 明文迁移辅助（仅 0017 迁移与旧文件种子化使用） ----------
def extract_plaintext(value: str, var_name: str) -> str:
    """把一段明文 Key 写入 data/secrets/llm_apis.env（0600），返回其 file: 引用。

    这是「明文 → 引用」的一次性搬运：密钥离开数据库（备份/审计不再泄漏），
    落到 gitignore 保护的本地文件；生产应继续改用 env:/kms: 引用。
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("空 Key 无需迁移")
    path = SECRETS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{var_name}="
    lines = [ln for ln in lines if not ln.startswith(prefix)]
    lines.append(f"{prefix}{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass    # Windows FAT/网络卷无 POSIX 权限，忽略
    return f"file:{path.relative_to(DATAPRO_DIR).as_posix()}#{var_name}"


def health(ref: str) -> dict:
    """引用健康状态（供管理端展示；绝不返回明文）。"""
    text = (ref or "").strip()
    if not text:
        return {"state": "missing", "detail": "未配置密钥引用"}
    try:
        resolve(text)
        return {"state": "ok", "detail": ""}
    except SecretRefError as exc:
        return {"state": "unresolved", "detail": str(exc)}


def main() -> None:
    """CLI 自检：python -m app.services.secret_ref check —— 逐条验证引用可解析（不回显 Key）。"""
    import sys

    if len(sys.argv) < 2 or sys.argv[1] != "check":
        print("用法：python -m app.services.secret_ref check")
        return
    try:
        from app.db.models import LLMApi
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            rows = db.query(LLMApi).order_by(LLMApi.created_at, LLMApi.id).all()
    except Exception as exc:  # noqa: BLE001
        print(f"业务库不可读：{exc}")
        return
    if not rows:
        print("llm_api 表为空")
    for r in rows:
        h = health(r.api_key_ref)
        mark = {"ok": "OK", "missing": "未配置", "unresolved": "失败"}[h["state"]]
        print(f"{r.id}  {r.name:<24} {r.api_key_ref or '（空）':<52} [{mark}] {h['detail']}")


if __name__ == "__main__":
    main()
