# DataPro 缺口分析 — 后端（使用者视角 + 卖家视角）

| 文档信息 | 内容 |
|---|---|
| 拆分来源 | 《DataPro-缺口分析-使用者与卖家视角》（产品经理 许清楚）+《DataPro-缺口分析-前后端技术评审》（架构师 高见远）+《DataPro-JSON字段取数评审》（架构师，数据源层最新结论） |
| 性质 | 将原「四象限合一」的产出**按端拆分**，本文件只讲后端；前端见《DataPro-缺口分析-前端.md》 |
| 覆盖范围 | 使用者·后端 8 条（U-B01~U-B08）+ 卖家·后端 6 条（S-B01~S-B06），共 **14 条** |
| 计数口径 | 可行性按逐条评审表精确计数：✅5 / ⚠️6 / ❌3（原评审总览粗分略有出入，以逐条表为准） |
| 核对依据 | `AGENTS.md`（最新，`datapro_source` + 5 张 `ingest_*` 已落地）、功能清单、架构评审裁决一~五、源码只读核对 |
| 状态 | 供排期与拍板参考 |

> **一句话结论**：后端只有 **1 条 correctness 项必须进首发**——U-B01 结果下发 `data_as_of`（配 U-F05）；只有 **1 条架构级根因**——S-B01 多租户，且首发用 L0 实例级物理隔离即可把成本从 L 降到 S~M；其余多项要么降 P2/二期，要么根本不是研发任务（U-B06 只是补数据）。铁律：任何新增能力都不得绕过 `validator.py` 表白名单 + 只读账号双防线。

---

## 1. 后端缺口总览

| 视角 | 条数 | 可行性 | 必须架构级改造 | 说明 |
|---|---|---|---|---|
| 使用者·后端 | 8 | ✅4 / ⚠️2 / ❌2 | 3（U-B02 多源、U-B04 异步、U-B08 行级） | 新鲜度/别名/权限申请/记忆可见可复用；多源/异步/行级触及链路约束 |
| 卖家·后端 | 6 | ✅1 / ⚠️4 / ❌1 | 2（S-B01 L1、S-B03 对外 API） | S-B01 是唯一致命根因；容器实跑与监控是部署侧补验证，不是新开发 |

**优先级分布**：P0 = U-B01、S-B01（目标保留但换 L0 方案）；P1 = U-B05、S-B02、S-B04（compose 实跑）、S-B06（Grafana/Loki 部署）；P2/二期 = U-B02、U-B03（限定范围）、U-B04、U-B07、U-B08、S-B03、S-B05，U-B06 移出研发计划作日常数据维护。

---

## 2. 使用者视角 · 后端（可感知的服务能力）

