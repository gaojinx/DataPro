# AGENTS.md — DataPro 项目长期上下文

> 本文件是项目的长期上下文与协作约定。**每次改完代码请顺手更新对应章节**（见文末「维护约定」）。

## 1. 项目定位与阶段

- **项目**：DataPro（数据管家 Agent），面向业务人员的自然语言取数助手：Chat2SQL、元数据/口径问答、数据巡检。
- **核心约束**：全程只读；先口径后取数；召回不到不臆造；全程留痕可审计。
- **当前阶段**：MVP 可运行骨架，核心链路已打通并接本机 MySQL 数仓；未上线。
- **LLM 状态**：本机注册表已配置 DeepSeek 官方接口（`base_url=https://api.deepseek.com`），含两个可切换模型条目：`deepseek-flash`（默认）与 `deepseek-v4-pro`；Key 自 0017 起以**密钥引用**存 `llm_api.api_key_ref`（当前为 `file:data/secrets/llm_apis.env#...`，生产应改 `env:`/`kms:` 引用），运行时由 `app/services/secret_ref.py` 解析，解析失败 fail-closed 按「未配置」处理。管理员可在顶栏模型 chip 直接切换默认模型，下一次请求即生效。单条取数链路实测走真实模型（INTENT→…→PRESENT 全通）。`LLM_MOCK=true` 仅在无可用 Key 时兜底「口径模板降级」；无口径仍转人工（escalate）。评测 50 条种子集真实模型 50/50 通过（详见 §6 评测基线）。

## 2. 技术栈与端口

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | Python 3.14 + FastAPI + SQLAlchemy 2 + pymysql + sqlglot | 自研 9 节点状态机，**不使用 LangChain** |
| 前端 | Vue 3 + Vite 6 + Element Plus（按需引入，`unplugin-vue-components`） + Univer（开源版，按需加载） + SheetJS | Codex 风格三栏对话页；主包 499 kB / gzip 169 kB |
| 后端端口 | `127.0.0.1:8100` | `uvicorn app.main:app` |
| 前端端口 | `5175` | Vite dev，代理 `/api`、`/admin` 到 8100 |
| 数仓 | MySQL 8.0.32 `dwd_shop` | root/123456；只读账号 `ro/ro`；含订单/用户/渠道 + 测试维表 `dim_goods`/`dim_region`/`dim_campaign` 与汇总表 `dws_category_daily`/`dws_channel_daily`/`dws_goods_daily` + 投放日表 `dwd_campaign_daily` + 退款明细 `dwd_refund_detail_di` |
| 业务库 | MySQL `datapro` | 账号 `datapro/pw`；19 张表：query_record / audit_log / message / user_account / export_request / access_request / db_access / meta_table / meta_column / metric_caliber / metric_caliber_history / id_sequence / llm_api / role_permission / config_doc / kb_doc / kb_chunk / **meta_embedding** + alembic_version。`db_access` 增 `expires_at`（NULL=永久，过期后 `tables_for()` 自动不放行）。`query_record` 含 `outcome`/`error_code`/`spec_json`/`sql_final`，可按结果分类与错误码统计 |
| 数据源库 | MySQL `datapro_source` | 独立库，只放数据源登记与血缘（5 张 `ingest_*` 表）；连接串 `SOURCE_DATABASE_URL`，留空时按业务库名推导为 `<业务库>_source`；独立 engine（`app/db/source_session.py`）与独立 Alembic 版本链（`alembic_source/`） |
| 迁移 | Alembic（两棵树） | 业务库 `alembic/versions`（head = `0021_meta_embedding`）+ 数据源库 `alembic_source/versions`（head = `0001_ingest_layer`，独立 `alembic_version`）。`init_db()` **先迁移数据源库、再迁移业务库**（0012 要把 `ingest_*` 复制过去后才能删表）；历史库自动 stamp；`python -m app.db.source_session` 可单独迁移数据源库 |
| 密钥引用 | `app/services/secret_ref.py` | `llm_api` 表只存引用不存明文：`env:变量名`（含项目 .env 兜底）/ `file:路径[#变量]`（dotenv 风格密钥文件，相对项目根）/ `kms://路径[#字段]`（内置 Vault KV HTTP 后端，`SECRET_KMS_URL`+`SECRET_KMS_TOKEN_ENV` 配置；`register_kms_backend()` 可接入云厂商）。解析带 TTL 缓存（`SECRET_REF_CACHE_TTL=60`，轮换自动生效）；非法/明文形态引用在 `validate_ref` 被拒（`SecretRefError` 继承 ValueError→400）；`GET /admin/llm-apis` 只返回引用+脱敏尾号+`key_state`；自检：`python -m app.services.secret_ref check`。0017 迁移自动把历史明文搬到 `data/secrets/llm_apis.env`（0600、gitignore） |
| 数据生命周期 | worker 定时清理 | `AUDIT_RETENTION_DAYS`（180）/`QUERY_RETENTION_DAYS`（365）/`MESSAGE_RETENTION_DAYS`（365），worker 每日 06:30 分批（5000 行/批）删除；依赖 `created_at` 索引，避免全表扫。0 表示不清理 |
| 导入承接层 | 数据源库 `datapro_source` 内 5 张登记表 | `ingest_source`（来源系统）/ `ingest_dataset`（一个数据集 = 一张物理表，登记落地库·主键·增量列·分区列·schema 版本）/ `ingest_field_map`（源字段→目标列 + 类型/清洗/枚举归一）/ `ingest_batch`（批次血缘：文件、sha256、行数、状态、耗时）/ `ingest_row_error`（坏行样本）。物理数据仍是一数据集一张真实表（建议 `ods_{source}_{dataset}`），**不采用单表 JSON 堆叠**，否则 Text2SQL 只能生成 JSON_EXTRACT |
| 数据新鲜度 | `app/services/partition_clock.py` | 相对时间窗锚点 = 基准表 `MAX(dt)`（默认 `dwd_order_detail_di`），10min TTL，启动期与 worker 每 10min 刷新；探测失败回退日历 T-1 不阻断；`PARTITION_READ_ENABLED=false` 完全回退，`EVAL_FIXED_ANCHOR=YYYY-MM-DD` 供评测固定。结果事件带 `data_as_of`/`data_as_of_basis`；**显式「昨天/指定日期」恒按日历**，不随锚点漂移 |
| 限定范围缓存 | `app/services/query_cache.py` | **只缓存意图分类与口径问答，取数结果永不缓存**。键含归一化问题 + 资料指纹（+ 口径问答的 user/role/可见表白名单），资料或权限一变自动不命中，无需失效通知；Redis 优先、不可用降级进程内存；`LLM_MOCK=true` 时整体跳过（mock 是假数据，缓存只会污染测试）。四开关：`QUERY_CACHE_ENABLED`/`CACHE_TTL_INTENT`/`CACHE_TTL_META`/`CACHE_TTL_NEGATIVE`；状态与清空在「设置 → 运维处置」，指标 `datapro_cache_total{scope,result}` |
| 会话状态 | Redis | `dp:sess:{sid}` 哈希 + `:msgs` 列表，TTL = `SESSION_TTL_HOURS`；不可用时降级进程内存 |
| 共享 JSON 状态 | `app/services/shared_json.py` | 配置域统一入口，两套后端：`CONFIG_STORE=db`（默认，落 `config_doc`：`SELECT ... FOR UPDATE` 行锁 + 短 TTL 读缓存 + 首次自动用现有文件种子化）或 `file`（回滚开关，mtime 指纹 + 跨进程文件锁 `fcntl/msvcrt` + `os.replace` 原子写）。库不可用熔断 30 秒后重试，期间读写都退回文件；损坏文件优先读 `.bak`、否则改名 `.corrupt` 并回退默认值。覆盖技能/知识库/巡检规则与结果/定时任务/口径裁决/学习数据/记忆/文件索引/会话登记 |
| 文档知识库 RAG | `app/services/doc_rag.py` + `embed_client.py` | 0020 引入，与元数据召回**并行**而非替换：元数据靠精确标识符（`amount` vs `total_amount` 向量化会混），长文本靠语义。上传 `.md/.txt/.html` → 解析（html 的 h1~h6 转 `#` 标题）→ 按标题路径分段、段内按句末切窗（`DOC_CHUNK_SIZE`/`DOC_CHUNK_OVERLAP`）→ 批量 embedding 落 `kb_chunk.vector`（float32 小端 BLOB，比 JSON 省 70%）→ 检索时**向量余弦 + 字符 2-gram 覆盖率**加权融合，低于 `DOC_RAG_MIN_SCORE` 整条丢弃（回答「未收录」而不是拼低分片段）。embedding 必须配 `EMBED_BASE_URL`/`EMBED_MODEL`/`EMBED_API_KEY_REF`（密钥引用）三项，缺任一项或调用失败自动降级为纯词法检索；**未配置时入库仍成功但 `embed_model` 为空**，前端标「未向量化」，可事后 `POST /docs/{id}/reindex` 补。换 embedding 模型必须 reindex：`kb_chunk.dim` 与当前维度不符的块在检索时直接跳过并告警，绝不算错相似度。原文另存 `kb_doc.content`，reindex 才可重复执行（从 chunk 拼回会带重叠重复）。命中经 `answer_meta(ctx, recall, docs)` 注入 `prompts/doc_qa.txt`，回答按 `[n]` 引用，`sources` 增加 `type=doc`（标题 § 章节）。管理接口 `/api/v1/knowledge/docs`（列表/上传/删除/reindex/stats/search 预览），权限点 `knowledge.manage` |
| 图片理解 | `app/services/vision.py` | 主模型（V4-Flash / V4-Pro）**不接受图片**，图片必须先交视觉模型 `deepseek-v4-flash-vision-exp` 转成文字描述，才能进入问答与取数链路。格式按**魔数**判定（扩展名不可信：`.png` 实为 JPEG、或把别的文件改名成图片都很常见）；base64 `data:` URL 走 OpenAI 兼容的 content 块数组，图片只能出现在 user 消息里。`VISION_MODEL` 可配、`VISION_ENABLED=false` 一键关；`VISION_BASE_URL`/`VISION_API_KEY_REF` 留空即复用 LLM 注册表默认项的地址与 Key（换 Key 不用改两处）。**失败时不让模型自由发挥**：未配置 / 模型不支持 / 超限 / 非有效图片 / 调用失败 五类原因由 `image_fallback_text` 给出确切说明与下一步（表格截图导 CSV），并且**完全不调模型**——否则模型看到只有文件名，会编出「当前只接收文本」这种与事实不符的解释，这正是「上传图片分析报错」的真实成因。解析结果写入 `ctx.image_notes`，`generator` 拼到 prompt 末尾并显式标注「不是数仓数据、不得当作查询结果」；节点 `VISION` 进步骤时间轴。单次最多 3 张、单图 10MB |
| 可观测 | Langfuse（可选） | `tracing` 薄封装：未配 Key 全部 no-op；trace/generation/score 分别走 OTLP 与 scores 接口 |
| 上传临时库 | MySQL `dp_uploads` | 会话文件解析成 `f_{file_id}` 表（多 sheet 为 `_s2`…） |
| LLM | openai 兼容 SDK | 支持 DeepSeek/Qwen 等；注册表 JSON 持久化 |
| 认证 | PBKDF2 密码哈希 + HMAC 签名 Token | B12 已完成；角色 admin/user + 10 个权限点；Token 默认 12h，只带 `jti`/`epoch`（**不含 role/perm**）；退出写黑名单；角色/权限/状态每次请求回查 `user_account`（`auth_state`，3s 缓存 + bump 主动失效），改密/禁用/改角色/改权限在同一事务递增 `user_account.token_version`（迁移 `0010_token_version`）使旧令牌立即失效；`POST /auth/refresh` 续期 |
| 限流 | Redis 固定窗口 + 内存降级 | 按用户每分钟计数（默认 30，`RATE_LIMIT_PER_MINUTE`，0 关闭）；超限 E007/429 |
| 部署 | Docker + K8s | 后端/前端 Dockerfile + nginx 反代（SSE 关闭缓冲）；`ops/k8s/datapro.yaml` 含 API/worker/web/Service/Ingress/HPA/PVC；`INIT_DB_ON_STARTUP=false` 时由 initContainer 迁移 |

