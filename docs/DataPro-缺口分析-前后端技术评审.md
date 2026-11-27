# DataPro 缺口分析 — 前后端技术评审

| 文档信息 | 内容 |
|---|---|
| 产出角色 | 架构师（高见远 / Gao） |
| 任务来源 | 主理人任务 #2（团队 software-datapro-gap） |
| 上游输入 | 《DataPro-缺口分析-使用者与卖家视角》（产品经理 许清楚，实为 **32 条**：U-F12 + U-B8 + S-F6 + S-B6；任务简报写「31 条」，按产品文档逐条计数更正）+ `AGENTS.md` 全文（唯一长期事实源） |
| 前序产出 | 《DataPro-架构评审与技术优化方案》（本人上次评审，裁决一~五） |
| 工作方式 | 只读核对源码与文档；结论均标注 `文件:行号` 或文档章节；未改代码、未改其他文件 |
| 核对源码 | `app/api/routes_chat.py`、`app/core/statemachine.py`、`app/core/time_window.py`、`app/services/{db_access,shared_json,data_boundary,caliber_ambiguity}.py`、`app/api/{routes_admin,routes_exports}.py`、`app/db/models.py`、`frontend/src/App.vue`、`app/config.py` |
| 状态 | 供主理人汇编、供产品待确认 5 项拍板参考 |

> **一句话结论**：32 条缺口里，**使用者侧 20 条中约 15 条是「补入口/补数据/低成本增强」，可复用现有能力在 1~2 周内见效**；**卖家侧 12 条里有 1 条真正的架构级根因——S-B01 多租户**，其余卖家缺口大多挂在它下面。技术侧的核心建议是：**首发不要做应用层 `tenant_id` 贯穿，更不要为卖家场景提前做行级隔离**，改用「实例级物理隔离（一套部署一客户）+ 全链路预留 `tenant_id` 埋点」把 S-B01 从 L 级降为 S~M 级，同时把 U-F05/U-B01 数据新鲜度作为唯一必须进首发的 correctness 修复。

---

## 1. 评审总览

### 1.1 四象限整体可行性与改造分布

| 象限 | 条数 | 可行性分布 | 整体判断 | 架构级改造 vs 低成本复用 |
|---|---|---|---|---|
| **使用者·前端**（U-F01~F12） | 12 | ✅ 8 / ⚠️ 4 / ❌ 0 | **全部可做**。多数是展示层与入口层，后端能力已就绪；无一条需要动 9 节点状态机 | 架构级 0；低成本复用 12（其中 U-F05 依赖 U-B01 一个字段，U-F10 后端 100% 就绪） |
| **使用者·后端**（U-B01~B08） | 8 | ✅ 4 / ⚠️ 2 / ❌ 2 | **分化明显**：新鲜度/别名/权限申请/记忆可见是复用或小规模新增；多数据源/缓存/异步长查询/行级隔离会触及链路约束 | 架构级 3（U-B02 多源、U-B04 异步、U-B08 行级）；低成本复用 5（U-B01/B05/B06/B07 + U-B03 限定范围后） |
| **卖家·前端**（S-F01~F06） | 6 | ✅ 2 / ⚠️ 3 / ❌ 1 | **多数不是前端技术问题，而是等后端/定位裁决**：S-F03/S-F05/S-F06 强依赖 S-B01/S-B02；S-F02 白标在私有化形态下是纯配置 | 架构级 1（S-F03 随 S-B01）；低成本复用 3（S-F02/S-F04/S-F01 站内版） |
| **卖家·后端**（S-B01~B06） | 6 | ✅ 2 / ⚠️ 3 / ❌ 1 | **S-B01 是唯一致命根因**，决定其余 5 条的存在意义；容器实跑与监控是部署侧补验证，不是新开发 | 架构级 2（S-B01 L1、S-B03 对外 API）；低成本复用 4（S-B01 L0、S-B02 实例级、S-B04、S-B06 部署部分） |

**归纳**：真正「必须架构级改造」的只有 4 条——**S-B01（应用层多租户）、U-B02（多数据源）、U-B04（异步长查询）、U-B08/S-B01-L2（行级隔离）**。其余 27 条都可以在不动状态机、不破只读红线的前提下，用「补入口 + 补数据 + 补配置 + 补验证 + 小范围增强」完成。

### 1.2 架构判断的三个支点

1. **S-B01 的成本完全由「隔离层级」决定，不由「做不做」决定**。实例级物理隔离（L0）≈ S~M；应用层 `tenant_id`（L1）≈ L（数周，贯穿 9 个面）；数仓行级（L2）≈ L+ 且需数仓项目。**首发选 L0**。
2. **只读红线与「先口径后取数」是不可交易的约束**。U-B02/U-B04/S-B03 若设计不当，都会引入「绕过 validator 直接执行」的旁路——这是评审中最需要设防的点，而不是功能本身。
3. **数据新鲜度（U-B01/U-F05）是唯一 correctness 项，必须首发**。复用上次评审裁决一的 `partition_clock` 方案，改动可收敛到 3 个文件 + 1 个新模块。

---

## 2. 逐条技术评审表

> 图例：可行性 ✅可行 / ⚠️有条件可行 / ❌不建议现在做；工作量 S ≤ 2 人日 / M ≈ 1 周 / L > 2 周；优先级修正列写「产品级 → 建议级」。

### 2.1 使用者·前端（12 条）

