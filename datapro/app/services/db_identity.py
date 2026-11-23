"""会话级数据库身份（F27 安全修复）。

问题：共享只读账号 ro 被整库授权 dp_uploads.*，任何会话都可能读到其他会话的上传文件表。

方案：每个会话一个 MySQL 只读账号，仅授予
  - dwd_shop.*（公共数仓，本就全员可读）
  - 本会话已解析文件表 dp_uploads.f_{file_id}
账号密码由 session_id 与服务端密钥派生，不落盘；重启后可按需重建。

说明：骨架用固定密钥派生，生产应改为 Vault/KMS 托管或短时令牌，并配置账号过期策略。
"""
import hashlib
from urllib.parse import quote, urlsplit, urlunsplit

import sqlalchemy as sa

from app.config import settings

TEMP_DB = "dp_uploads"


def _digest(session_id: str) -> str:
    return hashlib.sha256(f"{settings.session_db_secret}:{session_id}".encode()).hexdigest()


def session_db_user(session_id: str) -> str:
    return "dp_u_" + _digest(session_id)[:16]


def session_db_password(session_id: str) -> str:
    return "P" + _digest(session_id)[16:40]


def session_db_url(session_id: str) -> str:
    """以会话账号连接数仓（默认库 dwd_shop；文件表用 dp_uploads.表名 限定）。"""
    parts = urlsplit(settings.data_source_url)
    user = quote(session_db_user(session_id))
    pwd = quote(session_db_password(session_id))
    netloc = f"{user}:{pwd}@{parts.hostname}:{parts.port or 3306}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/dwd_shop", "", ""))


def ensure_session_user(conn, session_id: str, tables: list[str] | None = None) -> None:
    """创建/校正会话账号与授权（需管理连接）。"""
    user = session_db_user(session_id)
    pwd = session_db_password(session_id)
    conn.execute(sa.text(f"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY '{pwd}'"))
    conn.execute(sa.text(f"ALTER USER '{user}'@'%' IDENTIFIED BY '{pwd}'"))
    conn.execute(sa.text(f"GRANT SELECT ON dwd_shop.* TO '{user}'@'%'"))
    for table in tables or []:
        conn.execute(sa.text(f"GRANT SELECT ON `{TEMP_DB}`.`{table}` TO '{user}'@'%'"))


def revoke_temp_db_from_shared_account(conn) -> None:
    """回收共享只读账号对临时表库的整库权限（幂等）。"""
    try:
        conn.execute(sa.text("REVOKE SELECT ON `dp_uploads`.* FROM 'ro'@'%'"))
    except Exception:
        pass  # 未授权过或账号不存在


def revoke_session_table(conn, session_id: str, table: str) -> None:
    try:
        conn.execute(sa.text(
            f"REVOKE SELECT ON `{TEMP_DB}`.`{table}` FROM '{session_db_user(session_id)}'@'%'"))
    except Exception:
        pass