## 3. 目录结构

```
AgentProject/
├─ AGENTS.md                   # 本文件：长期上下文
├─ .gitignore                  # 已排除 node_modules/dist/运行数据/密钥
├─ docs/                       # 29 份项目文档（章程/HLD/接口/权限/评测/灰度/Runbook + 现状分析与优化需求池 / 架构评审与技术优化方案 / 团队协作规范与交付流程 + 缺口分析-使用者与卖家视角 / 缺口分析-前后端技术评审 + 缺口分析-前端 / 缺口分析-后端 + JSON字段取数评审 + 页面体验小功能清单 / 页面体验小功能-技术评审 + 页面体验小功能-前端 / 页面体验小功能-后端 + 前端遗留项-后端配套 + Agent执行能力-场景与边界 / Agent执行能力-架构设计 + 高危B能力-安全开放设计 + 本地向量编码器-导出与部署）
└─ datapro/
   ├─ app/
   │  ├─ main.py               # FastAPI 入口 + trace 中间件 + 启动建表
   │  ├─ config.py             # pydantic-settings，全部配置走环境变量
   │  ├─ prompts.py            # Prompt 加载
   │  ├─ api/                  # routes_chat（SSE）/ routes_admin / routes_files / routes_auth /
   │  │                        # routes_skills（F38）/ routes_schedules（F39）/ routes_knowledge（F40）/ routes_trace（F41）/ routes_inspections（A08/F16）/ routes_exports（B13/F11）/
   │  │                        # routes_learning（A11 技能规范/记忆的列表、启停、删除，权限点 skills.manage）/
   │  │                        # routes_access（U-B05 权限自助申请 + 可申请表目录）/ routes_usage（S-F05 用量统计，query_record 只读）
   │  ├─ core/                 # statemachine（9 节点）/ time_window（时间语义确定性解析）/ errors（E001~E010）/ trace
   │  ├─ db/                   # models（业务库）/ models_source（数据源库 BaseSource + 5 张 ingest 表）/ session / source_session
   │  ├─ llm/                  # client（超时/JSON 容错/备用模型）/ tools
   │  └─ services/             # intent / recall / generator / validator / executor /
   │                           # answer / file_store / file_ingest / db_identity / secret_scan（上传内容疑似密钥告警）/
   │                           # session_registry / session_janitor / session_state（Redis 会话状态）/ tracing（Langfuse）/ llm_registry / audit / persist / auth / rate_limit（B14 限流）/ token_store（Token 黑名单 + 账号令牌版本）/ permissions（权限矩阵）/
   │                           # skill_registry（技能包）/ smalltalk（寒暄与自我介绍）/ schedule_registry（定时任务）/ schedule_runner（调度执行）/
   │                           # knowledge_base（新媒体行业知识库）/ web_search（联网搜索）/
   │                           # query_spec（语义中间表示 IR + 多轮槽位）/ fewshot（按查询形状检索示例）/
   │                           # spec_check（IR 与 SQL 形状一致性校验）/ caliber_ambiguity（口径歧义检测与选择解析）/
   │                           # masking（返回层脱敏）/ metadata_store（元数据库表读取 + 种子回退，60s 缓存）/ feedback_pool（👍 自动候选池）/
   │                           # data_boundary（F50 数据边界问答：直读数仓 dt 分区 MIN/MAX）
   │                           # inspection（巡检规则 + 执行 + 结果持久化 + webhook 告警，A08）/ export_registry（导出申请与审批，B13/F11）/ webhook（飞书/钉钉/企微通知）/ file_diff（文件版本单元格级对比）/
   │                           # caliber_rulings（D03 口径裁决）/ metrics（I03 Prometheus 计数器）/ db_access（A02 用户级数据表权限）/
   │                           # auto_learn（A11 自动学习）/ learned_skills（技能规范）/ memory_store（长期记忆）/
   │                           # vector_recall（A05 向量路：OpenVINO 编码可选 + 纯 Python 余弦 + 关键词优先融合）/
   │                           # doc_rag（0020 文档知识库：解析切分 + 向量/词法混合检索 + 段落引用）/ embed_client（API embedding：批量/重试/归一化/fail-closed）/ embedding_store（0021 元数据向量缓存）/ vision（图片理解：视觉模型转文字 + 诚实降级）
   ├─ alembic.ini / alembic/    # I05 业务库迁移：env.py 从 app.config 取连接串，versions/0001_baseline ~ 0021_meta_embedding
   ├─ alembic_source.ini / alembic_source/  # 数据源库迁移（独立版本链）：env.py 取 SOURCE_DATABASE_URL，versions/0001_ingest_layer
   ├─ importer/import_metadata.py  # D05 元数据导入：XLSX/CSV → meta_table/meta_column/metric_caliber（dry-run/upsert）
   ├─ importer/export_vector_docs.py  # A05 向量库文档导出：表/口径/知识库 → JSONL（uid/type/text/payload）
   ├─ importer/ingest_vectors.py      # A05 灌 Milvus：文档 + OpenVINO 编码向量 → datapro_meta（批量/重建/dry-run）
   ├─ worker/inspection_worker.py  # 每日 06:00 巡检 + 每小时会话回收
   ├─ worker/align_seed_dates.py   # 日终种子日期对齐（默认 dry-run，--apply 执行；执行后固定日期评测需重取基准）
   ├─ worker/check_langfuse.py     # Langfuse 健康检查 + trace 上报自检
   ├─ Dockerfile / requirements.txt # 后端镜像（API 与 worker 共用）
   ├─ ops/langfuse/                # Langfuse v3 自托管 docker-compose + .env.example
   ├─ ops/k8s/                     # K8s 编排（datapro.yaml）与 secret 示例
   ├─ ops/mysql/init/              # MySQL 首启脚本：创建 datapro_source 并授权（仅空数据卷执行）
   ├─ eval/                    # runner（分类指标）+ harvest（历史问答转 golden）+ datasets/
   │                           # eval_v1_seed.jsonl（种子集）/ harvest_draft.jsonl / failure_taxonomy.jsonl / eval_golden.jsonl
   ├─ prompts/                 # intent / clarify / sql_gen / sql_fix / answer / meta_qa / **doc_qa（文档 RAG 引用式回答）** / reject / auto_skill（A11 沉淀判断）/ auto_memory（A11 记忆抽取）/ fewshot.json（形状示例）
   ├─ poc/openvino_embed/      # OpenVINO embedding 加速 POC（独立 venv，不接入主链路；结果见 results.md / results_int8.md / recall_consistency.md）
   ├─ tests/                   # 55 个测试文件，368 项
   ├─ frontend/public/branding.json  # 品牌白标（产品名/logo/版本/强调色），缺省即 DataPro
   ├─ frontend/                # Vue3 + Vite
   │  └─ src/{App.vue, main.js, style.css, api/{chat,admin,files,auth,skills,schedules,knowledge,trace,inspections,exports,learning}.js, components/LoginView.vue, editor/UniverSheet.vue}
   ├─ data/                    # 运行数据（上传文件、密钥文件 data/secrets/），已 gitignore；llm_apis.json 已删除（Key 走密钥引用）
   ├─ .tmp/                    # 构建/临时目录，已 gitignore
   └─ pyproject.toml / docker-compose.yml / .env.example
```

> `univer-demo/` 是独立演示项目，已从本仓库忽略。GitHub 远端：`git@github.com:gaojinx/DataPro.git`（分支 `main`）。

## 4. 已实现功能与关键设计决策

### 4.1 已实现（与 `docs/DataPro-功能清单.md` 对应）