| ID | 可行性 | 改造范围（模块/文件/接口） | 工作量 | 前置依赖 | 优先级修正 | 风险与约束 |
|---|---|---|---|---|---|---|
| **U-F01** onboarding 引导 | ✅ | `frontend/src/App.vue:186-193`（`HERO_CARDS`）、`:186`（`showHero` 首次进入态）、`frontend/src/style.css`；复用 `docs/DataPro-灰度方案与用户手册.md` §2 黄金公式文案 | S | 无 | **P0 → P0（保留）** | **必须遵守 `AGENTS.md §4.2-6`「功能不使用弹窗、不以常驻小字解释」**——用主区逐步引导卡/首次进入的示例卡重排实现，不得做 modal 或长文案铺底；引导需可跳过且不再重复出现（localStorage） |
| **U-F02** 常用问法收藏 | ⚠️ | 本地版：`App.vue` 输入区 + `localStorage`（按 user key）；跨端版：新增 `question_template` 表（Alembic）+ `routes_*` CRUD + `frontend/src/api/` 新模块。**注意 F38 是管理员预设回复（`routes_skills.py`），不是个人收藏，不可复用** | S（本地）/ M（跨端） | 跨端版需身份体系（已有） | **P2 → P2（保留，首发只做本地版）** | 本地版换浏览器丢失需明说；收藏内容进入 prompt 前需走与原文同样的越界/权限校验，不得作为「可信输入」绕过意图识别 |
| **U-F03** 澄清口径说明 | ✅ | `App.vue` 澄清态渲染（复用 `statemachine.py:232-233` 的 `clarify` 事件 `options`）；**纠正：`caliber_ambiguity.py:62-65` 的 `clarify_text()` 已把「A. 支付订单数（定义摘要，M003）」下发，后端不缺字段** | S | 无 | **P2 → P2（保留，纯前端展示优化）** | 属「已实现却被列为缺口」的前端呈现问题，不新造后端接口；仅做折叠/对比排版增强，避免把定义摘要塞回 prompt 造成重复 |
| **U-F04** 图表类型单一 | ⚠️ | `App.vue:913-937`（`chartOf` 纯 CSS 条形，限 2~20 行）；方案 A：引入 `echarts` + 新组件 `ChartView.vue`（趋势/占比/多指标）；方案 B：纯 SVG 自绘折线/环形，零新依赖。数据形状可直接取 result 事件的 `spec`/`spec_display`（`statemachine.py:409-410`）判断 grain | M（A）/ M（B） | 无后端改造；result 事件已带 spec | **P1 → P1（保留，但建议方案 B 起步）** | 前端当前无图表库（`AGENTS.md §2` 仅 Element Plus/Univer/SheetJS），引 ECharts 主包再增 ~1MB；`AGENTS.md §6` 已有「主包 1.15MB 告警」，须异步 chunk 加载；图表不得改变数值，仅可视化 |
| **U-F05** 结果卡标数据截止日 | ✅ | `App.vue:2187`（`ev-window` 时间窗徽标旁）新增「数据截至 {data_as_of}」chip；`App.vue:963`（`hasEvidence`）加入该字段触发依据区 | S | **强依赖 U-B01 下发 `data_as_of`** | **P0 → P0（保留，correctness）** | **绝不可拿 `time_window_end` 冒充数据截止日**——前者是查询窗口末端，后者是分区真实 `MAX(dt)`，二者在种子漂移时差 2 天（`AGENTS.md §6`）；老结果无此字段时前端不显示即可 |
| **U-F06** SQL 人话解释 | ⚠️ | 低成本方案：`App.vue` 依据区用已有 `spec_display` + `assumptions`（`statemachine.py:405/410`）模板化拼「按 X 维度统计 Y，限定 Z 时间」；高成本方案：GENERATE 节点加一次 LLM 解释（新增字段 `sql_explain`） | S（模板）/ M（LLM） | 模板方案无依赖 | **P2 → P2（保留，建议模板方案，去掉 LLM 调用）** | 已有 `explanation` 字段（`statemachine.py:404`）是生成解释，先确认其内容是否已满足「人话」需求再决定新增；额外 LLM 调用会叠加 P90 且引入解释与 SQL 不一致的新幻觉面 |
| **U-F07** 追问建议 chips | ⚠️ | 静态规则版：`App.vue` 结果卡下方，依据 `spec.dimensions` / `grain` 生成「按渠道拆一下」「上月对比」chips（复用 result 事件 `spec`）；上下文生成版：新增后端建议接口 + LLM 调用 | S（静态）/ M（LLM） | 静态版无依赖 | **P1 → P1（保留，先做静态版）** | 产品矩阵把 U-F07 与 U-B06 绑为一条，实际**二者完全独立**（前者前端 chips，后者后端别名数据），建议拆开排期；chips 触发的追问必须与多轮槽位继承（`AGENTS.md §4.1 多轮对话`）兼容 |
| **U-F08** 导出进度通知 | ⚠️ | `App.vue` 导出区轮询 `GET /api/v1/exports?status=`（`routes_exports.py:59-64` 已支持按状态过滤，B13 已有 `status` 字段）；「完成提醒」接 U-F11 通知中心；或复用 `app/services/webhook.py` 推飞书/钉钉/企微 | M | U-F11（通知中心）或现有 webhook 配置 | **P2 → P2（降级为「复用现有状态轮询 + 通知中心」，不新造导出能力）** | **纠正：B13 的申请/审批/重跑/下载已完整实现（`AGENTS.md §4.1 B13`），缺口只是「触达」，不是导出功能**；轮询频率需限流，避免多用户刷接口 |
| **U-F09** 拒答下一步行动 | ✅ | `App.vue` 拒答/0 命中渲染区；按钮复用 `HERO_CARDS` 示例卡与 E002 可查询范围说明（`statemachine.py:166-167`） | S | 无 | **P2 → P2（保留，建议提前到低成本迭代）** | 按钮点击后发送的文本同样走完整意图识别，不得直接当 SQL 执行；文案遵循「召回不到不臆造」（`AGENTS.md §4.2-2`） |
| **U-F10** 口径裁决/会话回收入口 | ✅ | `App.vue:1143-1150`（`ALL_SETTINGS_TABS` 新增 admin 分组「口径裁决」）；`frontend/src/api/admin.js` 新增 `listRulings/setRuling/deleteRuling/sweepSessions`。**后端已就绪：`routes_admin.py:36-57` 裁决 CRUD、`routes_admin.py:149-154` sweep** | S | 无 | **P0 → P0（保留，全清单性价比最高项）** | 零后端开发、纯补入口；写操作仍走 `require_admin`/`require_permission("sessions.sweep")`；前端隐藏不作为唯一防线（`AGENTS.md §4.1 前端可见性隔离`） |
| **U-F11** 通知中心 | ⚠️ | 阶段一（聚合视图）：`App.vue` 顶部铃铛 + 角标，聚合现有 `GET /api/v1/inspections`（A08 结果）、`GET /api/v1/exports`（B13 状态）、定时任务运行记录（F39），**零新后端表**；阶段二（完整通知）：新增 `notification` 表 + 各写入方挂接 + 已读状态 | M（阶段一 S~M）/ L（阶段二） | 阶段二依赖身份体系（已有） | **P1 → P1（保留，明确先做阶段一聚合视图）** | 阶段一不是真「推送」，需把边界说清；阶段二写入方多（巡检/导出/审批/限流/冻结），容易漏挂；通知内容含业务信息，多租户下必须带 tenant 过滤（见 §5） |
| **U-F12** 应用内帮助入口 | ✅ | `App.vue:1866-1981` 侧栏/头像菜单加入口 + 新 `HelpView.vue`；文档来源是静态 `docs/DataPro-灰度方案与用户手册.md`，建议摘成静态 FAQ 组件而非引入 markdown 渲染依赖 | S | 无（与 S-F04 合并实现一次） | **P1 → P1（与 S-F04 合并为一个交付项）** | 产品矩阵把 U-F12/S-F04 列为一处是对的，实施上不得做两遍；避免引入新 md 渲染依赖造成包体 inflation |

### 2.2 使用者·后端（8 条）