| ID | 缺口 | 性质 | 依据 | 可行性 / 工作量 | 改造范围（模块/文件） | 建议级别 | 风险与约束 |
|---|---|---|---|---|---|---|---|
| U-B01 | 结果未主动下发数据新鲜度；默认窗口锚点 `today-1` ≠ 真实分区 | 增强（correctness，复用 F50） | `功能清单 F50`；`AGENTS.md §6`；架构裁决一 | ✅ / S~M | 新增 `app/services/partition_clock.py`（TTL 缓存）；`data_boundary.py:67-99` 抽 `latest_dt()`；`statemachine.py:402-412` result 事件加 `data_as_of`；`config.py` 三开关；`time_window.py:35-59` 相对窗口用 anchor；`routes_chat.py:133-166` 透传无需改 | **P0 保留，唯一必进首发** | 多表分区不一致时标基准表名；**显式「昨天/指定日期」恒按日历**；`EVAL_FIXED_ANCHOR` 隔离评测并重跑基线；`PARTITION_READ_ENABLED=false` 可完全回退 |
| U-B02 | 不支持多数据源（F27 只是会话级临时表） | 全新 | `AGENTS.md §2/§4.1 F27`；章程 §3.2 | ❌ / L | 触及 `recall.py`/`generator.py`/`validator.py`/`executor.py`/`db_access.py:24`/`metadata_store.py`；每源需只读账号 + 表白名单 | **P1 → P2/二期** | **最新纠正**：基础设施（`datapro_source` + `ingest_*` 5 表 + 独立 engine）已落地，但那是「登记/血缘」不是「多源取数」（`executor.py:44-51` 仍单 engine）；异构源放大 E006，首发若必须只做同构 MySQL 只读副本 |
| U-B03 | 查询结果无缓存，重复问题全链路重跑 | 全新 | `AGENTS.md §4.1` 无缓存层 | ⚠️ / M | 新增 `query_cache.py`（Redis + 内存降级），挂 `routes_chat.py:50-120` 前置；**首发只缓存 INTENT 分类与 meta_qa，不缓存取数结果** | P2 保留但严格限定 | **越权串味首要风险**：键必须含 `tenant_id + user_id + token_version/role + A02 表白名单快照 + 归一化问题 + IR/spec + forced_caliber_id + data_as_of + history 摘要 + 裁决/记忆版本`；负结果短 TTL；缓存命中会掩盖新分区 |
| U-B04 | 无异步长查询，大查询被 A09 拦截后无路可走 | 全新/增强 | `AGENTS.md §4.1 A09`；`功能清单 B06` | ❌ / L | 新增 job 表/队列（复用 worker）、结果持久化、状态接口、前端任务页；提交时快照身份/口径/时间窗，执行前复核 A02 | P2，**先做 B13 + 阈值治理的 S 级替代** | 不破只读红线，但破坏「同步 SSE 单一入口」假设；异步期间权限可能被收回→越权快照；放开 A09 需阈值治理而非绕过；多租户下任务也带 tenant |
| U-B05 | 无权限自助申请，越权只能 E003 + 线下找管理员 | 全新（复用 B13 审批框架） | `功能清单 A02/B13`；`models.py:82-104`；`routes_exports.py:59-104` | ✅ / M | 抽象通用审批表；审批通过写 `db_access.set_tables()`（`:127-150`）并失效 60s 缓存；通知接 U-F11 | P1 保留 | 不改状态机（只在 VALIDATE 前 E003 路径）；**授权必须带有效期 + 审批留痕**，撤销与删用户清权一并设计；多租户下带 tenant |
| U-B06 | 电商口语别名覆盖有限（知识库 28 条多新媒体） | 补数据（非研发） | `功能清单 F40/A04`；`models.py:111` aliases | ✅ / S | `data/knowledge.json` 补电商词条；`metric_caliber.aliases` 补口语别名；复用 `import_metadata.py` upsert | **从迭代计划移出，作日常数据维护** | 一行配置/一条数据，不是研发任务；别名过多污染召回，新增后跑 `recall_eval` 确认表命中 ≥97.5%、口径 ≥92% 不回退 |
| U-B07 | 个性化记忆作用域不透明，回答不标注引用了哪条记忆 | 补入口（增强 A11/F30） | `routes_chat.py:94` 已注入 `memory_store.for_user()` | ✅ / S~M | `PipelineContext` 加 `memory_notes`；result 事件增字段；前端依据区复用 F30 折叠展示 | P2 保留（低成本） | 措辞只能是「本轮参考了」，**不能声称「因此得出」**（LLM 归因不可靠）；记忆按用户隔离，展示不得跨用户泄漏 |
| U-B08 | 商家场景无多店铺行级隔离 + 经营看板 | 全新（依赖行级） | `AGENTS.md §2` 表清单无 `shop_id`；架构裁决三 | ❌ / L + 数仓项目 | 数仓加 `shop_id` 并回填；应用侧用户→店铺映射；GENERATE/VALIDATE 强制行过滤 + 校验必含过滤；`db_access` 从 user→表 扩为 user→表+行范围 | P2 维持，**不可短期交付** | **与裁决三直接冲突，需用户重新拍板**；行过滤漏一处即静默跨店越权，风险高于表级 E003；建议单独立项，不进首发 |

---

## 3. 卖家视角 · 后端（能不能卖、能不能 Scale）