- **取数链路**：意图识别 → 澄清 → 召回 → 生成 → 校验 → 执行 → 结果呈现；失败重试/升级；统一错误码。意图识别为「LLM 分类 + 规则兜底」（B04）：规则先判越界/口径，只有规则判取数时调一次模型复核（`intent_llm`，JSON 输出 query/meta_qa/smalltalk/reject，带最近 3 轮提问），模型 reject → forbidden、smalltalk 在流水线内忽略；`LLM_MOCK=true`/无配置/超时/解析失败回退规则；`INTENT_LLM_ENABLED` 可关。
- **只读执行**：sqlglot 校验（写操作/表白名单/危险函数/LIMIT/字段存在性）+ 只读账号 + 会话身份透传 + 超时 + 行数截断 + 日期占位符 `{date_start}`/`{date_end}`。
- **时间语义解析（确定性）**：`app/core/time_window.py` 负责把"昨天/上周/上月/近 N 天/近 N 月/指定日期/区间/季度/年"解析成闭区间；模型只写 `{date_start}`/`{date_end}`，不做日期算术。时间歧义（环比/同比/两月对比/月初月末）转澄清并给出对比期选项；完全没提时间时取默认最近 7 天，并在 assumptions 中声明「默认值」；多轮追问沿用上一轮时间窗。
- **语义中间表示（F43/IR）**：`app/services/query_spec.py` 把问题抽成 `QuerySpec{metric, time_phrase, grain, dimensions, filters, ambiguous}`，链路变为 INTENT → RECALL → **PARSE → TIME** → 澄清/GENERATE。IR 负责锁定指标/时间/维度，原句仍完整传给生成模型（只给 IR 会丢语义）。多轮缺失槽位从上一轮继承（`_SESSION_SPECS`），"按品类拆分""那 11月16日到11月22日呢"都能正确补全。`grain` 只表示输出粒度，窗口只覆盖一个时间桶时自动回落为 `total`（避免返回序列而非单值）。抽取失败（无模型/超时）退化为规则抽取，不阻塞链路。
- **few-shot 按形状检索**：`app/services/fewshot.py` + `prompts/fewshot.json`（静态）+ `data/fewshot_dynamic.jsonl`（历史，A06）。形状 = 输出粒度（单值/时间序列/维度分组）× 指标类型（count/sum/avg/ratio），按 IR 打分，同形状最多取 2 条（静态+历史，历史同分优先），最多 3 条；示例 SQL 一律用 `{date_start}`/`{date_end}`。历史示例由 `python -m eval.promote_fewshot` 从复核过的 golden 生成：固定日期能对上 `expect.time_window` 的自动替换为占位符，替换不了的整条跳过。示例错一条会污染全部生成，新增示例前必须过校验器。
- **结构化重试**：每轮重试都改变输入——已有上一版 SQL 时走 `sql_fix` 定向修正（原 SQL + 校验错误 + 表结构/口径/时间窗/记忆/历史规范，只改出错部分，prompt `sql_fix` v2）；无上一版时带校验错误重生成、示例加到 3 条、温度 0.2 跳出确定性重复；第 2 次退化为「最小可执行查询」（单表单指标、不加额外维度/排序/子查询）；模型连续不给 SQL 时追加「必须输出 SQL，不确定写 assumptions，禁止 need_info」。
- **口径裁决（D03）**：管理员通过 `/admin/calibers/rulings` 对反复歧义的口径词裁决默认口径（`data/caliber_rulings.json`）。歧义检测时若命中裁决且该口径在候选中，直接采用并在「依据」写明；未裁决的歧义仍然澄清。裁决只做默认值，用户显式选择（A/B）优先。
- **监控与告警（I03/I04）**：`GET /metrics` 输出 Prometheus 文本（`datapro_chat_requests_total`/`datapro_chat_outcomes_total`/`datapro_errors_total`/`datapro_rate_limited_total`），进程内计数、无第三方依赖，`METRICS_ENABLED=false` 可关；告警规则在 `ops/prometheus/alerts.yml`（错误率、限流、无流量、拒答占比）。Grafana 看板与 Loki 属部署侧。CI：`.github/workflows/ci.yml` 跑后端 pytest + 前端 `vitest run` 与 build（本地无 Linux 环境，未真机验证）。
- **口径歧义检测（A10）**：`app/services/caliber_ambiguity.py` 在同一指标词命中多个口径（如「新增用户」M004 注册 / M005 下单）且公式不同时先澄清，列出 A/B 与定义摘要，不生成 SQL；会话保存待选口径，下一轮支持 A/B、编号、口径名与「注册/支付」关键词选择，命中后恢复原问题并由 `forced_caliber_id` 锁定召回；未选择且仍歧义 → E001，绝不猜口径；口径依赖表自动进入召回（下单口径带出订单明细表），生成 prompt 标注「已确认口径」。
- **IR 一致性校验（F47）**：`app/services/spec_check.py` 在 VALIDATE 通过后、EXECUTE 之前比对规格与 SQL 形状（纯规则，不调模型）：规格 `grain=total` 且无维度却出现顶层 `GROUP BY`、问题里点名的维度却没有 `GROUP BY`、规格有时间范围却未用 `{date_start}`/`{date_end}`（硬编码日期）→ 回炉一次；仍不一致则 `escalate(reason="IR_INCONSISTENT")`，绝不返回形状不符的结果。只校验 `source=llm` 的规格（规则抽取可能漏维度）；问题含「看看/明细/列表/哪几」等明细诉求、或 `metric` 处于 ambiguous 时不按 total 判定，避免误伤。
- **结果语义自检**：0 行、或单值聚合为 0 时，跑一次时间窗探针（COUNT），区分「时间窗内无数据」与「业务量为 0」，说明写入 assumptions，绝不改数值；聚合为负/为空也给出提示。
- **数据边界问答（F50）**：问「数据最新到几号 / 数据覆盖哪些日期 / 有多少天数据」时不再拒答——`app/services/data_boundary.py` 用规则识别这类问法（负例「最近 7 天销售额」「昨天支付订单数」不误判），对白名单内带 `dt` 的表直接执行 `MIN(dt)/MAX(dt)/COUNT(DISTINCT dt)` 探针，先给「最新数据到 X 日，覆盖 A ~ B」，再列出各表明细（点名某张表时只探该表；点名不存在的表仍走 E003）。答案来自真实分区，不经过生成模型、不编造数值。意图规则把边界问法直接归为 `meta_qa`，避免被意图模型判成越界。
- **文件能力（F27）**：CSV/TSV/JSON/XLSX 上传 → 解析（多 sheet）→ 临时表 → 可查询；2 万行 × 50 列；列名 `c1..cn` + 原始表头注入 prompt。
- **会话隔离安全**：每会话一个 MySQL 只读账号，按表授权；共享 `ro` 的 `dp_uploads` 整库权限已回收；删除/过期 DROP 表 + REVOKE + DROP USER。
- **管理能力**：LLM API 注册表（增删改/测试/设默认/Key 脱敏），仅管理员（Token 角色校验）；设置页「对话记录」分组（F49）按用户/天数/反馈/关键词筛选并分页，列表按时间倒序合并 `query_record`（取数轮次，带 SQL/反馈/标记码）与 `message`（全量消息，含闲聊/澄清/技能，来源列区分），行内展开上下文与回答，接口 `GET /admin/records`（B20）。
- **登录与鉴权（B12）**：独立登录页；PBKDF2 密码哈希；HMAC 签名 Token（默认 12h，含 `jti`/`epoch`/`perm`）；聊天/反馈/会话接口要求登录；会话按创建者归属；文件接口（上传/列表/预览/下载/锁定/删除）全部要求登录，文件记录带 `owner` 并按上传者校验（管理员可访问全部，历史无归属文件放行），预览/下载因浏览器原生请求无法带 Header 支持 `?token=`；退出登录把 `jti` 写入黑名单，改密/禁用/改角色/改权限递增 `epoch` 让该账号旧令牌立即失效（`token_store` 优先 Redis、故障降级内存）；`POST /api/v1/auth/refresh` 用有效令牌换新令牌，前端每 10 分钟检查、剩余不足 2h 自动续期；用户管理（新增/编辑/删除/改密/改角色/启停）在设置页「权限与角色」。**登录态失效统一跳登录页**：前端 `sessionExpired()` 清本地 Token/用户并派发全局事件，各 API 模块遇 401 均调用；打开页面时先校验 Token 过期时间，过期直接显示登录页（不先弹错误）。演示账号 `admin/admin123`、`analyst/user123`。
- **用户级数据表权限（A02/S1）**：`app/services/db_access.py` 维护「用户 → 可访问表」映射，存储优先业务库 `db_access` 表（Alembic 0009，0015 增 `expires_at`），库不可用降级 `data/db_access.json`；60s 缓存、写入即失效。规则：**未配置（无记录）= 沿用全局表白名单**（保持既有行为与演示账号可用）；配置后只允许所列数据表；管理员不受限；**删除用户时同步清理其表权限**（避免同名账号重建继承旧授权，清理失败只记日志）。状态机在 VALIDATE 前按 `ctx.user_role` 与 `ctx.user_id` 取用户白名单，SQL 引用越权表时直接 `reject(E003)`、不消耗重试（重试无法获得权限）。管理接口 `GET/PUT /admin/users/{id}/table-access`（`users.manage`），设置页「权限与角色」用户表单提供「继承全部表 / 仅限勾选表」与按元数据表勾选。
- **终态事件必带错误码（P2 收口）**：`escalate` 过去有两条路径不带 `reason_code`（模型连续给不出 SQL 的那条连 `message` 都没有，前端只能显示默认兜底文案），导致 `query_record.error_code` 全表为空、按 E001–E010 统计错误率时整段消失。现已统一：`NO_SQL`/`IR_INCONSISTENT`/`SCAN_TOO_LARGE` 分别带 E010/E010/E009 与可读文案，`routes_chat` 再用 `fallback_reason_code()` 兜底，任何终态都不会漏码。
- **统一错误映射（F58）**：任何对外错误都带 `code/message/detail/trace_id`——`app/core/errors.py` 定义 CODE_CATALOG（E001~E010）、HTTP 状态码→码映射与人话；`app/main.py` 注册四类处理器（DataProError / RequestValidationError / StarletteHTTPException / 兜底 Exception）。`message` 是可直接展示的人话（不含敏感堆栈，原始异常只进 `detail` 供排障），路由级英文 detail（Not Found 等）自动替换为映射人话，具体中文 detail 保留原文。`detail` 仍保留字符串以兼容旧前端/测试。前端 `frontend/src/api/errors.js` 定义同款映射 + `parseResponse()`/`toApiError()`，全部 API 封装统一委托；弹窗显示「[码] 人话（追踪号 X）」，不再只显示裸 500；SSE error 事件带映射后的 `reason_code`；**401 由 App.handleAuthExpired 统一弹一次、API 层静默**，避免并发请求弹出多条重复提示。回归测试 `tests/test_error_mapping.py`（12 项）。
- **页面体验小功能（F59~F61，X-* 18 条）**：按《DataPro-页面体验小功能-前端》落地 17 条（X-A01 字号三档按技术评审裁决移出、单独立项）。首批：整表 TSV 复制（已脱敏、转义制表符/换行/引号）、空值仅 `null/undefined` 显示 `—` 且 **`0` 原样显示**、发送键 `data-tip` 明示快捷键、消息级本地时间戳（审计仍以服务端 `created_at` 为准）、结果表显示序号、空状态语义图标。迭代档：草稿按 `dp_draft_{username}` 300ms 落盘并在发送/插入/登出清理；示例卡「填入」图标（`@click.stop` 避免误发）；表头三态排序与每页 50 行分页均生成**视图**，`msg.result.rows` 原数组只读且不新增网络请求（后端已硬帽 200 行）；单一全局秒级 tick 显示运行中耗时；单色骨架占位（禁 shimmer/光晕）与反馈 160ms 回弹均纳入 `prefers-reduced-motion`；顶栏两级面包屑。条件项：后端 result 事件透传 `est_scan_rows`（复用 EXECUTE 前已算出的 `estimated`，**不另发 EXPLAIN**）与 `numeric_columns`（在 `default=str` 之前按 `int/float/Decimal` 推断，DECIMAL 金额序列化后与订单号同为字符串，前端只能靠白名单区分）；前端显示预估 chip、按白名单 display-only 千分位（编码列原样），输入框纯字数计数不截断。回归：`tests/test_statemachine.py` 新增 1 项（22 项通过），前端 build 通过。
- **右侧栏常驻开合把手（F70）**：`App.vue` 在 shell 内加 `.panel-rail`，仅 chat 视图显示；面板关闭时贴屏幕右缘、打开时贴面板左缘（`right:300px`，窄屏 320px），图标 ArrowLeft/ArrowRight 随状态切换，title/aria 给「展开文件与搜索/收起侧栏」。原有顶部文档图标与 `Esc` 关闭入口保留；`togglePanel()` 仍是单一状态源。浏览器实测开/关/Esc 均正常，把手右缘与面板左缘 gap=0。

- **对话待发送小框（F69）**：回答进行中再次发送的内容进入输入框正上方的 `components/QueuedMessage.vue`（参考 WorkBuddy：浅灰紧凑卡片、与输入框同宽贴合、左侧拖拽点 + 内容、右侧三图标）。三个操作：↑ 插入输入框（取回内容、恢复草稿并聚焦）、修改（点正文/图标进 inline 编辑，Enter 或失焦提交、Shift+Enter 换行、Esc 取消、清空视为删除）、删除；受控组件只派发 `update:text/remove/insert`，不自治。`queuedText` 在登出/新会话/切换会话/停止生成时均清理，覆盖旧内容时提示「已替换」。6 项交互测试，前端合计 75 项。