| ID | 可行性 | 改造范围（模块/文件/接口） | 工作量 | 前置依赖 | 优先级修正 | 风险与约束 |
|---|---|---|---|---|---|---|
| **U-B01** 结果下发 `data_as_of` | ✅ | 复用上次评审裁决一：新增 `app/services/partition_clock.py`（TTL 缓存 + 惰性刷新 + 降级）；`app/services/data_boundary.py:67-99` 抽出 `latest_dt()` 单表 `MAX(dt)` 探针；`app/core/statemachine.py:402-412` PRESENT result 事件增加 `data_as_of`（及可选 `data_as_of_basis`）；`app/config.py` 增 `partition_read_enabled`/`partition_cache_ttl_seconds`/`eval_fixed_anchor`；`app/core/time_window.py:35-59` 相对窗口用 anchor。`routes_chat.py:133-166` 事件为直接透传，**无需改动** | S~M | 无（业务方需确认「基准表」口径） | **P0 → P0（保留，correctness，唯一必进首发）** | ① 多表分区不一致时，单表 anchor 可能「显得比实际新」——首发须在字段旁标基准表名，严谨版取涉及表 `MIN(MAX(dt))`（M）；② 显式「昨天/指定日期」继续按日历解析，不随 anchor 漂移（裁决一边界）；③ 评测用 `EVAL_FIXED_ANCHOR` 隔离，改动后需重跑 `eval_v1_seed` + `eval_golden` 归档新基线；④ 老客户端无字段不报错 |
| **U-B02** 多数据源 | ❌ | 触及全链路：`recall.py`（表召回）、`generator.py`（方言 prompt）、`validator.py`（表白名单/字段存在性）、`executor.py`（按源身份/连接）、`db_access.py:24`（`DEFAULT_DB="dwd_shop"`）、`metadata_store.py`（多源元数据）、数仓只读账号体系（`AGENTS.md §4.1 会话级数据库身份`） | L | 交付形态裁决 + 源侧只读账号 | **P1 → P2 / 二期（降级）** | F27 文件导入是会话级临时表（`dp_uploads`），**不是持久数据源，不可当作多源已实现**；每个新增源都需一套只读账号 + 表白名单，直接冲击「双重只读防线」；模型当前仅适配 MySQL 8.0 方言（`AGENTS.md §6`），异构源会放大 E006；首发若必须满足，限定「同构 MySQL 只读副本」为第一阶段 |
| **U-B03** 查询结果缓存 | ⚠️ | 新增 `app/services/query_cache.py`（Redis 优先、内存降级），挂接 `routes_chat.py:50-120` 入口前置；建议首发只缓存 **INTENT 分类结果** 与 **meta_qa/口径问答**，不缓存取数结果 | M | 无 | **P2 → P2（保留但严格限定范围）** | **越权串味是首要风险**：缓存键必须含 `tenant_id + user_id + token_version/role + A02 表白名单快照 + 归一化问题 + IR/spec + forced_caliber_id + data_as_of + history 摘要 + 裁决/记忆版本号`，任一变化即 miss；A02 权限可变、D03 裁决可变、A11 记忆按用户注入（`routes_chat.py:94`）——不把这些进键会把他人答案缓存给本人；负结果（E003/E002）短 TTL 或不缓存；缓存命中会掩盖 probe 与新分区，必须绑 `data_as_of` |
| **U-B04** 异步长查询 | ❌ | 需新增 job 表/队列（复用 worker 体系）、结果持久化、任务状态接口、前端任务页；状态机需支持「提交时快照身份/口径/时间窗，执行时复核 A02 权限」；现有 A09 扫描拦截（`executor.estimate_scan`）与 B13 导出审批是替代路径 | L | 身份/权限快照机制设计 | **P2 → P2（降级二期；先做「B13 导出审批 + 调高 `MAX_SCAN_ROWS`」的 S 级替代）** | **不破坏只读红线，但会破坏「同步 SSE 单一入口」假设**——异步执行时用户权限可能已被收回，必须在执行前复核，否则产生越权快照；A09 存在就是为了拦大查询，放开它需要阈值治理而非绕过；对 P90 是正面（移出同步链路）但新增任务态一致性问题；与 S-B01 多租户叠加时任务也需带 tenant |
| **U-B05** 权限自助申请 | ✅ | 复用 B13 审批框架：`export_request` 表模式（`models.py:82-104`）→ 新增通用审批/申请表（Alembic）；`routes_exports.py:59-104` 审批流可抽象复用；审批通过写 `db_access.set_tables()`（`db_access.py:127-150`），自动失效 60s 缓存；通知接 U-F11 | M | B13 框架（已有）、U-F11（可选） | **P1 → P1（保留）** | 不改状态机（只在 VALIDATE 前 E003 路径，与 A02 现状一致）；**授权必须带有效期与审批留痕**，否则「临时申请」变永久授权；撤销路径与删除用户清权（`db_access._clear`）要一并设计；多租户下申请表需带 tenant |
| **U-B06** 电商口语别名 | ✅ | 纯补数据：`data/knowledge.json`（F40，28 条多为新媒体）补电商词条；`metric_caliber.aliases` 字段（`models.py:111`）为 M003~M015 补口语别名（「卖了多少」「退货那些」）。复用 `import_metadata.py` upsert，无需改检索代码 | S | 业务方给别名清单 | **P1 → 降为「日常数据维护」，从迭代计划移出（不占研发资源）** | 这是产品优先级矩阵修正的典型项——**一行配置/一条数据，不是研发任务**；别名过多会污染召回（一词多口径），新增后必须跑 `eval/recall_eval.py` 确认表命中 ≥97.5%、口径 ≥92% 不回退 |
| **U-B07** 记忆引用可见 | ✅ | `routes_chat.py:94` 已把 `memory_store.for_user()` 注入 `PipelineContext.memories`；在 `PipelineContext` 增 `memory_notes`，PRESENT result 事件增字段（`statemachine.py:402-412`），前端依据区展示；复用 F30 依据折叠 | S~M | 无 | **P2 → P2（保留，低成本）** | **只能标注「本轮注入了哪些记忆条目」，不能声称「该记忆影响了答案」**——LLM 归因不可靠，措辞必须是「参考了」而非「因此得出」；记忆按用户隔离（`AGENTS.md §4.1 A11`）已实现，展示时不得跨用户泄漏 |
| **U-B08** 多店铺行级隔离 + 经营看板 | ❌ | 数仓侧：`dwd_shop` 加 `shop_id` 维度并回填事实表/维表（**当前表清单无 `shop_id`，`AGENTS.md §2`**）；应用侧：用户→店铺映射、GENERATE/VALIDATE 强制注入行过滤（仿 `{date_start}` 占位符机制）+ 校验 SQL 必含过滤条件；`db_access.py` 从 user→表 扩为 user→表+行范围 | L + 数仓项目 | **与上次评审裁决三（维持表级 B、行级另立项目）直接冲突，需用户重新拍板** | **P2 → P2（维持，且明确即使升 P0 也不是短期可交付）** | 这是「面向商家售卖」的专属能力，**不是 SaaS 化地基**，与 S-B01 性质不同；行过滤一旦遗漏即跨店越权，风险等级高于表级 E003；与 U-B02 多源叠加复杂度乘算；建议单独立项，不并入首发或商业化地基迭代 |

