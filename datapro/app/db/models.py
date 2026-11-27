"""DataPro 自有业务库模型（HLD §5 核心数据模型）。"""
from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Integer, LargeBinary, String,
                        Text, UniqueConstraint, func)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    """登录账号（B12）：密码以 PBKDF2 哈希存储，角色用于接口鉴权。"""
    __tablename__ = "user_account"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin / user
    status: Mapped[str] = mapped_column(String(16), default="active")
    permissions: Mapped[str | None] = mapped_column(Text, nullable=True)  # 权限覆盖（JSON 数组；空=继承角色默认）
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 令牌版本：改密/禁用/改角色/改权限时递增
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Message(Base):
    __tablename__ = "message"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # 提问人
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # query/general/skill/clarify/reject…
    content: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(32))
    # 附件引用快照（0018）：JSON 数组 [{file_id,name,ext,size,origin}]，只存指针不存内容，
    # 历史消息恢复后文件仍可点击预览（文件本体在 data/uploads，访问走 files 鉴权）
    attachments: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 终态事件快照（0019）：result/clarify/reject/escalate/error 的 SSE 事件 JSON，
    # 供跨设备恢复富结果（表格/KPI/依据），步骤时间轴是瞬态不存
    payload: Mapped[str | None] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class QueryRecord(Base):
    """取数记录：审计 + 👍👎 反馈沉淀历史问答对（口径库模板 §3）。"""
    __tablename__ = "query_record"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # 提问人
    question: Mapped[str] = mapped_column(Text)
    question_context: Mapped[str | None] = mapped_column(Text, nullable=True)  # 前序用户问题（JSON 数组）
    recalled_schema: Mapped[str | None] = mapped_column(Text)
    generated_sql: Mapped[str | None] = mapped_column(Text)
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    final_answer: Mapped[str | None] = mapped_column(Text)
    user_feedback: Mapped[str | None] = mapped_column(String(8))  # up / down
    feedback_reason: Mapped[str | None] = mapped_column(String(64))
    # 结果分类（result/reject/escalate/clarify/error）：按错误码统计错误率的前提
    outcome: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    error_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    spec_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # IR 快照（可追溯改写依据）
    sql_final: Mapped[str | None] = mapped_column(Text, nullable=True)   # 校验器改写后的最终 SQL
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    node: Mapped[str] = mapped_column(String(16))
    question_digest: Mapped[str] = mapped_column(String(255))
    sql_text: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ExportRequest(Base):
    """大结果集导出申请（B13/F11）：审批状态、生成文件与下载审计的持久化记录。"""
    __tablename__ = "export_request"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    requester: Mapped[str] = mapped_column(String(64), index=True)
    question: Mapped[str] = mapped_column(Text)
    sql_text: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    window_start: Mapped[date | None] = mapped_column(Date, nullable=True)  # 原查询时间窗，导出重跑用
    window_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    preview_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file: Mapped[str | None] = mapped_column(String(128), nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MetricCaliber(Base):
    __tablename__ = "metric_caliber"
    metric_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    metric_name: Mapped[str] = mapped_column(String(64), index=True)
    aliases: Mapped[str | None] = mapped_column(String(255))
    definition: Mapped[str] = mapped_column(Text)
    logic: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / conflict
    source_table: Mapped[str | None] = mapped_column(String(128), nullable=True)     # D05：口径依赖表
    choice_keywords: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 歧义选择关键词
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class MetaTable(Base):
    """元数据表级信息（D05）：由导入脚本从填报模板写入，召回优先读这里。

    多来源后同一张表名可能出现在不同库（ods_xxx.orders / dwd_shop.orders），
    唯一键必须是 (db_name, table_name)，不能只用 table_name。
    """
    __tablename__ = "meta_table"
    __table_args__ = (UniqueConstraint("db_name", "table_name", name="uq_meta_table_db"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    db_name: Mapped[str] = mapped_column(String(64), default="dwd_shop")
    table_name: Mapped[str] = mapped_column(String(128), index=True)
    cn_name: Mapped[str] = mapped_column(String(128), default="")
    granularity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    update_freq: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dt_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str | None] = mapped_column(String(8), nullable=True)
    keywords: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 召回关键词，逗号分隔
    status: Mapped[str] = mapped_column(String(16), default="active")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class MetaColumn(Base):
    """元数据字段级信息（D05）：中文名、枚举含义、敏感标记，供召回与生成 prompt 使用。"""
    __tablename__ = "meta_column"
    __table_args__ = (UniqueConstraint("db_name", "table_name", "col_name",
                                       name="uq_meta_column"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    db_name: Mapped[str] = mapped_column(String(64), default="dwd_shop")
    table_name: Mapped[str] = mapped_column(String(128), index=True)
    col_name: Mapped[str] = mapped_column(String(128))
    cn_name: Mapped[str] = mapped_column(String(128), default="")
    data_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enum_meaning: Mapped[str | None] = mapped_column(String(255), nullable=True)
    caliber_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_pii: Mapped[bool] = mapped_column(Boolean, default=False)
    sample_values: Mapped[str | None] = mapped_column(String(255), nullable=True)
    common_pitfalls: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DbAccess(Base):
    """用户级数据表权限（A02 / S1）：一条记录 = 一个用户可访问一张表。

    无记录表示未配置（沿用全局白名单）；有记录时按记录收紧。
    username 外键指向 user_account.username（ON DELETE CASCADE），删用户时授权自动清理。
    """
    __tablename__ = "db_access"
    __table_args__ = (UniqueConstraint("username", "db_name", "table_name", name="uq_db_access"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 授予时回填，便于按 id 关联
    username: Mapped[str] = mapped_column(String(64), index=True)
    db_name: Mapped[str] = mapped_column(String(64), default="dwd_shop")
    table_name: Mapped[str] = mapped_column(String(128))
    granted_by: Mapped[str] = mapped_column(String(64), default="")
    granted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # NULL = 永久授权（手工授予沿用旧语义）；审批授予按有效期自动失效
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AccessRequest(Base):
    """数据表权限申请（U-B05）：E003 后的自助开通入口，审批通过写 db_access。"""
    __tablename__ = "access_request"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    requester: Mapped[str] = mapped_column(String(64), index=True)
    db_name: Mapped[str] = mapped_column(String(64), default="dwd_shop")
    table_name: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(255), default="")
    duration_days: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MetricCaliberHistory(Base):
    """口径变更历史（P2）：每次修改前留一份快照，支撑「先口径后取数」的审计链。

    不建外键：口径主记录删除后历史仍需保留。
    """
    __tablename__ = "metric_caliber_history"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric_id: Mapped[str] = mapped_column(String(16), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    metric_name: Mapped[str] = mapped_column(String(64), default="")
    aliases: Mapped[str | None] = mapped_column(String(255), nullable=True)
    definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    logic: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_table: Mapped[str | None] = mapped_column(String(128), nullable=True)
    change_type: Mapped[str] = mapped_column(String(16), default="update")  # create/update
    changed_by: Mapped[str] = mapped_column(String(64), default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class IdSequence(Base):
    """业务号序列（P2）：`INSERT ... ON DUPLICATE KEY UPDATE next_val=LAST_INSERT_ID(next_val+1)`。

    替代「1 + max(尾部数字)」：O(1)、无全表扫、并发不撞号。
    """
    __tablename__ = "id_sequence"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    next_val: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


# ---------------- 配置域（0014）：从 data/*.json 迁入业务库 ----------------
# 迁移原则：库优先、文件兜底；文件不再双写，避免两份真相。首次读到空表时从文件/环境变量种子化。


class LLMApi(Base):
    """LLM API 接入条目（原 data/llm_apis.json）：管理员维护，运行时热切换默认模型。

    0017 起不存明文 Key：api_key_ref 是密钥引用（env:/file:/kms://），运行时由
    app/services/secret_ref.py 解析；解析失败按「未配置」fail-closed 处理。
    """
    __tablename__ = "llm_api"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(255))
    api_key_ref: Mapped[str] = mapped_column(String(255), default="")   # 密钥引用，非明文
    model: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class RolePermission(Base):
    """角色默认权限矩阵（原 data/role_permissions.json）；admin 恒为全权，不落库。"""
    __tablename__ = "role_permission"
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    permissions: Mapped[str] = mapped_column(Text, default="[]")    # JSON 数组
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class ConfigDoc(Base):
    """配置文档（0016）：SharedJson 在 CONFIG_STORE=db 时的落库形态。

    doc_key = 相对 data/ 的路径（skills.json、uploads/index.json），
    payload 为整份 JSON 文本；version 用于排查「谁在什么时候覆盖了配置」。
    """
    __tablename__ = "config_doc"
    doc_key: Mapped[str] = mapped_column(String(191), primary_key=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class KbDoc(Base):
    """文档知识库（0020）：一篇上传文档的登记信息。

    与 `metric_caliber` / `knowledge.json` 的分工：那两处是**结构化条目**（一指标一行），
    这里放**非结构化长文本**（制度、规范、分析报告）。检索时两者都参与，但文档按
    chunk 命中并返回段落级引用，用户能看到依据出自哪一段。
    """
    __tablename__ = "kb_doc"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(512), default="")     # 文件名 / URL / manual
    doc_type: Mapped[str] = mapped_column(String(16), default="md")  # md / text / html
    category: Mapped[str] = mapped_column(String(64), default="通用")
    tags: Mapped[str] = mapped_column(String(255), default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    # 原文另存一份：换 embedding 模型后要能原样重建索引。只靠 chunk 拼回会带上窗口
    # 重叠的重复文本，reindex 一次比一次臃肿，且无法恢复被切分改动的段落结构。
    content: Mapped[str | None] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=True)
    embed_model: Mapped[str] = mapped_column(String(64), default="")  # 建索引时用的模型，换模型需 reindex
    content_sha: Mapped[str] = mapped_column(String(64), default="")  # 内容指纹，用于重复上传识别
    status: Mapped[str] = mapped_column(String(16), default="ready")  # ready / error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class KbChunk(Base):
    """文档切块（0020）：检索的最小单元。

    vector 存 float32 原始字节（小端）：1024 维 JSON 文本约 15 KB，二进制只要 4 KB，
    且检索时不需要 json.loads 整库。维度单独存 `dim`，检索时维度不一致的块直接跳过——
    换了 embedding 模型却忘了 reindex 时，跳过比算出乱序相似度安全。
    """
    __tablename__ = "kb_chunk"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(32), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str] = mapped_column(String(255), default="")   # 标题路径：一级 > 二级
    text: Mapped[str] = mapped_column(Text)
    vector: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    dim: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)


class MetaEmbedding(Base):
    """元数据文档向量缓存（0021）：表/口径/字段的向量，按文本指纹复用。

    为什么必须落库：表 + 口径 + 字段约 130 份文档，走 API embedding 全量编码要
    2~5 秒；每个进程启动都重算一遍，等于把「重启」变成「首问必慢」。有了这张表，
    只有文本真正变了的文档才会重新编码，其余直接读缓存。

    换 embedding 模型时无需手工清理：`model` 与 `text_sha` 同时参与命中判定，
    模型一变，旧行自然不再被使用（再由 warmup 逐步覆盖）。
    """
    __tablename__ = "meta_embedding"
    uid: Mapped[str] = mapped_column(String(191), primary_key=True)  # table:x / caliber:M001 / column:t.c
    doc_type: Mapped[str] = mapped_column(String(16), default="", index=True)
    text_sha: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    dim: Mapped[int] = mapped_column(Integer, default=0)
    vector: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


# ---------------- 数据导入承接层已迁出 ----------------
# 数据源登记与血缘（ingest_source / ingest_dataset / ingest_field_map / ingest_batch /
# ingest_row_error）自 0012 起存放在独立库 datapro_source，模型见 app/db/models_source.py，
# 连接与迁移见 app/db/source_session.py 与 alembic_source/。
# 不要在这里重建这 5 个模型：Base.metadata 会重新把它们建回业务库。