- **前端测试（F62）**：引入 Vitest + `@vue/test-utils` + happy-dom（`npm test`，CI 同步）。表格/草稿/时间逻辑抽到 `frontend/src/utils/{table,draft,time}.js`；结果表抽为 `frontend/src/components/ResultTable.vue`，设置页抽出 `SettingsTabs/SettingsAppearance/SettingsAccount.vue`（页签分组与选中、主题上报、账户信息与能力/管理员表），App.vue 通过 props/emits 受控引用，`msg.result.rows` 保持只读。测试 **69 项 / 10 文件**：纯函数 40（TSV 转义、`null→—` 且 `0→0`、数值白名单、排序不改原数组、分页/序号、草稿账号隔离与配额降级、错误映射含 401 静默与 `x-trace-id`）+ ChartView SSR 6 + ResultTable 交互 9 + LoginView 5（空密码校验、演示号、失败文案、密码眼睛、loading）+ 设置页三组件 9。顺带修正两个缺陷：无业务码时 404 误用 E002 业务文案（改为优先 HTTP 状态语义）；上一轮分页块插入 `v-if` 与 `v-else` 之间导致小结果表下方误渲染图表（已改为两个显式 `v-if`）。
- **学习数据存储兼容（A11 修复）**：`memories.json` / `learned_skills.json` 历史遗留为 `{"memories": [...]}` 包装格式，读路径早已兼容、写路径没有，导致新增记忆/技能抛 `'str' object has no attribute 'get'`（删除用户时只看到一条 warning，实际 A11 静默失效）。已加 `_edit_items()` 统一取内层列表、保持文件原形态，`add/upsert/update/delete/clear_user` 全部可用。
- **权限自助申请（U-B05）**：用户被 E003 拦截后可在 `POST /api/v1/access/requests` 提交申请（`GET` 查本人进度），管理员用 `GET /admin/access/requests` + `POST /admin/access/requests/{id}/approve|reject` 审批。通过后按有效期（默认 30 天、夹在 1~365 天；显式 0 夹到 1 而非回落默认）增量写 `db_access.expires_at`，到期由 `tables_for()` 自动过滤，无需撤销任务；**增量授予不清空该用户其他表**（`grant_table` 与整体覆盖的 `set_tables` 分开）。全部过期返回空集合而非 None，避免静默回退全局白名单（那是提权）。同一用户同一张表只允许一条 pending；表名必须在元数据内；审批失败（写库异常）保持 pending 并返回 503，不出现「显示已通过但没权限」。前端在设置页「权限申请」提交/查进度/审批，E003 事件回传 `tables` 支持一键预填，通知中心聚合待审批与驳回。前端入口：设置 →「权限申请」（`GET /api/v1/access/catalog` 提供表名+中文名，不含字段；普通用户可读，数据边界仍由 A02 + SQL 校验 + 只读账号守），同一页面管理员可见待审批列表并直接通过/驳回。
- **权限矩阵（B12）**：`app/services/permissions.py` 定义 11 个权限点（chat.use / files.upload / files.delete / skills.manage / schedules.manage / knowledge.manage / inspections.manage / users.manage / llm.manage / eval.run / sessions.sweep）；管理员固定全权；普通用户默认 chat.use + files.upload + files.delete + skills.manage + schedules.manage + inspections.manage；角色默认矩阵存 `data/role_permissions.json`（管理员可在设置页调整），用户级覆盖存 `user_account.permissions`（Alembic 0003，NULL 表示继承）；令牌携带 `perm` 快照，权限变更加 `epoch` 失效旧令牌；写接口统一用 `require_permission(code)`，前端按钮用 `can(code)` 显隐。**改默认矩阵后已登录用户需重新登录**（旧令牌的 perm 是快照）。
- **请求限流（B14/E007）**：`app/services/rate_limit.py` 用 Redis 固定窗口按用户每分钟计数（默认 30，`RATE_LIMIT_PER_MINUTE=0` 关闭），超限 `/api/v1/chat` 返回 429 并提示可重试秒数；Redis 不可用降级进程内存计数。
- **前端可见性隔离（B12 补充）**：普通用户侧栏为「对话」「技能」「定时任务」「巡检」「导出记录」「设置」；管理员侧栏为对话/技能/定时任务/知识库/巡检/导出审批（不含设置，设置收进头像二级菜单）。头像二级菜单仅管理员可用：通用分组（外观风格/账户与统计）+ 管理员分组（API 管理/数据源/权限与角色/对话记录），普通用户点头像无反应（无 chevron、无菜单）。「账户与统计」对管理员额外展示全部账户表（账号/显示名/角色/状态/最近登录，只读，维护仍在权限与角色），普通用户只看本人信息。`openView` 兜底校验（仅「知识库」为管理员专属，非管理员传入时回落对话；技能/定时任务/巡检/导出视图可进，写操作仍由权限点控制）；设置分组按角色过滤，普通用户只有「外观风格」「账户与统计」，`switchSettings`/`openAccountView` 同样校验；换账号登录重置回对话首页。后端读接口同步收紧：`/api/v1/skills*` 读需登录（`all=1` 需 `skills.manage`）、`/api/v1/schedules*` 需 `schedules.manage`（按 owner 过滤）、`/api/v1/knowledge*` 需 `knowledge.manage`、`/api/v1/inspections*` 需 `inspections.manage`、`/admin/*` 需管理员、`/api/v1/exports` 全员可读（普通用户仅本人），前端隐藏不作为唯一防线。
- **前端体验**：独立登录页与 Token 登录态；Codex 式侧栏（日期分组/折叠/重命名/删除确认/账户二级菜单/常驻退出），折叠按钮在侧栏品牌行，桌面收窄为 56px 图标栏（宽带动画，收起后保留新对话/对话/账户头像图标，会话列表隐藏、账户区贴底、账户菜单右移弹出），移动端走顶栏抽屉；对话区 Codex 化——用户紧凑气泡、处理过程「已处理 N 步 · 耗时」折叠时间轴（运行中流式显示步骤）、SQL 代码卡带语言标签与复制、消息操作 hover 浮现、输入区 18px 圆角无重阴影（按钮三态：停止 / 运行中「插入」排队 / 空闲「发送」）；无弹窗主区视图、右侧文件与搜索面板、文件来源筛选、消息复制/重生成、导出 MD/CSV、Ctrl+K、暗色主题。删除类操作（会话/文件/技能/定时任务/词条/模型/用户/导出文件）统一为「首点进入确认态、再点执行」的 3 秒两步内联确认；**回答进行中或文件编辑中禁止新建/切换/删除会话与切换视图**（提示先停止或保存/取消），删除当前预览文件自动退回对话，切换会话清理导出表单/反馈面板/溯源结果/折叠状态；文件编辑期间隐藏返回与版本入口；顶栏文件面板与 Ctrl+K 在非对话视图自动回到对话；会话快照写 localStorage 有配额兜底。
- **二级设置页（F37）**：横向分组标签（数据源 / 模型与AI / 外观风格 / 权限与角色 / 对话记录 / 账户与统计），按角色过滤：普通用户只看到外观风格与账户与统计；模型表格列为名称/Base URL/模型/API Key/操作，支持设为当前、测试连接、编辑、删除；添加/编辑为内联表单（无弹窗）。
- **文件预览（F34）**：文件面板或消息附件点击进入主区预览；表格按工作表切换（前 200 行，数据来自解析临时表），图片/PDF 内联渲染；接口 `GET /api/v1/files/{id}/preview` 与 `download?inline=1`。
- **文件在线编辑（F35/F36）**：CSV/TSV/XLSX 可在主区用 Univer 开源版编辑，保存为新版本文件（`origin=edited`，原件保留）并自动重新解析临时表；XLSX 用 SheetJS 在浏览器读写（保留数据与多工作表名，样式/公式/图表有损）；单表 ≤5000 行、总计 ≤10000 行；打开时加 5 分钟进程内锁。
- **文件版本链与回滚（F49/B19）**：文件索引新增 `root_id`（首版 id）与 `version`（从 1 递增），编辑保存与恢复都归到同一链；`GET /api/v1/files/{id}/versions` 返回全链，`POST /api/v1/files/{id}/restore` 以历史版本内容生成新版本并重新解析临时表（原件不覆盖）；历史索引启动时自动补字段，旧文件仍可用；预览页显示 `Vn` 徽标与「历史版本」折叠。
- **文件版本对比（F49/B19）**：`app/services/file_diff.py` 提供 CSV/TSV/XLSX 单元格级 diff，行用 `difflib.SequenceMatcher` 对齐以识别插入/删除行，表头单独对比；接口 `GET /api/v1/files/{id}/diff?against=<历史id>`；单表最多 300 处、总计 1000 处变更，超出标 `truncated`；前端在历史版本行点「对比」，展示摘要（修改/新增/删除行/表头）与 `列行 before → after` 明细。
- **技能包（F38）**：侧栏「技能」页对全员开放（`skills.manage`，普通用户默认拥有）；字段为名称/发送内容/回复内容/说明/启停；回复内容非空即「预设回复」技能，发送内容命中后直接回复、不走取数链路（用于口径说明类问题）；启用技能的问法仍作为首页建议卡对全员展示；JSON 持久化 `data/skills.json`。
- **寒暄与自我介绍**：`smalltalk.should_answer_general` 判定非取数问题（寒暄/身份/模型/含糊输入，且不含「时间+指标」）时，交给接入的 AI 模型直接回答（`llm_client.chat`，system 约束不得编造数值），不进入状态机；无可用模型时回退内置说明；口径与取数问题不受影响。
- **口径别名（U-B06，属数据维护非研发）**：`metric_caliber.aliases` 是召回主入口，口语问法靠它命中（「广告ROI/投产比」「营业额/流水」「客单」「退货单数」「新客」等）。改法两条：改 `recall.SEED_CALIBERS`（新装与库不可用时的种子）**且**用 `importer.import_metadata.upsert("calibers", [...])` 写库（会经 `caliber_history` 留快照），两边必须一致。注意 `_caliber_match` 收的问题已转小写，**别名比较也必须转小写**——否则 `GMV`/`ROAS`/`DAU`/`广告ROI` 全部失效（曾长期如此）。加别名后必须跑 `python -m eval.recall_eval` 确认表/口径命中率不回退，并注意别把某个别名做成另一个别名子串（如给 M003 加「订单量」会与 M015「退款订单量」互撞，导致无谓澄清）。
- **新媒体行业知识库（F40）**：侧栏「知识库」页，预置 28 条术语（流量/内容质量/账号增长/广告投放/转化/电商/平台机制/达人合作），字段为术语/别名/分类/定义/计算口径；页面与读取接口仅管理员可见（`knowledge.manage`），对话内术语召回对全员生效（走服务层，不依赖该接口）；页面为「分类筛选（带数量）+ 单行折叠列表」，默认只显示术语/分类/别名/定义摘要，点击行才展开完整定义与口径（不再把 28 条全部平铺成大卡片）；召回阶段并入 `RecallResult.knowledge`，"什么是完播率""涨粉率怎么算"等元数据问答直接命中；JSON 持久化 `data/knowledge.json`。
- **定时任务（F39）**：侧栏「定时任务」页对全员开放（`schedules.manage`，普通用户默认拥有），支持每日/每周固定时间自动执行取数问题，保留最近 20 次运行记录，可立即运行；**普通用户只看/维护本人创建的任务**（接口按 `owner` 过滤，越权访问统一 404），管理员可看全部；**删除用户时同步清理其创建的任务**（失败只记日志，调度循环下一轮自动移除作业）；定时任务执行按任务 owner 身份，因此同样受 A02 用户表权限约束；调度用 APScheduler（`AsyncIOScheduler` + `CronTrigger`，时区 Asia/Shanghai），`schedule_runner.start_scheduler()` 随 FastAPI lifespan 启停，每 30 秒把 JSON 配置同步成 cron 作业（增删改即时生效）；JSON 持久化 `data/schedules.json`。
- **多轮对话**：省略指标的追问（"那上周呢""按品类拆分一下"）不再被当作寒暄，由状态机回溯最近一条带指标的问题合并理解；合并句一律本轮在前（`本轮；本次补充（时间/条件优先于上句）：上一问`），时间窗优先解析本轮原句，其次 IR 时间、再合并句、最后上一轮——修掉"问上周实际沿用昨天"的静默错误。槽位替换支持「换成/改成/改为 + 时间/指标/维度」（如"换成上周""换成销售额""维度换成渠道"），维度替换会覆盖而非叠加上一轮；「再按渠道拆一层」为增量补维度；「它上周呢」按 IR 继承指标。寒暄/能力类回答带最近 3 轮用户提问，可回答"我刚才问了什么"；澄清只问缺失要素（时间/指标/粒度）；天气等无关话题不合并上下文。
- **会话状态（B11）**：`app/services/session_state.py` 把历史消息、会话归属、澄清轮次、多轮槽位存 Redis（`dp:sess:{sid}` 哈希 + `:msgs` 列表，TTL = `SESSION_TTL_HOURS` 默认 24h，最多保留 40 条消息，`dp:sessions` ZSET 按活跃时间索引）；Redis 连接失败自动降级进程内存并只告警一次；多实例共享会话，重启不丢上下文与槽位。**全量消息持久化（B07）**：每轮 user/assistant 消息异步写 MySQL `message` 表（Alembic 0006 加 `username`/`kind`，kind 为 query/general/skill/clarify/reject 等），Redis 过期后仍可审计；写失败只记日志。
- **可观测（F48）**：`app/services/tracing.py` 是 Langfuse 的薄封装，`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` 未配置时全部 no-op。一次提问一条 `chat` chain trace（问题、session_id、user_id、节点事件、SQL、结果摘要、query_id），每次模型调用一个 generation（模型、prompt_v、token 用量、耗时），反馈写 `user_feedback` score；trace id 落 `query_record.langfuse_trace_id`（Alembic 0002），应用关闭时 flush。上报异常只记日志，不影响主链路。自托管编排见 `ops/langfuse/`，连通性用 `python -m worker.check_langfuse` 自检。
- **联网问答（F42）**：天气/新闻/股价等实时类问题走 Bing RSS 搜索（无需 Key，`app/services/web_search.py`），搜索结果作为参考交给模型，回答附「来源」链接并写入 `sources`（前端「依据」里显示“网页”标签）；不得用搜索结果替代数仓数值。`WEB_SEARCH_ENABLED=false` 可关闭。抓取无 Key、结果质量受 Bing 限制，需要更稳定可接博查/Serper 等 API。
- **小基数 GROUP BY 防泄露（A07）**：VALIDATE 阶段拦截「按个体/敏感列分组」——`user_id/uid/member_id/customer_id/order_id/order_no/trade_no/session_id/device_id` 与 `phone/id_card/bank_card/email/address/real_name` 不能作为 GROUP BY 键（按它们分组等于返回个人明细），错误提示引导改按天/渠道/品类等业务维度；`channel_id` 等维表外键正常放行。规则在 `masking.is_pii_column/is_identifier_column`，与返回层脱敏共用一份列名规则。
- **扫描量预估拦截（A09）**：EXECUTE 前用 `EXPLAIN` 预估扫描行数，超过 `MAX_SCAN_ROWS`（默认 200 万）直接 `escalate(reason="SCAN_TOO_LARGE")` 并回 E009，不执行大查询；预估失败自动放行；`SCAN_GUARD_ENABLED=false` 或 `MAX_SCAN_ROWS=0` 关闭。规则在 `executor.estimate_scan`，前端 escalate 现在展示后端 message。
- **巡检（A08/F16）**：`app/services/inspection.py` 规则存 `data/inspection_rules.json`（`rowcount_drop` 行数骤降 / `null_ratio` 空值率 / `no_data` 断流），结果存 `data/inspection_results.json`（最近 200 次）；worker 每日 06:00 执行，失败按 `INSPECTION_WEBHOOK_URL` 推企业 IM，未配置只记日志。管理端接口 `/api/v1/inspections`（结果/规则/立即执行）需 `inspections.manage`（普通用户默认拥有；管理员可在权限矩阵收回），前端侧栏「巡检」页对全员开放，可启停规则、手动巡检、展开逐条明细。
- **动态 few-shot（A06）**：`eval/promote_fewshot.py` 把复核过的 golden 转成 `data/fewshot_dynamic.jsonl`；只收日期已占位化的 SQL，硬编码日期自动替换、无法替换的跳过；`fewshot.py` 按文件 mtime 自动重载，与静态示例合并去重，同形状最多 2 条、历史示例同分优先。
- **自动学习（A11）**：`app/services/auto_learn.py` 在每轮对话结束后异步（`persist.spawn`，失败只记日志）做两件事——① 成功取数（有 result + SQL）时调模型判断是否值得沉淀为「技能规范」，写入 `data/learned_skills.json`（标准问法 + 日期占位化 SQL + 口径 + 注意事项；按用户隔离、归一化问题去重并累加命中、单人上限 30）；② 用户消息含偏好/约定线索（"以后/默认/记住/口径/不要/只看"等）时调模型判断是否值得长期记住，写入 `data/memories.json`（偏好/术语/规则/口径；按用户隔离、去重、单人上限 50、手机号/身份证/银行卡拒绝写入）。生成 prompt（`sql_gen` v7 / `sql_fix` v2）注入当前用户记忆与匹配到的历史规范，并明确「仅参考、口径优先」；规范 SQL 只作结构参考，最终仍走校验与执行。自动生效、可在技能页「技能规范 / 记忆」标签停用或删除；`AUTO_LEARN_ENABLED`/`AUTO_MEMORY_ENABLED` 可关，阈值 `AUTO_SKILL_MIN_CONFIDENCE`/`AUTO_MEMORY_MIN_CONFIDENCE`；删除用户时同步清理学习数据。前端管理页支持条数角标、关键词搜索、记忆类型筛选、统计「共 N · 启用 M」、点击展开完整 SQL/记忆全文，管理员可切「我的 / 全部用户」（默认只看自己）；对话结束后延迟探测本轮新增项，命中后在回答下方提示「已自动学习 N 条技能规范、M 条记忆」并可跳转对应标签。
- **前端水印（S06）**：登录后全屏平铺「用户名 · 当前时间」（每分钟刷新），`pointer-events:none` 不拦截任何交互；仅作溯源提示，不替代后端审计。
- **大结果集导出审批（B13/F11）**：结果卡「申请导出」内联提交（问题/SQL/用途/原查询时间窗），记录优先存业务库 `export_request`（Alembic 0007，时间窗列见 0008；库不可用降级 `data/export_requests.json`）；侧栏「导出」入口对全员开放——管理员显示「导出审批」可看全部并审批，普通用户显示「导出记录」只看本人申请、下载已通过文件；审批通过后用申请人身份与**原查询时间窗**重跑 SQL（不再退化成默认最近 7 天），落 CSV 到 `data/exports/{EP-xxxx}.csv`（上限 `EXPORT_MAX_ROWS` 默认 10 万）并允许申请人/管理员下载；驳回写备注；每次下载写 `audit_log`（node=EXPORT）。
- **向量召回（A05，默认关闭）**：`app/services/vector_recall.py` 在召回阶段补位——关键词命中优先，向量命中按余弦相似度排序填充（`VECTOR_TOP_K`/`VECTOR_MIN_SCORE`）。编码器二选一（`VECTOR_ENCODER=auto/local/api`）：`api` 复用 `embed_client` 的 OpenAI 兼容 /embeddings（推荐，零本地依赖）；`local` 需 openvino + transformers 且 `VECTOR_IR_PATH` 指向有效 IR（数据不出域，镜像约 +1GB）；`auto` 先试本地再退 API，都不可用则整体关闭。**向量落 `meta_embedding` 缓存**（0021，`uid`+`text_sha`+`model` 三者同时命中才复用），只有文本真正变了的文档才重新编码；启动期后台 `prewarm()`，也可 `python -m importer.warm_vectors [--prune]` 手工预热——不预热的话，编码上百份文档的开销会落在第一个用户提问上，而 `TIMEOUT_RECALL=3` 一到就静默退化为纯关键词召回（现象：刚部署时召回特别差）。索引按 `doc_type` 分槽（表/口径/字段三类独立），共用一个槽会互相冲掉索引导致每次全量重编码；`recall.py` 对向量路加 `asyncio.wait_for(TIMEOUT_RECALL)`，超时/异常都退回关键词路。后端两种：默认 `VECTOR_BACKEND=memory`（进程内纯 Python 索引，按文本指纹重建）；`milvus` 走 `MilvusIndex`（pymilvus 延迟导入，按 type 过滤，连不上自动回退内存）。灌库：`export_vector_docs` 生成 JSONL → `importer.ingest_vectors` 编码写入 `datapro_meta`（`--recreate` 重建、`--dry-run` 只编码）。切换：`VECTOR_RECALL_ENABLED=true`、`VECTOR_BACKEND=milvus`、`MILVUS_URI=<uri>`、`VECTOR_TOKENIZER` 与 IR 对应。**检索粒度是「一条元数据 = 一份文档」，不做切片**（表文档平均 176 字、最长 261 字，切开会把「表名+描述+关键词+字段」拆散，生成阶段拿不到完整字段就会编字段）；文档文本由 `vector_recall.table_doc_text/caliber_doc_text/knowledge_doc_text` 单一提供，内存索引与 Milvus 灌库共用，**禁止各写一套**（两套文本不会报错，只会让召回率随部署形态漂移）。上传文件走 F27 解析成真实临时表，不做文本切片；产品目前没有 PDF/Word 长文档摄取入口。**字段级检索（二级文档）**：`type=column`，`uid=column:{table}.{col}`，文本 = 表名 + 表中文名 + 列名 + 列中文名 + 类型 + 业务提示（**不含样例值与 PII 标记**——编码后会落到 Milvus，等于把数据带出边界）；摊平与文本构造函数为 `flatten_columns` / `column_doc_text`，检索与灌库共用。两套命中规则不可混用：`_column_hits`（**全名/英文整词**）用于**表补位**，`_derived_column_hits`（**中心语后缀**，如「预算」→「计划预算」）只对**已召回表**做字段提示——碎片词（「工单数量」→「单数」）若参与补表，会把本该 E002 的问题静默查到订单表。表内只保留匹配最长的字段，避免「支付订单数」与「退款订单数」被同时推荐（实测提示精度 0.298→0.368、召回不变）。生成侧把命中字段排到字段列表最前，措辞为「问题中提到的字段（仅供参考，口径仍以其 logic 为准）」，不用强指令以免带偏。
- **本地编码器（数据不出域）**：`VECTOR_ENCODER=local` 需要 OpenVINO IR 产物，转换依赖（torch + optimum-intel）只在开发机/CI 装一次，**运行只需 `requirements-local-encoder.txt`（openvino + transformers + numpy）**；导入 `python -m importer.export_encoder`（导出后会扫描目录找 .xml 并用 OpenVINO Runtime 重跑一句自检，复用生产同一个 `pool_embeddings`）。`VECTOR_TOKENIZER` 必须指向**本地目录**，配成 HF id 时内网会卡到超时而只报「加载失败」；`VECTOR_POOLING=cls|mean` 选错不会报错、只会让相似度整体偏移（bge 用 cls，部分 sentence-transformers 模型用 mean），输出 2 维句向量的模型会被 `pool_embeddings` 直接放行、3 维取 CLS、其他维度报错而不是静默取 `[:,0]`。换模型/换池化后必须重跑 `python -m importer.warm_vectors`；文档库另需 `POST /docs/{id}/reindex`。完整步骤见 `docs/本地向量编码器-导出与部署.md`。
- **元数据导入（D05）**：`importer/import_metadata.py` 把填报模板写进 `meta_table`/`meta_column`/`metric_caliber`（Alembic 0004）。XLSX 按工作表名识别表级/字段级/口径，CSV 用 `--kind`；`--dry-run` 只校验不写库；按业务主键 upsert。`app/services/metadata_store.py` 统一读库（60s 缓存），库空或业务库不可用时回退 `recall.SEED_TABLES/SEED_CALIBERS`，库表按 `table_name`/`metric_id` 覆盖种子；召回、可查询范围说明、元数据管理接口都走它。导入时未知模板列会被过滤，口径缺 `owner` 兜底空串。
- **测试数仓与元数据字典（D02 测试版）**：`dwd_shop` 除订单/用户/渠道外，新增 `dim_goods`（100 商品，品类/品牌/单价）、`dim_region`（100 城市，区域-省份-城市）、`dws_category_daily`（50 行，天×品类）、`dws_channel_daily`（10 行，天×渠道）、`dim_campaign`（10 个投放计划）、`dwd_campaign_daily`（100 行，天×计划花费/曝光/点击/转化/GMV）、`dwd_refund_detail_di`（20 行退款明细）、`dws_goods_daily`（180 行，天×商品销售额/销量）；订单表补 `region/province/city/goods_id/goods_name/qty` 并回填 500 行，保证每天 7 个区域都有订单。已导入 11 张表、80 个字段、14 个口径（M003 支付订单、M006 销售额、M007 销量、M008 客单价、M009 退款金额、M010 动销商品数、M011 支付用户数、M012 退货率、M013 广告花费、M014 广告ROI、M015 退款订单数等）。
- **返回层脱敏（S04）**：`app/services/masking.py` 在 PRESENT 生成结果事件时统一脱敏——手机号保留前 3 后 4、身份证前 6 后 4、银行卡只留后 4、邮箱保留首字符；列名命中 `phone/mobile/id_card/bank_card/email/address/real_name` 等关键词时整列打码（`channel_name`/`category_name` 等业务列不误伤）；数值列不改，只对字符串打码；表格、CSV 导出、审计落库拿到的都是脱敏后的行。`MASK_ENABLED=false` 关闭，`MASK_EXTRA_COLUMNS` 追加列名关键词。
- **结果呈现**：answer-first——AI 用一句话总结结果（仅可使用返回数值，不得新增数字）→ KPI 大数字（单行聚合）→ 图表/表格 → 「依据」折叠（SQL/口径/行数/耗时）→ 回到底部按钮；口径问答由模型结合知识库/口径/表结构组织答案，无模型时降级为资料原文。
- **溯源标记码（F41）**：每条回答返回 `trace_code`（`DP-` + query_record.id 四位补零，如 `DP-0077`），前端答案底部可一键复制；右侧搜索面板粘贴标记码回车调用 `GET /api/v1/trace/{code}` 回查原始问题、回答、时间、行数，用于不翻会话直接定位问题。
- **反馈与审计（B08/S03/D04）**：结果事件回传 `query_id`；反馈（准确/不对）与原因写 `query_record`；👍 同时把快照写入 `data/feedback_candidates.jsonl`（`feedback_pool`，按 query_id 去重、上限 500、写失败只记日志），`harvest` 自动并入草稿（无断言、标 volatile、reviewed=false），人工复核后才 promote；👎 不进候选池，走 failure_taxonomy。节点级审计异步写 `audit_log`；业务库不可用时降级跳过。`query_record` 存两条信息：`question` 为本轮用户原话，`question_context`（JSON 数组，启动时自动补列）为参与合并的前序提问（含被跳过的中间轮次），多轮追问不再因「只保留上一问」丢失上下文，`harvest` 可直接复现多轮用例；`username`（Alembic 0005）记录提问人，供管理后台按用户筛选（历史记录为 NULL）。
- **评测**：种子集 50 条；分类指标（executable_rate / table_hit_rate / time_window_pass_rate / result_match_rate / false_answer_rate / failure_breakdown）；真实模型基线 50/50。召回专项 `eval/recall_eval.py`（50 条）：表命中 97.5%（39/40）、口径命中 92%（23/25），剩余 miss 为 C024/C025 多轮依赖（召回评测不带 history，线上由会话槽位继承覆盖）。
- **golden 沉淀（F46）**：`eval/harvest.py` 把 `query_record` 转成评测候选，但**不自动并入**——产出 draft（`reviewed=false`）→ 人工复核改 `reviewed=true` → `--promote` 并入 `eval_golden.jsonl`（按 history+question 去重，编号 G001…）。入库前重跑校验与执行，SQL 失效标 `suspect`；👎 不进 golden，单独写 `failure_taxonomy.jsonl`（caliber/time/schema/completeness/performance/presentation）；相对时间（昨天/上月/最近7天）标 `volatile` 且不生成断言，避免数据日期滚动后误报。首批人工复核入库 3 条（G001 销售额 72470.55、G002 单日品类拆分、G003 固定单日 43），`eval_golden` 3/3、错答率 0；同题重复（H109≈C021）、上下文缺失（H115 丢了「按品类拆分」轮次）、无数据窗口（H110 上月 0 单）与相对时间用例均未入库。重跑 harvest 会覆盖草稿，但按 `(history, question)` 保留人工 `reviewed`、手工断言（must_not/min_rows/assumption_contains）与「复核：」开头的备注，时间窗/scalar 仍用重跑值；草稿按同一 key 去重；`promote` 编号从已有 `Gxxx` 最大号往后顺延，二次 promote 不会撞号。