### 2.3 卖家·前端（6 条）

| ID | 可行性 | 改造范围（模块/文件/接口） | 工作量 | 前置依赖 | 优先级修正 | 风险与约束 |
|---|---|---|---|---|---|---|
| **S-F01** 演示/试用链路 | ⚠️ | 站内版：`App.vue` 增加「试用/演示」入口 + 演示账号（`AGENTS.md §6` 已有 `admin/admin123`、`analyst/user123`）+ 脱敏演示数据集；站外版：站外静态产品页（`AGENTS.md §6` 末条，Site `37lhyy3u5n4xv69lcj5`）接 HTTPS 后端，需部署域名 + CORS（已有 `CORS_ALLOW_ORIGINS` 白名单） | S（站内）/ M（站外） | **产品待确认⑤：出域合规裁决** | **P0 → P0（保留，但收窄为站内演示 + 脱敏集；站外接后端转二期）** | **合规红线：出域裁决前不得用公有云 LLM 演示真实数据**（P0-1 未定，见上次评审裁决二）；`LLM_MOCK=true` 仅口径模板降级，演示效果有限，需准备全脱敏固定集；公网 HTTPS 页只能访问 HTTPS 后端，`127.0.0.1:8100` 他人不可达（`AGENTS.md §6`） |
| **S-F02** 品牌白标 | ✅ | `App.vue:1869`（`<div class="logo">DP</div>`）、`:2050`（`big-logo`）+ favicon/产品名；方案：新增 `frontend/public/branding.json`（私有化静态配置）或 `GET /branding` 接口（SaaS），配合 CSS 变量 `style.css` | S（静态配置）/ M（租户级） | 私有化静态版无依赖；租户级依赖 S-B01 L1 | **P1 → P1（保留，先做静态配置版）** | **实例级隔离形态下不需要 S-B01 即可交付**（每客户一份配置）；`AGENTS.md §4.2-6` 视觉基线（去渐变/单一强调色/字重≤600）需保留，白标只换 logo/名称/主题色变量，不得放任客户破坏排版约定 |
| **S-F03** 多租户切换器 | ❌ | UI 本身 S（`App.vue` 顶栏租户下拉），但**强依赖 S-B01 的租户身份与数据隔离**；无后端租户概念时切换器只是「换皮」，会造成越权假象 | S（UI）/ M（真隔离） | **S-B01** | **P1 → 冻结（依赖 S-B01 路线确定；L0 形态下该概念消失，直接取消）** | 若首发选实例级物理隔离（一客户一部署），**根本不需要切换器**——这是最容易被产品矩阵误导的一项；提前做只会做出一个无隔离保证的危险入口 |
| **S-F04** 帮助中心 | ✅ | 与 U-F12 同一实现：`HelpView.vue` + FAQ/教学视频位 + 侧栏入口（`App.vue:1866-1981`） | S | 无 | **P1 → P1（与 U-F12 合并计数，做一次）** | 产品矩阵已标为同一缺口，实施上避免重复排期；视频位需考虑站外资源与合规，首发建议静态图文 |
| **S-F05** 用量/套餐展示 | ⚠️ | 完整版：`App.vue` 新用量页 + `S-B02` 计量接口；最小版：`GET /admin/records` 或 `query_record` 计数按用户统计（`AGENTS.md §4.1 B20` 已有对话记录接口），展示「本月调用量」 | M（完整）/ S（最小） | **S-B02（完整版）**；最小版无依赖 | **P1 → P1（保留，但套餐部分冻结至定位裁决；先做最小个人用量页）** | 内部工具定位下「套餐」概念不存在，需按待确认①决定；B14 限流是按用户/分钟固定窗口（`routes_chat.py:56`、`rate_limit.py`），**不是套餐计量，不可拿来当用量数据**；多租户下用量必须带 tenant 过滤 |
| **S-F06** 管理员交付控制台 | ❌ | `App.vue` 新交付控制台视图（租户列表/健康度/用量/告警/上线进度），数据接口分别来自 S-B01/S-B02/S-B06；现有设置页是自用型（`App.vue:1143-1150`） | S（壳）/ L（数据面） | **S-B01 + S-B02 + S-B06** | **P2 → P2（冻结，依赖链最长）** | 依赖链长，单独先做只会得到空壳；实例级隔离形态下退化为「现有设置页 + Grafana 看板」，可取消；多租户下控制台本身是最高权限面，越权风险最大 |

### 2.4 卖家·后端（6 条）

