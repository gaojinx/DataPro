"""DataPro 配置中心：全部配置走 pydantic-settings，禁止硬编码。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 应用
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8100
    # 启动时是否自动执行建表/迁移；K8s 多副本时由 initContainer 执行，容器内关闭
    init_db_on_startup: bool = True

    # 配置域存储：db=业务库 config_doc（默认，多实例强一致）；file=仅本地 JSON（回滚开关）
    config_store: str = "db"
    # config_doc 读缓存 TTL（秒）：配置读多写少，1 秒的跨实例延迟可接受
    config_store_cache_ttl: float = 1.0

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
    # 扫描量预估拦截（A09）：EXPLAIN 预估行数超过阈值时转人工，0 表示关闭
    scan_guard_enabled: bool = True
    max_scan_rows: int = 2_000_000
    # 数据新鲜度（U-B01）：相对时间窗用真实最新分区做锚点，替代日历 T-1
    partition_read_enabled: bool = True
    partition_cache_ttl_seconds: int = 600
    partition_anchor_table: str = "dwd_order_detail_di"   # 基准表：MAX(dt)
    eval_fixed_anchor: str = ""                          # 评测固定锚点（YYYY-MM-DD），空=不启用
    # 大结果集导出（B13/F11）：审批通过后最多导出行数
    export_max_rows: int = 100_000
    # 生命周期：审计/取数记录/消息保留天数（0 表示不清理）；由 worker 每日执行分批删除
    audit_retention_days: int = 180
    query_retention_days: int = 365
    message_retention_days: int = 365

    # 自有业务库（会话/审计/元数据）
    app_database_url: str = "mysql+pymysql://datapro:pw@127.0.0.1:3306/datapro"
    # 数据源库（来源/数据集/字段映射/批次/坏行）：留空则按业务库名推导为 <业务库>_source
    source_database_url: str = ""
    # 启动时若数据源库不存在，是否尝试自动创建（需要账号具备 CREATE 权限）
    source_db_auto_create: bool = False
    redis_url: str = "redis://127.0.0.1:6379/0"
    # 每用户每分钟请求上限（B14/E007）；0 表示关闭限流
    rate_limit_per_minute: int = 30
    # 限定范围缓存（U-B03）：只缓存意图分类与口径问答，取数结果永不缓存
    query_cache_enabled: bool = True
    cache_ttl_intent: int = 21_600        # 意图分类 6h（纯文本函数，不易陈旧）
    cache_ttl_meta: int = 1_800           # 口径问答 30min（键含资料指纹，改资料即失效）
    cache_ttl_negative: int = 120         # 未命中资料的负结果 2min

    # LLM
    llm_mock: bool = True
    web_search_enabled: bool = True   # 通用问答（天气/新闻等）是否联网搜索
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    llm_fallback_model: str = ""

    # 图片理解（视觉模型）：主模型不接受图片，图片只能交给视觉模型转成文字描述。
    # 视觉模型是实验性的，必须能一键关掉；关掉后对图片只做诚实说明，不假装能看。
    vision_enabled: bool = True
    vision_model: str = "deepseek-v4-flash-vision-exp"
    vision_base_url: str = ""          # 空 = 复用 LLM 注册表默认项的 base_url
    vision_api_key_ref: str = ""       # 空 = 复用 LLM 注册表默认项的 Key（引用，非明文）
    vision_timeout: float = 30.0
    vision_max_retries: int = 2
    vision_max_images: int = 3         # 单次最多解析几张：多图成本与延迟线性增长
    vision_max_bytes: int = 10 * 1024 * 1024   # 单图上限；再大先压缩，别把整段请求拖死

    # 密钥引用（0017）：llm_api 表只存引用（env:/file:/kms://），运行时解析，不明文落库
    secret_ref_cache_ttl: float = 60.0      # 解析结果缓存秒数；轮换密钥最多这么久生效
    secret_kms_url: str = ""                # Vault 地址（如 https://vault:8200）；空=不支持 kms: 引用
    secret_kms_mount: str = "secret"        # Vault KV 挂载点
    secret_kms_kv_version: int = 2          # Vault KV 引擎版本（1 或 2）
    secret_kms_token_env: str = "VAULT_TOKEN"  # 存放 Vault Token 的环境变量名

    # 可观测（Langfuse，可选）：Key 齐全且开关打开才上报，否则全部 no-op
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # 跨域来源白名单（逗号分隔）：部署到公网的静态页需要跨域调用后端
    cors_allow_origins: str = ("https://37lhyy3u5n4xv69lcj5.traesite.cn,"
                               "http://127.0.0.1:5175,http://localhost:5175")

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
    # 巡检失败告警 webhook（企业 IM 机器人；留空只记日志）
    inspection_webhook_url: str = ""
    inspection_webhook_type: str = "auto"   # auto | feishu | dingtalk | wecom | generic
    inspection_webhook_secret: str = ""     # 钉钉加签 / 飞书签名密钥（可空）
    recall_degraded_mode: str = "auto"  # auto | force_keyword

    # 返回层脱敏（S04）：内置手机号/身份证/银行卡/邮箱规则；追加列名关键词用逗号分隔
    mask_enabled: bool = True
    mask_extra_columns: str = ""

    # 向量召回（A05）：默认关闭；编码器二选一——本地 OpenVINO IR，或复用 API embedding
    vector_recall_enabled: bool = False
    vector_encoder: str = "auto"            # auto=有本地 IR 用本地，否则用 API embedding；local / api
    vector_ir_path: str = ""
    vector_tokenizer: str = "BAAI/bge-small-zh-v1.5"
    # 句向量池化：cls（bge 等中文模型约定）| mean（部分 sentence-transformers 模型）
    # 池化方式选错不会报错，只会让相似度分布整体偏移——换模型时必须一起确认。
    vector_pooling: str = "cls"
    vector_max_length: int = 128
    vector_top_k: int = 3
    vector_min_score: float = 0.35
    # 字段级（二级文档）：数量是表文档的 6~8 倍，阈值与 top_k 单独一套
    vector_column_top_k: int = 8
    vector_column_min_score: float = 0.30
    vector_backend: str = "memory"          # memory | milvus
    # 向量路单独超时：与 TIMEOUT_RECALL 解耦。首次编码（尚未预热完成）需要几秒，
    # 用 3 秒的召回总超时会把这一轮直接掐成纯关键词，表现为「刚上线时召回很差」。
    vector_timeout: float = 5.0
    # 元数据向量缓存（0021）：落库复用，避免每个进程启动都重新编码上百份文档；
    # 库不可用时自动退回纯内存，不影响召回正确性，只是重启后要重算
    vector_cache_enabled: bool = True
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_collection: str = "datapro_meta"

    # 文档知识库 RAG（0020）：Markdown/网页/纯文本切块向量化后做语义检索，
    # 与元数据召回并行。命中不足阈值时宁可回答「未收录」也不注入弱相关片段。
    doc_rag_enabled: bool = True
    doc_rag_top_k: int = 4
    doc_rag_min_score: float = 0.35        # 融合分阈值（0~1）；低于此分一律不注入
    doc_rag_lexical_weight: float = 0.25   # 词法分权重，其余给向量分；embedding 不可用时自动全给词法
    doc_rag_max_context_chars: int = 4000  # 注入 prompt 的参考资料上限，防止挤压其他槽位
    doc_rag_cache_ttl: float = 30.0        # chunk 索引进程内缓存秒数；写入后本实例立即失效
    doc_chunk_size: int = 360              # 目标 chunk 字数（中文按字符计）
    doc_chunk_overlap: int = 80            # 相邻 chunk 重叠字数，避免切断上下文

    # Embedding（API，OpenAI 兼容 /embeddings）：三项齐备才启用，否则文档 RAG 回退词法检索
    embed_base_url: str = ""
    embed_model: str = ""
    embed_api_key_ref: str = ""            # 密钥引用（env:/file:/kms://），与 llm_api.api_key_ref 同一套解析
    embed_dim: int = 1024                  # 期望维度；实际返回维度不一致只告警，以实际为准
    embed_batch_size: int = 16
    embed_timeout: float = 10.0
    embed_max_retries: int = 3

    # 监控（I03）：/metrics 文本端点开关
    metrics_enabled: bool = True

    # 意图识别：LLM 分类优先，失败/未配置回退关键词规则
    intent_llm_enabled: bool = True

    # 自动学习（A11）：成功取数后沉淀技能规范；对话后抽取长期记忆（旁路异步，失败不影响主链路）
    auto_learn_enabled: bool = True
    auto_memory_enabled: bool = True
    auto_skill_min_confidence: float = 0.6
    auto_memory_min_confidence: float = 0.7

    @property
    def cors_origins(self) -> list[str]:
        """CORS 白名单列表；空字符串表示不限制来源（仅本地联调时使用）。"""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def source_db_url(self) -> str:
        """数据源库连接串：显式配置优先，否则由业务库名推导 <db>_source。

        推导而非硬编码，是为了本地/开发环境无需额外配置，同时避免在代码里
        写死账号口令（生产必须显式设置 SOURCE_DATABASE_URL）。
        """
        explicit = (self.source_database_url or "").strip()
        if explicit:
            return explicit
        from sqlalchemy.engine import make_url

        url = make_url(self.app_database_url)
        if not url.database:
            raise ValueError("APP_DATABASE_URL 缺少数据库名，无法推导数据源库；"
                             "请显式配置 SOURCE_DATABASE_URL")
        return url.set(database=f"{url.database}_source").render_as_string(hide_password=False)


settings = Settings()