### 4.2 关键设计决策

1. **自研状态机**而非 LangChain，节点可替换、可观测、可测试。
2. **召回 0 命中不编造**（E002/E003）：优先走答案优先（answer-first）——有可用模型时先给「最可能理解」的答案（能落到现有表/口径就说明用哪张表哪个口径），再列其他可能理解与可查询范围（0 行、不出现任何数值）；仍无法组织时 E002 拒答。数据边界类问题（见 F50）直接读真实分区作答，不进入这条分支。
3. **降级不造假**：无 LLM 时仅用口径模板生成；无口径一律 escalate，不返回看似正确的错误数据。模板能力（`generator._template_from_caliber`）：口径 logic 按 `<聚合表达式> WHERE <过滤>` 解析，聚合函数白名单 `COUNT/SUM/AVG/MIN/MAX/ROUND`（含 `SUM(pay_status=2)` 这类布尔求和与 `NULLIF` 防除零），表达式再过字符白名单 + SQL 关键字黑名单两层（`SUM(amount) FROM x` 这种只含字母空格的畸形写法也会被拒）；`{date}` 按**实际日期列**重写成区间（新增用户注册口径用的是 `register_date`，只认 `dt` 会留下字面量 `{date}` 变成废 SQL）；只认 `{date}/{date_start}/{date_end}` 三种占位符，其余一律转人工；按召回顺序逐个口径尝试，第一个不可模板化不代表要转人工。
4. **双重只读防线**：模型输出先过 sqlglot 白名单校验，再由只读账号兜底。
5. **会话级数据库身份**：有解析文件的会话用独立账号；无文件会话回退共享只读账号（共享账号已无 `dp_uploads` 权限，安全等价）。
6. **UI 约定**：功能不使用弹窗，一律主区视图；不以常驻小字解释功能，用图标/徽标/图形/tooltip 表达；视觉遵循 unslop 基线——去渐变/光晕/彩色阴影，统一 Element Plus 图标，字重上限 600，间距对齐 4px 网格，单一强调色。
7. **长对话可扫读**：答案在上、依据在下、过程可折叠。
8. **旁路不阻塞**：审计/记录写入失败只记日志，60 秒内不再重试，不影响主链路。
9. **功能先登记再开发**：新功能先写入 `docs/DataPro-功能清单.md`（含变更登记），再实现。
10. **日期算术不进模型**：时间窗由 `time_window` 确定性解析，SQL 只写 `{date_start}`/`{date_end}`。模型最不擅长的就是日期推算，且算错时会返回"看起来正确"的错误数字（问上月实际查近 7 天），比拒答更危险。
11. **0 单 ≠ 无数据**：单值聚合为 0 时必须探针确认时间窗内是否有分区数据，否则用"0"回答业务问题等于给出错误结论。
12. **先 IR 后 SQL**：question → QuerySpec → SQL，IR 可回显、可澄清、可跨轮继承。但原句必须与 IR 一起进 prompt——只给 IR 会丢掉"有多少…注册过"这类修饰，反而更容易 need_info。
13. **自动学习只做参考、不做事实源**：AI 沉淀的规范与记忆只是生成 prompt 的参考（口径、知识库、表结构始终优先），规范 SQL 不直接执行；自动写入必须可撤销（列表可见、可停用/删除）、按用户隔离、限量并过滤敏感信息，宁可少记不可记错。