| ID | 可行性 | 改造范围（模块/文件/接口） | 工作量 | 前置依赖 | 优先级修正 | 风险与约束 |
|---|---|---|---|---|---|---|
| **S-B01** 多租户与租户级隔离 | ⚠️ | **三层路线，见 §3.1**。L0 实例级：部署脚本/配置隔离（每客户独立 `datapro` 库 + `dp_uploads` + `data/` + 密钥），**代码只做 `tenant_id` 埋点预留**；L1 应用层：见 §3.1 的 9 个面；L2 数仓行级：同 U-B08 | S~M（L0）/ L（L1）/ L+（L2） | **产品待确认①②（定位 + 交付形态）** | **P0 → P0（保留目标，但首发换方案为 L0；若坚持 L1 则降 P1/二期）** | 代码现状确认：`app/` 全目录 **无 `tenant_id` 概念**（grep 结果为空）；`User.username` 全局 unique（`models.py:17`）→ 跨租户同名账号冲突；`db_access.py` 按 username 索引 → 同名串权；`shared_json.py:130-177` 是全文件 read-modify-write 且 `snapshot()` 返回全量 → **多租户共文件必然串味**，必须按租户分目录；`dp_uploads` 临时表名 `f_{file_id}` 需索引带 tenant；SSE 会话（`routes_chat.py:63-70` owner 校验）只需扩为 `(tenant, owner)` |
| **S-B02** 租户配额/计费/审计 | ⚠️ | L0 形态：`rate_limit.py` key 从 `chat:{username}`（`routes_chat.py:56`）改为按实例配额；新增 `usage_record` 表（Alembic）+ 定时任务汇总；`audit_log`（`models.py:70-79`）加 tenant 维度；账单导出 | M（L0）/ L（L1） | S-B01 路线 | **P1 → P1（保留，随定位裁决决定形态）** | 不改状态机；计费数据必须来自 `query_record`/`audit_log` 的真实计数，不能拿限流计数当账单；内部工具定位下退化为「部门用量统计」，仍可做 |
| **S-B03** 对外 API + 开发者门户 | ❌ | FastAPI 现有 REST/SSE 是内部接口（`AGENTS.md §2`）；需新增独立 API Key 体系、速率档位、开发者门户前端、SDK；对外取数必须复用 `Pipeline`（`routes_chat.py:41-43`）而非直连数仓 | L | S-B01 L1 + S-B02 计量 | **P2 → P2（二期，维持）** | **最高只读红线风险**：ISV 一旦能直连数仓，双重只读防线（sqlglot 白名单 + 只读账号）即失效；对外 API Key 不能复用用户 HMAC Token（`AGENTS.md §4.1 B12` 令牌语义不同）；多租户下 Key 必须绑 tenant，否则横向越权 |
| **S-B04** 交付自动化实跑 | ✅ | **无代码开发**：Dockerfile / compose / `ops/k8s/datapro.yaml` 已编写并通过语法校验（`AGENTS.md §5/§6`、上次评审裁决五）；工作 = 在有 Docker 机器构建镜像（替换占位镜像名）、compose 起全栈、验证 RWX PVC / Ingress / initContainer 迁移、CI Linux 实跑一次 | S~M | 有 Docker 的机器、registry | **P1 → P1（保留，但拆：compose 实跑=P1；K8s 实跑=P3 按日活>500 触发）** | 与裁决五「首发单节点」不冲突（compose 即单节点）；K8s 强行上 2 副本会触发上次评审 P2-1 多实例改造，不可混为一谈；本机无 Docker，验证必须转有 Docker 机器/CI |
| **S-B05** License / 版本管理 | ⚠️ | 私有化：License 文件（签名 + 到期 + 功能项）校验中间件 + 版本台账；SaaS：由 S-B02 套餐体系替代，无需独立 License | M（私有化）/ —（SaaS 用套餐） | 交付形态裁决（待确认②） | **P2 → P2（保留；内部工具定位则取消）** | 不改状态机；License 校验应放在启动/中间件层，不得放在取数链路中增加 P90；密钥/证书管理需纳入 `.env`/gitignore 约定（`AGENTS.md §8`） |
| **S-B06** 运营埋点 + SLA 监控 | ⚠️ | **已有**：`GET /metrics` Prometheus 文本（I03，`AGENTS.md §4.1`）、F48 Langfuse 薄封装、F41 标记码；工作 = 部署 Grafana/Loki + `ops/prometheus/alerts.yml`（已有）落地、业务看板读 `query_record`/`audit_log`、客户级 SLA 报表按 tenant 聚合 | M（部署为主）/ L（客户级报表） | S-B01（客户级报表）、P0-1 合规（Langfuse） | **P1 → P1（保留，拆：Grafana/Loki 部署先行；客户级 SLA 报表随 S-B01）** | `/metrics` 是进程内计数，多实例按 pod 抓取 + 查询层聚合即可，**不需改代码**（上次评审裁决四）；Langfuse 上报含问题/prompt/SQL，属数据出域面，必须随 P0-1 书面结论走，否则回退本地 trace + F41 |

---

## 3. 重大架构决策（对产品待确认 5 项的技术建议）

### 3.1 待确认①+②：定位与交付形态 → **架构建议：先按「可演进的内部工具 + 私有化交付」落地，SaaS 走渐进路线**

**能拍板的部分（架构侧定）**：无论最终定位如何，**首发都采用 L0 实例级物理隔离 + 全链路 `tenant_id` 埋点预留**。这是「现在不花 L 级成本、未来不被锁死」的唯一稳妥路线。

**多租户渐进路线（S-B01 的完整答案）**：

| 层级 | 做法 | 成本 | 适用形态 | 首发是否做 |
|---|---|---|---|---|
| **L0 实例级物理隔离** | 一客户一套部署：独立 `datapro` 业务库、`dp_uploads`、`data/` 目录、LLM 注册表、密钥；部署脚本参数化。**代码侧仅做埋点**：新增表/JSON 统一带 `tenant_id` 列默认 `"default"`，`PipelineContext` 加 `tenant_id` 默认 `"default"`，Redis key 预留 `{tenant}` 段 | **S~M** | 私有化 / 少量大客户 / 内部工具 | ✅ **首发做这一层** |
| **L1 应用层 `tenant_id`** | 共享库共享表，tenant 维度贯穿 9 个面（下表） | **L（数周）** | 公有云 SaaS、多小客户 | ❌ 首发不做；定位确定为 SaaS 后单独立项 |
| **L2 数仓行级隔离** | 数仓加 `shop_id`/`tenant_id`，SQL 强制行过滤 | **L+ 数仓项目** | 商家多店铺 / 同一张表不同人看不同行 | ❌ 不做；与裁决三冲突，需用户重新拍板（见 §3.2） |

**L1 必须贯穿的 9 个面（供未来立项的范围基线）**：

| # | 面 | 现状证据 | 改造要点 |
|---|---|---|---|
| 1 | 认证/身份 | `models.py:17` `username` 全局 unique；B12 Token 只带 `jti`/`epoch`/`perm`（`AGENTS.md §4.1`） | `User` 加 `tenant_id`，唯一键改 `(tenant_id, username)`；Token/黑名单/令牌版本按 tenant 作用域；`auth_state` 回查带 tenant |
| 2 | 会话/SSE | `routes_chat.py:63-70` owner 校验；`session_state.py` Redis `dp:sess:{sid}`（`AGENTS.md §2`） | 会话归属扩为 `(tenant, owner)`；Redis key 改 `dp:sess:{tenant}:{sid}`；限流 key `chat:{tenant}:{user}`（`routes_chat.py:56`） |
| 3 | 审计/记录 | `models.py:70-79` `audit_log` 仅 `user_id`；`query_record` 仅 `username` | 所有业务表加 `tenant_id` + 索引；所有读接口（`/admin/records`、`/api/v1/trace`）带 tenant 过滤 |
| 4 | 元数据/口径 | `metadata_store.py`（60s 缓存 + 种子回退，`AGENTS.md §4.1 D05`） | `meta_table/meta_column/metric_caliber` 加 tenant；`recall.SEED_*` 按租户分组，避免 A 租户回退到 B 租户种子 |
| 5 | 知识/规则/配置 JSON | `shared_json.py:130-177` 全文件 read-modify-write、`snapshot()` 返回全量；涉及 skills/knowledge/rulings/schedules/inspection/role_permissions/llm_registry/learned_skills/memories/feedback_pool | **必须按租户分目录 `data/{tenant_id}/*.json`**（最低成本、审计最清晰）；若坚持单文件则每条记录带 tenant 且所有 `snapshot()` 消费方强制过滤——漏一处即越权 |
| 6 | 文件/上传库 | `dp_uploads` 会话临时表 `f_{file_id}`（`AGENTS.md §2`）；`file_store.py` 索引带 `owner` | 文件索引加 tenant；上传库按租户分库或表名前缀；预览/下载 `?token=` 鉴权（B12）加 tenant 校验；删除/回收（`session_janitor.py`）按 tenant 作用域 |
| 7 | 权限矩阵 | `db_access.py:99-122` 按 username 查表白名单；`permissions.py` 11 权限点 | `db_access` 加 tenant 维度，否则跨租户同名账号串权；角色矩阵支持租户级覆盖；`require_permission` 带 tenant |
| 8 | 向量/召回 | `vector_recall.py`、`datapro_meta` 集合（`AGENTS.md §4.1 A05`） | 集合按 tenant 分（或 payload 带 tenant 且查询强制过滤）；`export_vector_docs`/`ingest_vectors` 按租户跑 |
| 9 | 状态机/API 层 | `PipelineContext`（`statemachine.py:60-94`）无 tenant；`routes_chat.py:87-94` 构造 ctx | `PipelineContext` 加 `tenant_id`，所有 service 入口透传；审计 `audit_service.record`、Langfuse trace、SSE 事件带 tenant（前端不显示，仅后端鉴权用） |