| ID | 缺口 | 性质 | 依据 | 可行性 / 工作量 | 改造范围 | 建议级别 | 风险与约束 |
|---|---|---|---|---|---|---|---|
| S-B01 | 无多租户与租户级隔离；`metadata`/知识/规则/审计/文件均无 `tenant_id` | 全新（架构级根因） | `AGENTS.md §2/§4.1 A02`；grep `app/` 无 tenant_id；`models.py:17` username 全局 unique | ⚠️ / **S~M（L0）** / L（L1）/ L+（L2） | 三层路线见 §4.1。L0：每客户独立 `datapro`/`dp_uploads`/`data/`/密钥 + 代码埋点预留；L1：贯穿 9 个面 | **P0 目标保留，首发换 L0；坚持 L1 则降 P1/二期** | `db_access.py` 按 username 索引→同名串权；`shared_json.py:130-177` 全文件读写→多租户必串味，须 `data/{tenant_id}/` 分目录；SSE owner 校验扩 `(tenant, owner)` |
| S-B02 | 无租户级配额/计费/审计；B14 只按用户/分钟固定窗口 | 全新 | `功能清单 B14/S03`；`AGENTS.md §4.1 B14` | ⚠️ / M（L0）/ L（L1） | `rate_limit.py` key 改实例配额；新 `usage_record` 表 + 定时汇总；`audit_log`（`models.py:70-79`）加 tenant；账单导出 | P1（随定位裁决） | 不改状态机；**计费数据必须来自 `query_record`/`audit_log` 真实计数，不能拿限流计数当账单**；内部工具定位下退化为部门用量统计 |
| S-B03 | 无对外 API Key / 开发者门户 / 速率档位 | 全新 | `AGENTS.md §2/§3` 内部接口 | ❌ / L | 新 API Key 体系、速率档位、门户/SDK；对外取数必须复用 `Pipeline`（`routes_chat.py:41-43`） | P2 二期 | **最高只读红线风险**：ISV 直连数仓即绕过双重防线；API Key 不能复用 B12 HMAC Token；Key 必须绑 tenant，否则横向越权 |
| S-B04 | 容器编排已写未实跑；无一键安装/升级与发布流水 | 补验证 + 补文档 | `AGENTS.md §5/§6/§7(P3)`；裁决五 | ✅ / S~M | **无代码开发**：镜像构建（替换占位镜像名）、compose 起全栈、验证 RWX PVC / Ingress / initContainer、CI Linux 实跑 | P1（compose 实跑）；K8s 实跑 P3（日活>500 触发） | 与裁决五「首发单节点」不冲突；K8s 上 2 副本会触发上次评审 P2-1 多实例改造；本机无 Docker，转 CI/有 Docker 机器 |
| S-B05 | 无 License / 版本管理 | 全新 | 功能清单无条目 | ⚠️ / M（私有化）/ SaaS 由套餐替代 | 签名 + 到期 + 功能项的 License 中间件 + 版本台账 | P2（内部工具则取消） | 校验放启动/中间件层，不得进取数链路增加 P90；密钥/证书纳入 `.env`/gitignore |
| S-B06 | 运营埋点与 SLA 监控未落地（`/metrics` 已有但进程内） | 补部署 + 补文档 | `功能清单 I03/I04/F48`；`AGENTS.md §6` | ⚠️ / M（部署）/ L（客户级报表） | **已有**：`/metrics` Prometheus 文本、F48 Langfuse 薄封装、F41 标记码；工作 = Grafana/Loki + alerts.yml 落地、看板读 `query_record`/`audit_log` | P1 拆分：Grafana/Loki 先行；客户级 SLA 随 S-B01 | `/metrics` 多实例按 pod 抓取 + 查询层聚合即可，**不改代码**；Langfuse 上报含问题/SQL 属出域面，随 P0-1 裁决，否则回退本地 trace |

---

## 4. 关键架构裁决（已拍板 / 待拍板）

### 4.1 S-B01 多租户：三层路线，首发选 L0

| 层级 | 做法 | 成本 | 适用形态 | 首发 |
|---|---|---|---|---|
| **L0 实例级物理隔离** | 一客户一套部署（独立业务库/上传库/data/密钥），代码仅做 `tenant_id` 埋点预留 | **S~M** | 私有化 / 少量大客户 / 内部工具 | ✅ 做 |
| L1 应用层 `tenant_id` | 共享库共享表，贯穿认证、会话/SSE、审计、元数据、知识 JSON、文件、权限矩阵、向量、状态机 9 个面 | L（数周） | 公有云 SaaS | ❌ 定位确定后单独立项 |
| L2 数仓行级 | 数仓加 `shop_id`/`tenant_id`，SQL 强制行过滤 | L+ 数仓项目 | 商家多店铺 | ❌ 不做，需重新拍板 |