## 5. 环境与启动/验证命令

本机特殊路径（Git / Python 均不在 PATH，需用全路径）：

- Python：`C:\Program Files\Python314\python.exe`
- Git：`C:\Program Files\Git\cmd\git.exe`
- `gh` CLI：未安装

```powershell
# 后端（cwd: datapro）
& "C:\Program Files\Python314\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8100

# 前端开发（cwd: datapro/frontend）
npm run dev

# 后端单测（pyproject 已固定 --basetemp=.pytest_tmp）
& "C:\Program Files\Python314\python.exe" -m pytest -q

# 数据库迁移（Alembic；CLI 不在 PATH，统一用 -m）
& "C:\Program Files\Python314\python.exe" -m alembic current
& "C:\Program Files\Python314\python.exe" -m alembic revision --autogenerate -m "add xxx"
& "C:\Program Files\Python314\python.exe" -m alembic upgrade head

# Redis（本机为 Windows 服务，安装目录 C:\Program Files\Redis）
Get-Service Redis
& "C:\Program Files\Redis\redis-cli.exe" ping

# 前端构建（系统 Temp 无写权限，必须改 TEMP）
$env:TEMP='e:\Project\AgentProject\datapro\.tmp'; $env:TMP=$env:TEMP; npm run build

# 评测
& "C:\Program Files\Python314\python.exe" -m eval.runner eval_v1_seed --verbose

# 历史问答 → golden 评测候选（draft → 人工 review → promote）
& "C:\Program Files\Python314\python.exe" -m eval.harvest --days 30 --limit 200
& "C:\Program Files\Python314\python.exe" -m eval.harvest --promote
& "C:\Program Files\Python314\python.exe" -m eval.runner eval_golden --verbose

# 召回质量评测（A05）：表命中/口径命中/miss 明细，报告落 eval/reports/
& "C:\Program Files\Python314\python.exe" -m eval.recall_eval --out eval/reports/recall_report.json

# golden → 动态 few-shot 示例（A06）：硬编码日期自动替换为占位符
& "C:\Program Files\Python314\python.exe" -m eval.promote_fewshot --dry-run
& "C:\Program Files\Python314\python.exe" -m eval.promote_fewshot

# 元数据导入（D05；先 dry-run 校验，再正式写库）
& "C:\Program Files\Python314\python.exe" -m importer.import_metadata --file 元数据.xlsx --dry-run
& "C:\Program Files\Python314\python.exe" -m importer.import_metadata --file 元数据.xlsx
# 导出向量库文档（A05 / D05 → 向量库）：{uid,type,text,payload} JSONL
& "C:\Program Files\Python314\python.exe" -m importer.export_vector_docs --out data/vector_docs.jsonl
# 灌 Milvus（需 pymilvus + 编码器可用）
& "C:\Program Files\Python314\python.exe" -m importer.ingest_vectors --dry-run
& "C:\Program Files\Python314\python.exe" -m importer.ingest_vectors --recreate

# 巡检 worker
& "C:\Program Files\Python314\python.exe" -m worker.inspection_worker

# 日终种子日期对齐（默认 dry-run；--apply 会把 dwd_shop 所有日期列平移到 T-1）
& "C:\Program Files\Python314\python.exe" -m worker.align_seed_dates
& "C:\Program Files\Python314\python.exe" -m worker.align_seed_dates --apply

# Langfuse 连通性自检（需先配 LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY）
& "C:\Program Files\Python314\python.exe" -m worker.check_langfuse

# 健康检查
curl.exe -s http://127.0.0.1:8100/health
curl.exe -s http://127.0.0.1:8100/metrics

# 容器化（本机未安装 Docker，未实跑验证）
docker build -t datapro-api:0.1.0 .                      # cwd: datapro
docker build -t datapro-web:0.1.0 .                      # cwd: datapro/frontend
docker compose up -d                                     # cwd: datapro（mysql/redis/api/worker/frontend）

# K8s（需集群与已推送镜像）
kubectl apply -f ops/k8s/secret.example.yaml
kubectl apply -f ops/k8s/datapro.yaml
kubectl -n datapro rollout status deploy/datapro-api
```

数据库初始化（一次性）：

- 数仓 `dwd_shop` 与只读账号 `ro` 已就绪。
- 业务库需存在 `datapro` 库与 `datapro/pw` 账号；后端启动时 `init_db()` 自动建表（失败降级为仅日志）。
- 上传库 `dp_uploads` 首次上传时自动建库/建表。

## 6. 已知约束与坑（含本机特殊处理）