**需用户拍板**：定位（内部 vs SaaS）、交付形态（私有化 vs 公有云 vs 两者）、License 策略。**架构侧建议**：若两者都要，先 L0 跑通私有化 1~2 家，再评估 L1；不要并行做，否则 SaaS 未验证就把 L 级改造塞进关键路径。

### 3.2 待确认③：行级隔离 → **架构侧拍板：不为「卖家场景」提前启动**

**结论（拍板）**：维持上次评审裁决三（B 方案表级 + 书面边界），**U-B08 / S-B01-L2 不提前**。

**理由**：
1. **性质不同**：S-B01 是 SaaS 化地基（没有它只能卖一家），行级隔离是「商家多店铺」这一可选售卖场景的专属能力；把后者绑进前者会把 L+ 数仓项目拖进首发关键路径。
2. **数仓侧成本是实打实的**：`dwd_shop` 现有表清单（`AGENTS.md §2`：订单/用户/渠道 + dim/dws/dwd 各表）**没有 `shop_id` 维度**，需要事实表加列、历史回填、维表建设、汇总表重建（`AGENTS.md §6` 已记载汇总表由订单派生、日期漂移需重建，说明回填牵一发动全身）。
3. **风险等级更高**：表级越权是 E003 拦截 + 审计（可发现），行过滤一旦在 GENERATE/VALIDATE 遗漏就是跨店数据直接返回（静默越权），与「宁可说不确定，绝不返回看似正确的错误数据」的章程原则正面冲突。
4. **要启动的必要条件**：用户明确要进入商家市场 + 安全团队重新签字（推翻裁决三边界）+ 数仓团队承诺 `shop_id` 改造排期。三者缺一不做。

**替代方案（满足「先看看效果」）**：用 L0 实例级隔离给每个商家独立部署（每家一套 `dwd_shop`），在业务上等价于行级隔离的效果，成本 S~M；只有当「一家客户内部有多个店铺、要在同一会话切换」时才真正需要行级。

### 3.3 待确认④：数据新鲜度升 P0 → **架构侧拍板：是，作为 correctness 修复进首发，复用裁决一**

**最小改动清单（可落地）**：

| 层 | 文件:位置 | 改动 |
|---|---|---|
| 缓存层 | 新增 `app/services/partition_clock.py` | TTL（默认 10min，`PARTITION_CACHE_TTL_SECONDS`）缓存 `anchor: date`；首次惰性加载、失败回退 `today-1` 并告警一次；worker 每小时兜底刷新（复用 `worker/inspection_worker.py`） |
| 探针层 | `app/services/data_boundary.py:67-99` | 抽出 `latest_dt()` 单表 `SELECT MAX(dt)`（只查订单事实表），`probe()` 与 clock 共用，避免两处口径分裂 |
| 时间语义 | `app/core/time_window.py:35-59` | `latest_partition(today, anchor=None)` 等相对窗口分支用 anchor；**显式「昨天/指定日期/区间」恒用日历 today** |
| 状态机 | `app/core/statemachine.py:402-412` | PRESENT result 事件增 `data_as_of=anchor.isoformat()` + `data_as_of_basis="dwd_order_detail_di.max(dt)"` |
| 网关 | `app/api/routes_chat.py:133-166` | **无需改代码**：result 事件是 `yield sse(event)` 直接透传；可选在 `tr.finish` metadata（`:140-143`）补 `data_as_of` 便于 Langfuse 追踪 |
| 前端 | `App.vue:2187` / `:963` | 时间窗徽标旁加「数据截至 X」chip；`hasEvidence()` 纳入该字段 |
| 配置 | `app/config.py` | 新增 `partition_read_enabled` / `partition_cache_ttl_seconds` / `eval_fixed_anchor`，默认值 = 现状（可回滚） |
| 评测 | `eval/runner` + `EVAL_FIXED_ANCHOR` | 固定 anchor 保持相对时间用例确定性；`eval_golden` G001~G003 固定日期不受影响；**改动后仍需重跑两条基线归档新报告** |

**兼容性**：老结果事件无 `data_as_of` → 前端不显示，不报错；`PARTITION_READ_ENABLED=false` 完全回退现状。

**业务方需确认（唯一非技术项）**：「数据截止日」的口径 = 订单事实表 `MAX(dt)` 还是「本轮涉及表中最早的 `MAX(dt)`」。架构建议首发用前者并在 UI 标注基准表名（避免用户误以为是所有表的最新）；严谨版取 `MIN(MAX(dt))` 作为二期增强。

### 3.4 交付形态对技术栈的影响 → **结论：抽象层已具备，形态切换成本低；但评测资产必须连带重评**

| 形态 | 技术影响 | 评判 |
|---|---|---|
| 私有化（推荐首发） | L0 物理隔离；白标走静态 `branding.json`（S-F02 静态版）；License 文件校验（S-B05）；compose 单节点（S-B04）；**无 `tenant_id` 贯穿成本** | ✅ 最短路径，与裁决五一致 |
| 公有云 SaaS | 必须 L1（§3.1 九面，L 级）；S-F03/S-F05/S-F06 才有意义；计量（S-B02）+ 对外 API（S-B03）成为刚需；多实例触发上次评审 P2-1 | ⚠️ 另立商业化地基迭代，不进首发 |
| 两者都要 | 先私有化跑通，再评估 SaaS；**共用代码库，用配置开关区分形态**，不要 fork | ⚠️ 用户拍板节奏 |