L1 九面中最危险的两处：`shared_json.py` 全文件 read-modify-write → 必须 `data/{tenant_id}/` 分目录；`User.username` 全局 unique → 改复合键 `(tenant_id, username)`，否则同名串权。

### 4.2 行级隔离（U-B08 / S-B01-L2）：架构侧拍板 **不提前**

维持上次裁决三（表级 B 方案 + 书面边界）。理由：这是商家市场的可选能力而非 SaaS 地基；`dwd_shop` 无 `shop_id`，回填牵一发动全身；行过滤漏一处是静默越权（比表级 E003 更危险）。启动必要条件：用户明确进商家市场 + 安全重新签字 + 数仓排期。替代：L0 给每商家独立部署，业务上等价。

### 4.3 数据新鲜度（U-B01）：唯一 correctness 项，最小改动清单

| 层 | 文件:位置 | 改动 |
|---|---|---|
| 缓存 | 新增 `app/services/partition_clock.py` | TTL 10min 缓存 anchor、惰性刷新、失败回退 `today-1` 告警一次、worker 每小时兜底 |
| 探针 | `data_boundary.py:67-99` | 抽 `latest_dt()` 单表 `MAX(dt)`，与 F50 `probe()` 共用 |
| 时间语义 | `time_window.py:35-59` | 相对窗口用 anchor；**显式日期恒按日历** |
| 状态机 | `statemachine.py:402-412` | result 事件加 `data_as_of` + `data_as_of_basis` |
| 网关 | `routes_chat.py:133-166` | **无需改**（事件直接透传） |
| 配置 | `app/config.py` | 三开关默认=现状，一键回滚 |
| 评测 | `EVAL_FIXED_ANCHOR` | 重跑 `eval_v1_seed`(50/50)、`eval_golden`、`recall_eval` 归档新基线 |

业务方唯一需确认：数据截止日取「订单事实表 `MAX(dt)`」还是「本轮涉及表 `MIN(MAX(dt))`」；架构建议首发前者并在 UI 标基准表名。

### 4.4 JSON 字段取数（补充结论，来自 JSON 评审）

当前链路对 MySQL JSON 内层字段是「**能查但不能稳查**」：执行层可通；召回层靠 `meta_table/meta_column` 扁平列列表，`$.字段` 无法召回；校验层实测两不通——路径写错（`$.nmae`）**误放**返回 NULL，`JSON_TABLE` 展开列 `j.dim` **误拦**为不存在字段。推荐：高频字段加生成列（`GENERATED ALWAYS AS (JSON_EXTRACT(...)) STORED`，生成列是真列，自动获得召回/校验/prompt 可见），或展开成 `ods_*` 规范化表并登记元数据 + `ingest_*` 承接层。**与项目既定决策一致：一数据集一真实表，不做单表 JSON 堆叠。**

### 4.5 缓存键与只读红线（Engineer 必读）

- 任何缓存键缺 `tenant_id + user_id + 权限快照 + data_as_of + 口径/记忆版本` 任一项即越权串味；首发只缓存 INTENT 与 meta_qa。
- U-B02/U-B04/S-B03 不得绕过 `validator.py` 表白名单 + 只读账号双防线；对外 API 绝不给第三方直连数仓。
- 配置开关默认=现状；SSE 新增字段可选、老客户端忽略；触及召回/生成/时间窗必须重跑评测并归档。
- 旁路（审计/通知/计量）写失败只 `logger.warning`，不阻塞主链路（`AGENTS.md §4.2-8`）。

---

## 5. 后端实施分组

**第一组 · 低成本见效（1~2 周）**
U-B01（`data_as_of`，唯一必进首发）、U-B06 别名补数据（从计划移出作维护）、U-B07 记忆引用可见、S-B04 compose 实跑。
→ 不改状态机、不破只读红线；与前端第一组协同交付。