- **系统 Temp 无写权限**：pytest 用 `--basetemp=.pytest_tmp`；前端构建必须把 `TEMP/TMP` 指到 `datapro\.tmp`，否则 esbuild 失败。
- **Git/gh 不在 PATH**：用全路径调用 `git.exe`；`gh` 未安装。GitHub 已配置 SSH（`~/.ssh/id_ed25519`，账号 `gaojinx`）。
- **后端未开 `--reload`**：改后端代码后必须重启进程才生效。重启前先停旧进程，避免 8100 端口冲突。
- **浏览器自动化**：旧 TRAE Chrome 扩展不可用（os error 10061）；现用 `playwright-browser-use` 技能（本地 Playwright CLI，`C:\Users\gaojinxin\.workbuddy\skills\playwright-browser-use`，Node 22 + playwright-core 已装）实测可用——非 headless、可见窗口、人工可随时接管。启动：`NODE_PATH=<skill>/node_modules node <skill>/pw-browser.js daemon`（后台），再 `open/goto/snap/click/fill/download/run-code`。外部平台「先知」（`xianzhi.umeng100.com`）导出实测走通：登录后落地 websiteManagement，需再 `goto /fe/infoAnalysis`，页面「导出数据」按钮直接下载 xlsx（Playwright `download` 的 `--path` 若目标不存在会被当成文件而非目录，注意指定已存在目录）。凭据不要经对话传递或落盘。
- **上传文件问答的两个坑（实测）**：① 上传文件表只绑定**当前会话**，切「新对话」后文件计数归 0、召回不到上传表，会错误命中数仓业务表给出「看似正确但答非所问」的结果——提问必须紧接上传、同会话；② 上传数据若日期在默认窗口之外，不带明确日期的提问会被时间过滤成 0 行，需**显式写日期**。两者本质都是 P0-4 时间窗锚点 + 上传文件跨会话持久化问题，尚未修。
- **前端 dev 访问地址**：`vite.config.js` 已设 `host: true`，`http://127.0.0.1:5175` 与 `http://localhost:5175` 均可访问；若只监听 `::1`，用 `127.0.0.1` 打开会表现为“发送报错”（接口请求失败）。
- **PowerShell 5 限制**：不支持 `&&`、`||`、三元运算符；`Invoke-WebRequest` 在非交互模式可能因 IE 引擎失败，联调优先用 `curl.exe` 或 Python `urllib`。含中文 JSON 建议写文件后 `curl --data-binary @file`。
- **种子数据日期会漂移**：`dwd_order_detail_di` 与 `dwd_user_di` 的日期列已滚动到 T-1（当前 2026-11-13 ~ 2026-11-22）。隔天会再次偏离，可用 `python -m worker.align_seed_dates`（默认 dry-run，`--apply` 执行）把全部日期列平移到 T-1；平移后固定日期评测用例需重新取基准值。订单表的地域/商品列（`region/province/city/goods_id/goods_name/qty`）由测试数据脚本一次性回填；`dws_category_daily`/`dws_channel_daily`/`dws_goods_daily`/`dwd_campaign_daily` 由订单表派生，日期漂移后需按同样窗口重建，否则各汇总表与订单表对不上。
- **`latest_partition()` 是算出来的 T-1，不等于数仓真实最新分区**：默认窗口与「0 单」自检提示都用 `today-1`（如 2026-11-24），而种子数据实际只到 2026-11-22，二者会不一致——问「最近 7 天」可能落在无数据的两天里，自检提示的「数仓最新分区」也会偏晚。真实分区以「数据到几号」类问题（F50 `data_boundary` 直读 `MAX(dt)`）的返回为准；数据漂移后先跑 `worker.align_seed_dates --apply` 对齐，再重取评测基准。
- **LLM Key 与降级**：注册表已配 DeepSeek 真实 Key；无 Key（或引用解析失败）时自动降级为口径模板模式（仅作兜底，不再作为评测主基线）。自 0017 起 Key 不落库不落 JSON：条目只存 `api_key_ref` 引用；`data/secrets/llm_apis.env` 是迁移时的过渡密钥文件（0600、gitignore），生产应改用 `env:`/`kms:` 引用后删除。
- **DeepSeek 模型名**：官方当前可用 `deepseek-flash`（V4.1-Flash）与 `deepseek-v4-pro`，`base_url=https://api.deepseek.com`；旧名 `deepseek-chat` 不在文档推荐列表（仍可调通，不建议再用）。官方 JSON Output 要求 prompt 含 `json` 字样并给样例，且偶发返回空 content——`_parse_json` 返回 None 时按失败重试/升级处理。
- **模型会写 PostgreSQL 方言**：曾用 `date_trunc` / `INTERVAL '1' DAY` 导致 MySQL 1370 → E010。`sql_gen.txt` v3 已写明 MySQL 8.0 方言、`{date_start}`/`{date_end}` 占位符（v1 曾用 `{date}`/`{start_date}`/`{end_date}`，现为兼容别名），并注入知识库术语参考（口径 logic 优先）；校验器拦截 `date_trunc`、`date_part`、`to_char` 等非 MySQL 函数并回炉修正；执行期 DB 异常统一映射 E006，日志记录原始 SQL 与错误。
- **模型会改写筛选值**：元数据 `meta_column.enum_meaning` 现注入生成 prompt（`字段取值…region ∈ {华东,…,东北}`），`sql_gen` v6 要求筛选值原样使用、不得加“区域/名称”后缀；曾出现把 `region='东北'` 写成“东北区域”导致 0 行。多轮追问的指标也加了规则兜底：合并问题里“本次补充”之前的口径词优先，避免追问时把“支付订单数”改写成“销售额”。
- **评测当前基线**：50 条（C001–C050），全链路真实模型 **50/50 通过**；`executable_rate=1.0`、`table_hit_rate=1.0`、`time_window_pass_rate=1.0`、`result_match_rate=1.0`、`false_answer_rate=0`、`table_overflow_rate=0`；报告落 `eval/reports/eval_v1_seed_report.json`。测试数仓补齐 `region/province/city/goods_name/qty` 后，原失败用例 C007（区域/城市）与 C008（商品自关联）已转通过；C026–C050 覆盖销售额/退款/客单价/销量、区域城市拆分、品牌、元数据问答、拒答、澄清、越界与相对时间。**注意**：新增 golden 用例时，取数类必须带 `expect` 断言，否则只能证明「没报错」。
- **golden 会随种子数据漂移**：`eval_v1_seed` 里 C021–C025 用固定日期，不要改成"昨天/最近7天"；`harvest` 产出的相对时间用例一律 `volatile`（无断言），要变成回归用例必须先由人把问题改成固定日期。数据日期滚动后需重跑 harvest。首批 golden 3 条（G001–G003）均为固定日期窗口，`eval_golden` 通过 3/3。重跑 harvest 已保留人工复核状态，但**相对时间的窗口与数值会随当天日期变化**，复核时看 `volatile` 标记而不是沿用旧印象。
- **`dim_channel` 在 `dwd_shop`**：早期元数据误写为 `dim` 库，导致 C005 走 E006；已修正 schema 并补充关联键说明（订单表 `channel` 存渠道名称，关联用 `channel_name`）。
- **本机没有 Docker / K8s**：`datapro/Dockerfile`、`frontend/Dockerfile + nginx.conf`、`ops/k8s/datapro.yaml` 已编写并通过 YAML 语法校验，但**未实际构建镜像、未上集群**，首次部署需在有 Docker 的机器上验证；YAML 中镜像名 `datapro-api:0.1.0`/`datapro-web:0.1.0` 需替换为实际 registry 地址。
- **K8s 数据卷要求 ReadWriteMany**：API 副本与 worker 共享上传/导出文件，`datapro-data` PVC 需要 RWX 存储类；只有 RWO 时把 API 副本改为 1，或改对象存储。worker 必须保持单副本（巡检/会话回收为单实例语义）。
- **K8s 迁移由 initContainer 执行**：`INIT_DB_ON_STARTUP=false` 时应用启动不再自动 upgrade，改由 `migrate` initContainer 跑一次 `init_db()`；本地直接跑仍默认 true。
- **数据源库必须预建**：升级到 `0012` 前需先创建 `datapro_source` 并授权（compose 由 `ops/mysql/init/01_source_db.sql` 在空数据卷首启时自动完成；已有数据卷或 K8s 需手工执行同款 SQL）。应用账号默认无全局 CREATE 权限，所以 `SOURCE_DB_AUTO_CREATE=false`；缺库时启动会报错并打印可执行 SQL，不会静默降级。`SOURCE_DATABASE_URL` 与业务库同名时 `0012` 跳过拆分（单库兼容模式），此时 5 张 `ingest_*` 表仍留在业务库。
- **0013 结构加固（已实跑）**：`query_record` 补 `outcome/error_code/spec_json/sql_final`（含历史回填 result55/escalate3/unknown84）；`audit_log` 补 `created_at` 与 `(user_id, created_at)` 索引；新增 `metric_caliber_history`（导入/修改前留旧值）与 `id_sequence`（导出号原子分配，首个新号 EP-0002 续接 EP-0001，8 线程并发无重号）；新增 1 个外键（`db_access.username → user_account.username ON DELETE CASCADE`）与 6 个 CHECK；删除空表 `session`（会话真值在 Redis + sessions.json）与 2 个前缀重复索引。`/admin/records` 支持按 `outcome`/`error_code` 筛选，并修正了分页 total 被截断成 `page*page_size` 的旧缺陷。
- **第一组缺口已落地（2026-11-26）**：数据新鲜度（U-B01/U-F05，真实分区 11-22 vs 日历 11-25 已实测纠正）、新手提问公式引导（U-F01）、追问 chips（U-F07）、拒答三动作（U-F09）、常用问法收藏（U-F02 本地版）、应用内帮助（U-F12/S-F04）、管理员运维处置（U-F10，口径裁决 + 会话回收）、记忆引用回显（U-B07）、白标静态配置（S-F02）。**Seller/SaaS 类（多租户、计量计费、对外 API、License）仍未做**，按缺口文档需先拍板定位与交付形态。前端新增引导遵循「不用弹窗、不用常驻小字」，引导卡可关闭且只出现一次。
- **配置域已全部入库（0014/0016）**：`llm_api` 与 `role_permission` 是独立表（按字段可查、可审计）；其余配置域（技能/知识库/巡检规则与结果/定时任务/口径裁决/学习数据/记忆/文件索引/会话登记）经 `SharedJson` 落 `config_doc` 文档表（`doc_key`=相对 `data/` 的路径，`version`/`updated_by`/`updated_at` 可追溯）。`data/*.json` 仅在 `CONFIG_STORE=file` 或库故障时使用，**库可用时不再双写**，手工改文件不生效（排障看 `GET /admin/config-docs`，带 `?key=` 可看完整 payload）。库故障熔断 30 秒，恢复后自动切回。测试通过 `tests/conftest.py` 强制 `CONFIG_STORE=file`，且服务单测用 `PermissionMatrix(tmp_path/...)`/`LLMRegistry(use_db=False)`，不要用全局单例写真实配置（曾发生 `test_api_authz` 把真实 `role_permissions.json` 的 user 写成空数组）。
- **前端构建包体**：Element Plus 已改按需引入（F63，`unplugin-vue-components` + `ElementPlusResolver`），主包 **499 kB / gzip 169 kB**（全量引入时为 1236 kB / gzip 400 kB）；程序化 `ElMessage` 与 `ConfigProvider` 的样式在 `main.js` 手动引入；单测环境 resolver 关闭样式注入（node 无法加载 theme-chalk css，且测试里 el-* 均已 stub）。Univer 编辑器为独立异步 chunk（约 6.8 MB / gzip 1.7 MB），只在打开 CSV/TSV 编辑时加载，属预期。
- **在线编辑限制**：CSV/TSV/XLSX；单表 ≤ 5000 行、总计 ≤ 10000 行；XLSX 用 SheetJS 转换，样式/公式/图表会丢失；Univer 官方 xlsx 导入导出属 Pro，不使用；编辑锁为进程内实现，仅适合单实例部署（多实例需 Redis，见 B11）。
- **SheetJS 版本**：npm 社区版 0.18.5 存在已知安全告警，仅用于处理当前会话的用户文件；后续可评估升级到官方 CDN 版本或服务端转换。
- **登录为演示账号**：`admin/admin123`、`analyst/user123` 仅首次启动写入，生产必须改密并替换 `AUTH_SECRET`；Token 黑名单存 Redis（不可用时降级进程内存，重启后丢失，仅影响已撤销令牌的重复拦截）；令牌版本持久化在 `user_account.token_version`，不随 Redis/重启丢失；权限矩阵角色默认存 `data/role_permissions.json`（已 gitignore），用户覆盖存 `user_account.permissions`。
- **管理员/用户权限隔离**：Token 不再携带 role/perm，每次请求回查 DB 实时角色/权限/状态；管理类权限（`users.manage`/`llm.manage`/`eval.run`/`sessions.sweep`）仅 admin 角色可持有，用户级覆盖与角色默认矩阵都会过滤掉；预设回复技能的 `answer` 仅管理员可配；`/admin` 所有写操作经中间件统一写审计（值脱敏）；导出申请强制过 SQL 校验并校验 session 归属；会话认领原子化、文件锁 owner 取认证主体、反馈只可评价自己记录。
- **上传表生命周期**：会话 TTL 默认 24h，每小时 janitor 回收账号与临时表；删除文件同步 DROP/REVOKE。回收 `sweep()` 全程持跨进程文件锁（`data/session_sweep.lock`），DROP 前用 `session_registry.recheck_active()` 二次校验，避免 worker 与 admin 手动并发、以及误删刚 touch 过的活跃会话。
- **上传内容密钥扫描（告警不拦截）**：`app/services/secret_scan.py` 在 `file_store.add`（覆盖上传/编辑保存/版本恢复）扫描原始字节，识别私钥块/AWS/Google/GitHub/`sk-` 风格/Slack/JWT/凭据赋值等 8 类形态；命中写 `entry.secret_hits`（rule/count/脱敏片段/行号），打告警日志 + `datapro_upload_secret_flag_total` 指标，前端上传弹 warning、文件列表显示「密」标记（悬停看规则）。扫描是旁路能力：内部异常按无命中处理，绝不阻断上传。**不拦截也不自动删除**——处置权（删文件/轮换密钥）交给上传者与管理员；已知遗漏：xlsx 是 zip 容器，原始字节扫不到单元格文本。
- **对话内附件侧栏预览（F52）**：对话消息流里点击文件附件不再跳主区整页预览，而是展开右侧边栏新增的「预览」页签做紧凑预览（表格前 50 行 + 工作表切换、图片/PDF 内联），主区停留在消息流不打断对话；页签上有「主区打开」跳转完整预览（可编辑/看版本）。实现：`openPanelPreview()` + `panelTab='preview'`，复用 `fetchPreview`；页签仅在存在预览对象时显示，关闭后回落「文件」页签。
- **消息历史持久化与恢复（F53，0018/0019）**：`message` 表增 `attachments`（TEXT，JSON 数组，只存 `{file_id,name,ext,size,origin}` 指针快照）与 `payload`（MEDIUMTEXT，终态 SSE 事件 JSON：result/clarify/reject/escalate/error）。前端发消息带结构化附件引用；`POST /chat` 的 `_sanitize_attachments` 白名单化并校验归属（文件必须真实存在于本会话、本人或历史公开可访问，客户端伪造的 file_id 被丢弃，上限 10 个）。助手消息落库时随存终态载荷（含 `steps` 节点轨迹与 `elapsed_ms` 耗时，F54），跨设备恢复可还原**结果表格/KPI/依据/澄清卡片/拒答卡/步骤时间轴**。接口：`GET /api/v1/sessions/{id}/messages`（升序、默认 200 条、上限 500；管理员/会话认领者可读，owner 记录过期（Redis TTL 24h）后回退 `persist.session_has_user`）；`GET /api/v1/sessions/archive`（本人参与过的会话归档，标题取本人首条提问）。前端：`switchSession` 本地快照为空时按 payload 富还原消息流（本地富快照优先不被覆盖）；登录/挂载时 `mergeServerSessions` 把归档并入侧栏（插在本地会话之后，saveSessions 截前 30 个时优先保住本地）；历史引用的文件被删除时点击提示「该文件已被删除」。
- **多实例 JSON 状态**：所有 `data/*.json` 状态统一走 `SharedJson`（见 §2 共享 JSON 状态），不再持有跨进程失效的进程内快照；`sessions.json` 由 api 的 `touch` 与 worker 的 `expired/remove` 共享，worker 不再用启动时的旧快照判过期；巡检 `run_all` 也持跨进程锁避免重复跑。单实例仍可用；多实例部署时这些 JSON 文件须放在共享卷上（docker-compose 已挂载 `./data`）。
- **本机 Redis**：已通过 winget 安装为 Windows 服务（`Redis`，安装目录 `C:\Program Files\Redis`），默认 6379；WinGet 的 `redis-server.exe` shim 曾因缺 DLL 启动失败（0xC0000135），直接用安装目录下的可执行文件。
- **Langfuse 服务端**：本机没有 Docker/Postgres，服务端未实跑；自托管编排已放在 `ops/langfuse/docker-compose.yml`（v3 全套：web/worker/postgres/clickhouse/redis/minio，首次启动自动建 org/project/admin），复制 `.env.example` 改掉密钥后 `docker compose up -d`。SDK 已接好，默认 no-op；启用只需在 `.env` 填 `LANGFUSE_PUBLIC_KEY/SECRET_KEY` 和 `LANGFUSE_HOST`（Cloud 或自托管），下一次启动即生效；配好后用 `python -m worker.check_langfuse` 验证健康检查与一条 trace 上报。上报含用户问题、prompt 与 SQL，接入外部服务前先确认合规。SDK 5.x 以 gzip protobuf 上报 OTLP（`/api/public/otel/v1/traces`），score 走 `/api/public/scores`。
- **定时任务仅单实例**：APScheduler 作业与同步签名都在进程内存，多实例部署会重复执行，需换 Redis job store 或分布式锁；任务配置与运行记录为 JSON 文件，多实例也会互相覆盖。
- **巡检同样单实例/JSON**：规则与结果分别存 `data/inspection_rules.json`、`data/inspection_results.json`（最多 200 次），多实例会互相覆盖；worker 每日 06:00 执行，管理端也可手动触发。巡检耗时与规则数线性相关，规则多时建议加重试与超时保护。
- **扫描量拦截依赖 EXPLAIN**：`EXPLAIN` 的 rows 是估算值，JOIN 场景取各步之和偏保守；预估失败直接放行，避免误伤正常查询。阈值按数仓规模调 `MAX_SCAN_ROWS`。
- **导出记录库优先、JSON 兜底**：`export_request` 表（Alembic 0007，0008 加 `window_start/window_end`）存申请/审批/文件/导出结果，多实例可见；业务库不可用时降级 `data/export_requests.json` 并保持进程内 JSON 模式，避免两边数据分裂。CSV 落 `data/exports/`，下载写 `audit_log`。审批通过后用申请人的会话身份与**原查询时间窗**重跑 SQL（仍走只读账号与表白名单），并**按申请人当前数据表权限复核**（A02：申请后被收权则导出失败、不执行）；若申请未带时间窗（历史记录）才回落默认最近 7 天。导出上限 `EXPORT_MAX_ROWS`（默认 10 万），超限截断并在记录里标 `truncated`。
- **数据库变更走 Alembic**：改 `models.py` 后必须 `-m alembic revision --autogenerate -m "..."` 生成版本再 `upgrade head`，不要再手写 `ALTER TABLE` 或依赖 `create_all`。启动时 `init_db()` 会 upgrade；若库里有表但没有 `alembic_version`，视为 create_all 建的历史库并自动 `stamp head`（前提是结构已等于基线）。
- **MySQL 的 TEXT 列不能带默认值**：`CREATE TABLE ... description TEXT NOT NULL DEFAULT ''` 会报 1101（BLOB/TEXT/GEOMETRY/JSON can't have a default value）。迁移里 TEXT 只写 `nullable=False`，默认值交给 ORM 的 Python 端 `default=""`。
- **技能包、定时任务与巡检的权限**：三页对全员开放，分别由 `skills.manage`/`schedules.manage`/`inspections.manage` 控制，普通用户默认拥有；定时任务按 owner 隔离（普通用户只维护本人任务，越权统一 404），技能与巡检规则为全局配置（普通用户修改会影响所有人生效）；管理员可在权限矩阵收回对应权限点。
- **自动学习依赖真实模型**：无可用 Key 或 `LLM_MOCK` 降级时 `llm_client.available()` 为 False，自动沉淀/记忆直接跳过，不会写入 mock 内容；学习数据是 JSON（`data/learned_skills.json`、`data/memories.json`），多实例会互相覆盖；自动写入按用户隔离、单人限量、敏感信息拦截，仍建议定期抽查「技能规范 / 记忆」列表。
- **Python 3.14 兼容**：pymysql 需 `cryptography`（已在依赖中）。`alembic.ini` 必须保持纯 ASCII——Python 3.14 的 configparser 按系统 locale（GBK）读取，中文注释会直接 UnicodeDecodeError。
- **产品页是纯静态站点**：`https://37lhyy3u5n4xv69lcj5.traesite.cn`（Site `37lhyy3u5n4xv69lcj5`，所有人可见）只做产品介绍，体验区是内置示例问答，不请求任何后端，任何电脑可直接打开。后端保留 `CORS_ALLOW_ORIGINS` 白名单（默认含该域名与 `127.0.0.1:5175`/`localhost:5175`），供后续再接真实取数时使用；真接后端时要注意：公网 HTTPS 页只能访问 HTTPS 后端，本机 `127.0.0.1:8100` 仅运行后端的那台电脑可连通，他人访问需内网穿透或服务器部署。