**模型侧补充（衔接上次评审裁决二）**：`app/llm/client.py:57-81` 的 openai 兼容注册表使私有模型替换在代码层是低~中成本；但 **prompt 与评测基线是模型相关资产**，换模型必须重跑 `eval_v1_seed`（50/50 基线）与 `recall_eval`，否则拿旧基线证明新模型。此项不随本评审拍板，仍挂 P0-1 书面裁决。

### 3.5 待确认⑤：演示链路合规边界 → **架构建议：出域裁决前只做站内脱敏演示，站外保持静态**

| 场景 | 是否允许 | 技术形态 |
|---|---|---|
| 站内演示 + 全脱敏固定数据集 + 公有云 LLM | ⚠️ 需安全确认「脱敏数据可否出域」 | 演示账号 + 独立脱敏库；问题经完整链路但数据无敏感字段；**不接生产 `dwd_shop`** |
| 站内演示 + `LLM_MOCK=true` | ✅ 可立即做 | 仅口径模板降级（`AGENTS.md §1`），取数能力演示受限，适合讲流程不适合讲效果 |
| 站外产品页接真实后端 | ❌ 裁决前不做 | 公网 HTTPS 只能访问 HTTPS 后端（`AGENTS.md §6` 末条），需部署 + 域名 + CORS + 证书，是 M 级且放大出域面 |
| 私有化模型 + 真实（未脱敏）数据演示 | ⚠️ 需私有化 GPU 资源立项 | 代码层兼容（裁决二），成本在资源侧 |

**需用户/安全拍板**：脱敏数据集能否出域给公有云 LLM；演示是否允许接生产数据。**架构侧默认**：裁决前站外产品页保持纯静态体验区（现状），站内演示用脱敏集 + MOCK 或私有模型。

---

## 4. 实施分组（S/M/L 分层）

> 原则：**低成本见效 → 商业化地基 → 二期**；依赖尽量只指向「定位裁决」这一组织决策，而非彼此串联。S/M/L 为单条工作量，非迭代总时长。

### 4.1 第一组：低成本见效迭代（S 为主，建议 1~2 周完成）

| 缺口 | 档 | 一句话 |
|---|---|---|
| **U-B01 + U-F05** | S~M | 数据新鲜度 correctness 修复：唯一必进首发的技术项（§3.3） |
| **U-F10** | S | 口径裁决/会话回收入口，后端 100% 就绪，性价比最高 |
| **U-F01** | S | onboarding 黄金公式引导（遵守禁弹窗约定） |
| **U-F09** | S | 拒答下一步行动按钮 |
| **U-F12 + S-F04** | S | 应用内帮助中心，**合并做一次** |
| **U-B06** | S | 电商口语别名补数据（从研发计划移出，作为数据维护） |
| **U-F02（本地版）** | S | 常用问法收藏 localStorage |
| **S-F02（静态版）** | S | 白标配置化（私有化形态下无需 S-B01） |
| **S-F01（站内版）** | S | 站内演示入口 + 脱敏集（合规裁决前用 MOCK/私有模型） |
| **U-B07** | S~M | 记忆引用可见（只能说「参考了」） |
| **U-F03** | S | 澄清口径摘要前端排版（后端已有字段） |
| **S-B04（compose 实跑）** | S~M | 镜像构建 + 单节点起栈验证（K8s 实跑留 P3） |

**该组价值**：不改状态机、不破只读红线、不引入 tenant 概念，覆盖产品 4 个 P0 中的 3 个（U-F01/U-F05/U-F10）与 S-F01。

### 4.2 第二组：商业化地基迭代（M 为主，依赖「定位裁决」）

| 缺口 | 档 | 一句话 |
|---|---|---|
| **S-B01 L0** | S~M | 实例级物理隔离 + 全链路 `tenant_id` 埋点预留（**不是 L1**） |
| **S-B02（实例/部门计量）** | M | 配额/用量来自真实 `query_record`，不拿限流计数当账单 |
| **S-F05（最小用量页）** | S | 先展示个人/部门调用量，套餐部分冻结 |
| **U-B05** | M | 权限自助申请，复用 B13 审批框架 + 授权有效期 |
| **U-F11（阶段一）** | S~M | 通知中心聚合视图（巡检/导出/任务），零新后端表 |
| **U-F08** | M | 导出进度轮询 + 接通知中心 |
| **U-F04 + U-F07（静态版）** | M | 图表增强 + 规则化追问 chips |
| **S-B06（Grafana/Loki 部署）** | M | 部署侧补验证，`/metrics` 无需改代码 |
| **U-B03（限定范围）** | M | 只缓存 INTENT 分类/meta_qa，缓存键含权限+tenant 维度 |

### 4.3 第三组：二期 / 需重新拍板（L 级或依赖冲突裁决）

| 缺口 | 档 | 触发条件 |
|---|---|---|
| **S-B01 L1** | L | 明确定位为公有云 SaaS，且接受 9 面贯穿改造 |
| **S-F03 / S-F05（完整版）/ S-F06** | M~L | S-B01 L1 落地后；L0 形态下 S-F03 取消、S-F06 退化为设置页+看板 |
| **U-B02 多数据源** | L | 交付形态明确 + 源侧只读账号就绪；限定同构 MySQL 起步 |
| **U-B04 异步长查询** | L | 先做 B13 + 阈值治理的 S 级替代仍不满足时 |
| **U-B08 / S-B01-L2 行级隔离** | L+ 数仓项目 | 用户明确进入商家市场 + 安全重新签字 + 数仓排期（§3.2） |
| **S-B03 对外 API** | L | S-B01 L1 + S-B02 计量就绪；须设「不可绕过 validator」红线 |
| **S-B05 License** | M | 私有化形态确定；SaaS 用套餐替代 |
| **S-F01（站外接后端）** | M | 出域合规裁决通过 + 部署域名/证书 |
| **S-B04（K8s 实跑）** | S~M | 日活>500（裁决五触发线），并先完成上次评审 P2-1 |
| **S-B06（客户级 SLA 报表）** | L | S-B01 L1 后 |

---

## 5. 共享约定 / 风险提示（Engineer 必读）

### 5.1 不可破的红线

1. **全程只读 + 先口径后取数**：U-B02/U-B04/S-B03 的设计**不得绕过 `validator.py` 表白名单 + 只读账号双防线**（`AGENTS.md §4.2-4`）；对外 API 尤其不能给 ISV 直连数仓的通道。
2. **召回 0 命中 / 无口径不臆造**：U-F09 按钮与 U-F07 chips 触发的追问必须走完整意图识别，不得当作可信输入直出 SQL（`AGENTS.md §4.2-2`）。
3. **确定性时间语义**：`data_as_of` 只作为「展示与缓存键」注入；**显式日期恒按日历 today**，不随 anchor 漂移（裁决一 + `AGENTS.md §4.2-10`）。
4. **视觉约定**：所有前端新增项遵守 `AGENTS.md §4.2-6`——不用弹窗、不用常驻大段小字、去渐变/光晕/彩色阴影、字重≤600、单一强调色、4px 网格。

