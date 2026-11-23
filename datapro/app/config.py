"""DataPro 配置中心：全部配置走 pydantic-settings，禁止硬编码。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 应用
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8100

    # 数据源（只读）
    data_source_url: str = "mysql+pymysql://ro:ro@127.0.0.1:3306/dwd_shop"
    # 上传文件临时表库（F27）：不指定默认库，便于首次自动建库；生产改为专用受限账号
    upload_db_url: str = "mysql+pymysql://root:123456@127.0.0.1:3306/"
    upload_max_rows: int = 20000
    upload_max_cols: int = 50
    # 会话数据库账号密码派生密钥（生产改为密钥管理服务托管）
    session_db_secret: str = "datapro-dev-secret-change-me"
    # 会话资源回收 TTL（S07）：超过该时长无活动的会话账号与临时表会被清理
    session_ttl_hours: int = 24
    exec_max_concurrency: int = 20
    exec_row_limit: int = 200
    exec_timeout_ms: int = 10_000

    # 自有业务库（会话/审计/元数据）
    app_database_url: str = "mysql+pymysql://datapro:pw@127.0.0.1:3306/datapro"
    redis_url: str = "redis://127.0.0.1:6379/0"

    # LLM
    llm_mock: bool = True
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_fallback_model: str = ""

    # 认证（B12 基础版：账号密码 + 签名 Token）
    auth_secret: str = "datapro-auth-secret-change-me"
    auth_token_ttl_hours: int = 12
    auth_seed_admin_password: str = "admin123"
    auth_seed_user_password: str = "user123"

    # 节点超时（秒）
    timeout_clarify: float = 5
    timeout_recall: float = 3
    timeout_generate: float = 20

    # 降级开关
    feature_data_pro_enabled: bool = True
    feature_inspection_enabled: bool = True
    recall_degraded_mode: str = "auto"  # auto | force_keyword


settings = Settings()