## 7. 待办

- **P0**：元数据字典生产实填（D02 测试版 11 表/80 字段/14 口径已入库；生产 50 表待业务方按模板补齐，`--dry-run` 校验后 `--kind` 导入即可）；评测集已扩到 50 条（E03），后续用 harvest 持续积累真实问答、复核 draft 后 promote；评测基线见 §6。用户级数据表权限（A02）代码与入口已就绪，**默认继承全局白名单**，生产上线前需按岗位在「权限与角色」为用户收紧授权表。
- **P1**：向量召回后端化（A05：进程内索引 + `MilvusIndex` 适配器 + `ingest_vectors` 灌库脚本均已就绪，`VECTOR_BACKEND` 切换、连不上自动回退；50 条召回评测表命中 97.5%、口径 92%；**Rerank 精排模型未实现**，当前为关键词优先 + 向量补位；剩余为真实 Milvus 部署联调与 50+ 表规模评测——本机无 Docker，Milvus 服务端尚未部署。POC 结论：bge-small 仅权重 INT8 单条 1.72x/余弦 0.9987，BGE-M3 2.43x/0.9992，详见 `datapro/poc/openvino_embed/`）。
- **P2**：日终种子日期对齐脚本已提供（`worker.align_seed_dates`，默认 dry-run），是否自动执行待定——执行后固定日期评测用例需重取基准；Langfuse 自托管编排与自检脚本已提供（`ops/langfuse/` + `worker.check_langfuse`），待有 Docker 的机器实际启动并验收看板；巡检告警已支持飞书/钉钉/企业微信与加签，填真实机器人地址即可（`INSPECTION_WEBHOOK_*`）。**前端入口缺口**：口径裁决管理（D03，`/admin/calibers/rulings`）与会话回收（S07，`POST /admin/sessions/sweep`）目前只有后端接口，设置页尚未提供操作页面/按钮。**默认窗口日期来源**：`latest_partition()` 仍按 `today-1` 计算，与数仓真实最新分区可能差几天；可改为启动时/定时用 F50 的 `MIN/MAX(dt)` 探针读取真实分区并缓存，让默认窗口与自检提示跟实际数据对齐。
- **P3**：I06 容器化与 K8s 编排已就绪（Dockerfile / compose / `ops/k8s`），按「日活 > 500」触发放量，首次上集群需验证镜像构建、RWX 存储与 Ingress/TLS。
- **文件编辑路线（不使用 Univer Pro）**：P1 只读预览、P2 CSV/TSV 编辑、P3 XLSX 多表编辑、版本链/回滚/单元格级对比均已完成；图片/PDF 只读预览已在 P1 覆盖。后续可选：把 diff 结果导出为报告。

## 8. 禁止事项

- 禁止提交密钥、`.env`、`data/`（上传文件与密钥文件）、`node_modules`、构建产物。
- 禁止绕过 `docs/DataPro-功能清单.md` 直接开发新功能；完成后同步状态与变更登记。
- 禁止用弹窗承载功能页；禁止用大段常驻小字解释功能。
- 禁止开放写操作、导出敏感字段（手机号/身份证/银行卡等）。
- 禁止在召回 0 命中或无口径时让模型自由生成 SQL。
- 禁止把会话数据库账号权限扩大到整库；禁止恢复共享账号对 `dp_uploads` 的整库授权。
- 禁止 force push / 改写远端历史（除非用户明确要求）。
- 禁止在本机使用 `python` 短命令（WindowsApps 别名可能指向无依赖环境），统一用全路径解释器。

## 9. 维护约定

每次改完代码，**主动**按以下顺序收尾（无需用户提醒）：

1. 更新 `docs/DataPro-功能清单.md`（功能状态 + 变更登记行）。
2. 更新本文件：涉及技术栈/目录/命令/约束/待办的变化必须同步。
3. 运行验证：后端 `pytest -q`；前端改动执行 `npm test` + `npm run build`（构建带 TEMP 覆盖）。
4. Git 提交与推送仅在用户明确要求时执行；文档同步不等于提交。