### 5.2 `tenant_id` 贯穿约定（首发埋点、L1 生效）

- **首发埋点**：所有新增 DB 表/JSON 结构带 `tenant_id` 列/字段，默认 `"default"`；`PipelineContext`（`statemachine.py:60-94`）加 `tenant_id` 参数默认 `"default"`；Redis key 采用 `dp:{tenant}:{...}` 格式预留段。**只埋点、不实现切换器**。
- **同名账号**：`User.username` 当前全局 unique（`models.py:17`），L1 时必须改复合唯一键 `(tenant_id, username)`；`db_access`（按 username）与 `token_store` 同步带 tenant，否则跨租户串权。
- **JSON 状态**：`shared_json.py` 是全文件 read-modify-write（`:166-177`），**多租户共用同一文件必然串味**；L1 采用 `data/{tenant_id}/*.json` 分目录，禁止仅在记录里加字段而消费方忘记过滤。
- **缓存键**：任何新增缓存的键必须包含 `tenant_id + user_id + 权限快照 + data_as_of + 口径/记忆版本`；缺一项即越权串味（详见 U-B03）。
- **审计/可观测**：`audit_log`、`query_record`、`/metrics` label、Langfuse trace 均需带 tenant（label 基数需控制，避免按用户打 label）。

### 5.3 兼容性与验证约定

- **SSE 事件**：沿用 `data: {json}\n\n`（`routes_chat.py:46-47`）；新增 `data_as_of` / `memory_notes` 均为**可选字段**，老客户端忽略即可，不得改变既有事件类型与字段名。
- **配置开关**：所有新增能力（`partition_read_enabled`、cache 开关等）必须有默认值且默认=现状，保证一键回滚（`AGENTS.md §5` / 上次评审 §5）。
- **评测**：任何触及召回/生成/时间窗的改动，必须重跑 `eval_v1_seed`（50/50 基线）+ `eval_golden` + `recall_eval`（97.5%/92%）并归档报告；相对时间用例用 `EVAL_FIXED_ANCHOR` 隔离；golden 新增必须带 `expect` 断言（`AGENTS.md §6`）。
- **旁路不阻塞**：审计/通知/计量写失败只 `logger.warning`、60s 内不重试，绝不影响主链路（`AGENTS.md §4.2-8`）。
- **文档同步**：新功能先登记 `docs/DataPro-功能清单.md`（含变更登记），改完同步 `AGENTS.md` 对应章节（`AGENTS.md §9`）；本次评审涉及的功能编号建议统一用 `U-*/S-*` 前缀，避免与既有 `F/B/A/D/S/E/I` 编号混淆。
- **验证命令**：Windows 全路径 Python `C:\Program Files\Python314\python.exe`；前端构建先 `$env:TEMP='e:\Project\AgentProject\datapro\.tmp'; $env:TMP=$env:TEMP; npm run build`；PowerShell 5 不支持 `&&`；无 Docker 的验证转 CI/有 Docker 机器。

### 5.4 「已实现却被列为缺口」的纠正清单（避免重复造轮子）

| 产品条目 | 纠正 | 一手证据 |
|---|---|---|
| U-F03 澄清口径说明 | 后端已下发定义摘要，属前端展示 | `caliber_ambiguity.py:62-65` `clarify_text()`；`statemachine.py:232-233` |
| U-F06 SQL 人话解释 | 依据区已有 SQL/口径/假设/`explanation`，先评估模板化即可 | `statemachine.py:404-405`；`App.vue:2187-2195` |
| U-F08 导出进度 | B13 申请/审批/状态/下载完整，缺的只是「触达」 | `AGENTS.md §4.1 B13`；`routes_exports.py:59-64,92-102` |
| U-F02 常用问法收藏 | F38 是管理员预设回复，不是个人收藏——**这条是真缺口**，不可用 F38 交付 | `AGENTS.md §4.1 技能包(F38)`；`routes_skills.py` |
| U-B07 记忆引用可见 | 记忆注入已实现，只需回显引用条目 | `routes_chat.py:94`；`AGENTS.md §4.1 A11` |
| S-B06 运营埋点 | `/metrics` 已实现，缺的是 Grafana/Loki 部署与聚合，不是新埋点系统 | `AGENTS.md §4.1 I03`；上次评审裁决四 |
| S-B04 交付自动化 | Dockerfile/compose/K8s YAML 已编写并通过语法校验，只差实跑 | `AGENTS.md §5/§6`；上次评审裁决五 |
| S-F01 演示链路 | 站外产品页**已存在**（纯静态），缺口是「链路打通」不是「没有产品页」 | `AGENTS.md §6` 末条 |
| U-B02 多数据源 | F27 是会话级临时表（`dp_uploads`），**不算持久数据源** | `AGENTS.md §2/§4.1 F27` |

### 5.5 待核实项（评审中无法从现有材料确认，不做臆断）

- `app/llm/client.py` 对私有模型 JSON mode 的兼容程度：上次评审已判定「低~中」，但**具体私有模型选型未定，无法核实**，随 P0-1 裁决后再评。
- U-B06 口语别名的实际召回提升幅度：**需补数据后跑 `recall_eval` 才能量化**，不预承诺数值。
- S-F02 白标中客户自定义主题色是否会破坏 `AGENTS.md §4.2-6` 视觉基线：**需设计评审**，架构侧建议限制在单一强调色变量内。
- 数仓 `dwd_shop` 是否已有任何隐藏的行级权限设施：上次评审裁决三已记「文档未记载该设施存在」，**若安全团队确认存在则 U-B08 成本需重估**。

---

## 6. 结论交付

本评审已完成产品 32 条缺口（任务简报记 31 条，按产品文档逐条计数更正）的全覆盖技术判断。核心结论三句话：

1. **首发只做两件技术事**：U-B01/U-F05 数据新鲜度 correctness 修复 + S-B01 的 L0 物理隔离与 `tenant_id` 埋点；其余低成本项按 §4.1 批量补齐。
2. **三个「看起来是 P0 但工作量巨大」的降级/换方案**：S-B01 应用层 L1 换 L0（保 P0 目标、降实现）、S-F03 多租户切换器冻结（L0 下取消）、U-B02 多数据源降 P2；反过来 **U-F10 零后端开发应最高优先落地**，U-B06 只是补数据应从研发计划移出。
3. **两个必须用户拍板才能解锁的技术决策**：定位与交付形态（决定 S-B01 层级）、演示数据出域合规（决定 S-F01 形态）；行级隔离由架构侧拍板维持不做，如推翻需安全签字 + 数仓排期。