**第二组 · 商业化地基（M，依赖定位裁决）**
S-B01 L0（物理隔离 + 埋点，不是 L1）、S-B02 实例/部门计量、U-B05 权限自助申请、S-B06 Grafana/Loki 部署、U-B03 限定范围缓存。

**第三组 · 二期 / 需重拍板（L）**
S-B01 L1、S-B03 对外 API、U-B02 多数据源、U-B04 异步长查询、U-B08 行级隔离（L+ 数仓项目）、S-B05 License、S-B06 客户级 SLA 报表。

> **实施进度（2026-11-26 核对）**
> - 已完成：**U-B01**（`partition_clock` + `PARTITION_READ_ENABLED`/`PARTITION_ANCHOR_TABLE`/`EVAL_FIXED_ANCHOR` 三开关 + worker 每 10 分钟刷新 + result 事件 `data_as_of`/`data_as_of_basis`）、**U-B07**（result 事件 `memory_notes`）、**U-B05**（`access_request` 表 + `db_access.expires_at` + 审批接口 + 前端「设置 → 权限申请」页，明细见 `AGENTS.md §4.1`）。
> - 仍属数据维护：**U-B06**（别名补数据，非研发）。
> - 已完成（2026-11-26 第二批）：**U-B03 限定缓存**——`app/services/query_cache.py` 只缓存意图分类与口径问答，键含归一化问题 + 资料指纹 + 身份 + 可见表白名单，Redis 优先/内存降级，`LLM_MOCK` 下自动跳过；运维页可看命中率并分域清空。**取数结果按文档要求一律不缓存**。
> - 未开始：S-B01 L0 埋点、S-B02 计量、S-B06 Grafana/Loki 部署；第三组维持不动。
> - 文档口径差异：U-B01 最终取「订单事实表 `MAX(dt)`」为锚点并在 UI 标注基准表名（对应待拍板 #4 的架构建议），显式日期仍按日历解析。

---

## 6. 后端「已实现 / 不要重复造轮子」纠正清单

| 条目 | 纠正 | 证据 |
|---|---|---|
| U-F03（关联后端） | 澄清定义摘要后端已下发，是前端呈现问题 | `caliber_ambiguity.py:62-65` |
| U-F06（关联后端） | 依据区已有 `explanation`/`spec_display`/假设 | `statemachine.py:404-405` |
| U-F08（关联后端） | B13 申请/审批/状态/下载完整，缺的只是触达 | `routes_exports.py:59-64,92-102` |
| U-B07 | 记忆注入已实现，只需回显条目 | `routes_chat.py:94` |
| S-B06 | `/metrics` 已实现，缺的是 Grafana/Loki 部署与聚合 | `AGENTS.md §4.1 I03`；裁决四 |
| S-B04 | Dockerfile/compose/K8s YAML 已写且语法校验通过，只差实跑 | `AGENTS.md §5/§6`；裁决五 |
| S-F01（关联） | 站外产品页已存在，缺的是链路打通 | `AGENTS.md §6` 末条 |
| U-B02 | F27 是会话级临时表，不算持久数据源；但 `datapro_source` + `ingest_*` 已落地（登记层），多源取数仍未打通 | `AGENTS.md` 最新；`executor.py:44-51` |

---

## 7. 后端待拍板

| # | 问题 | 影响 |
|---|---|---|
| 1 | 定位：内部工具 还是 对外 SaaS？交付形态：私有化 / 公有云 / 两者？ | 决定 S-B01 是 L0 还是 L1，以及 S-B02/S-B03/S-B05 的存在意义 |
| 2 | LLM 出域合规书面裁决（含私有化预算归属） | 决定 S-F01 演示形态、Langfuse 上报能否启用；裁决前只跑内部/脱敏数据 |
| 3 | 是否进入商家市场、需要行级隔离？ | 推翻裁决三需安全签字 + 数仓 `shop_id` 排期，否则 U-B08 不做 |
| 4 | 数据截止日口径：订单事实表 `MAX(dt)` 还是涉及表 `MIN(MAX(dt))`？ | 决定 U-B01 实现与 UI 标注文案 |
| 5 | 生产 50 表元数据填报责任人与时限 | 与本次缺口无直接依赖，但决定取数链路能否脱离种子回退 |
