# DataPro 架构设计文档（HLD）

| 文档信息 | 内容 |
|---|---|
| 版本 | v0.1（骨架，G1 评审后定稿） |
| 上游文档 | DataPro项目章程.md v0.1 |
| 责任人 | 后端 Lead |
| 状态 | 待评审 |

---

## 1. 设计目标与约束

- **正确性优先**：宁可拒答，不返回错误数据（章程 §2.2）
- **只读**：全链路对业务库零写操作
- **权限透传**：以登录用户身份查询，Agent 不自建权限体系
- **可审计**：每个决策节点留痕，可回放归因
- **框架最小化**：FastAPI + 自研状态机 + 原生 LLM SDK

## 2. 总体架构

```text
┌─────────────────────────────────────────────────────┐
│                    前端 Vue3 SPA                      │
│        对话界面 / 结果表格 / 导出 / 反馈入口            │
└──────────────────────┬──────────────────────────────┘
                       │ HTTPS (JWT)
┌──────────────────────▼──────────────────────────────┐
│                 API 网关层 (FastAPI)                  │
│    认证 / 限流 / 会话管理 / SSE 流式返回               │
├─────────────────────────────────────────────────────┤
│                编排层：取数状态机                      │
│  澄清 → 召回 → 生成 → 校验 → 执行 → 呈现              │
│       ↘ 拒答          ↘ 转人工                       │
├──────┬──────┬──────┬──────┬──────┬──────────────────┤
│ 召回  │ 生成  │ 校验  │ 执行  │ 巡检  │   审计模块        │
│ 服务  │ 服务  │ 器   │ 引擎  │ 调度  │  (旁路,不阻塞)    │
├──────┴──────┴──────┴──────┴──────┴──────────────────┤
│                     基础设施层                         │
│  MySQL(业务/审计)  Milvus(向量)  Redis(缓存)  LLM API  │
└─────────────────────────────────────────────────────┘
                       │ 只读账号 + 用户身份透传
              ┌────────▼────────┐
              │   MySQL 数仓     │
              └─────────────────┘
```

## 3. 模块划分

| 模块 | 职责 | 关键设计点 |
|---|---|---|
| API 网关层 | 认证、限流、会话 CRUD、SSE 推流 | JWT 认证；单用户并发限 3；会话上下文存 Redis（TTL 30min） |
| 状态机编排 | 驱动取数流程，节点间传递上下文 | 见 §4；每个节点超时可配；节点失败有明确降级路径 |
| 召回服务 | Schema/口径/历史问答混合召回 | 向量(Milvus) + 关键词(MySQL FULLTEXT) 双路召回 → Rerank 精排 → Top-K |
| 生成服务 | 调 LLM 生成 SQL / 元数据问答 | openai 兼容 SDK；结构化输出（JSON mode）；带 retry ≤2 |
| SQL 校验器 | 执行前静态检查 | sqlglot 解析 AST：禁写操作、表白名单、字段存在性、扫描量预估、强制 LIMIT |
| 执行引擎 | 只读连接池执行查询 | 独立连接池；查询超时 10s；行数阈值截断；以用户身份执行 |
| 结果呈现 | 摘要 + 表格 + 导出任务 | >1万行只回摘要，导出走人工审批任务队列 |
| 巡检调度 | 核心表日级巡检 | APScheduler 定时任务；规则可配（空值率/断流/行数波动）；告警走 IM webhook |
| 审计模块 | 全链路留痕 | 旁路异步写，不阻塞主流程；trace_id 贯穿 |

## 4. 状态机设计（核心）

### 4.1 状态定义

```text
INTENT        意图识别        → 元数据问答 / 取数 / 巡检 / 越界请求
CLARIFY       澄清追问        → 最多 2 轮，仍模糊则拒答
RECALL        Schema 召回     → 失败(0 命中) → REJECT
GENERATE      SQL 生成        → 失败 retry≤2 → ESCALATE
VALIDATE      SQL 校验        → 任一检查不过 → 回 GENERATE(带错误信息) 或 REJECT
EXECUTE       只读执行        → 超时/报错 → GENERATE(重试1次) → ESCALATE
PRESENT       结果呈现        → 正常结束
REJECT        明确拒答        → 输出原因（越界/无权限/口径缺失/过于模糊）
ESCALATE      转人工          → 生成工单，附完整上下文
```

### 4.2 转移图

```text
INTENT ──取数──► CLARIFY ──清楚──► RECALL ──命中──► GENERATE
   │                │仍模糊           │0命中            │
   │                ▼                ▼                ▼
   │             REJECT           REJECT          VALIDATE
   │                                                │  │
   │                                     通过───────┘  └不过(≤2次)
   │                                      ▼               ▼
   │                                   EXECUTE        GENERATE/REJECT
   │                                      │
   ├─问答──► 直接生成回答（走 RECALL+生成服务，不走校验/执行）
   ├─巡检──► 展示巡检结果
   └─越界──► REJECT
```

### 4.3 关键规则

- **VALIDATE 不过的处理**：第 1 次把校验错误回传给 LLM 修正；第 2 次仍不过 → REJECT（说明原因），不硬重试
- **CLARIFY 最多 2 轮**：防止无限追问损伤体验
- **RECALL 置信度门槛**：Top-K 召回分数低于阈值 → 直接拒答"未找到相关数据口径"，禁止模型自由发挥
- **每次节点转移写审计**：节点名、输入摘要、输出摘要、耗时、token 消耗

## 5. 核心数据模型

```sql
-- 会话与消息
session(id, user_id, created_at, status)
message(id, session_id, role, content, trace_id, created_at)

-- 取数记录（供审计与"历史问答对"沉淀）
query_record(id, session_id, question, recalled_schema, generated_sql,
             validated bool, executed bool, row_count, latency_ms,
             final_answer, user_feedback, created_at)

-- 审计日志（异步写入）
audit_log(id, trace_id, user_id, node, input_digest, output_digest,
          sql_text, latency_ms, created_at)

-- 元数据（巡检与召回的数据源）
meta_table(id, db_name, table_name, cn_name, description, owner, dt_level)
meta_column(id, table_id, col_name, cn_name, data_type, caliber_id, is_pii)
metric_caliber(id, metric_name, definition, logic, version, status, updated_by, updated_at)
```

## 6. 对外接口概览

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/v1/chat` | POST(SSE) | 对话主接口，流式返回 |
| `/api/v1/sessions` | CRUD | 会话管理 |
| `/api/v1/queries/{id}/feedback` | POST | 结果反馈（👍/👎+原因），回评测集 |
| `/api/v1/queries/{id}/export` | POST | 大结果导出申请 |
| `/api/v1/inspections` | GET | 巡检结果查询 |
| `/admin/metadata` | CRUD | 元数据维护（内部） |
| `/admin/eval/run` | POST | 触发评测回归（内部） |

详细出入参、错误码见《DataPro-ToolSchema与接口规范》。

## 7. 非功能设计

| 维度 | 目标 | 手段 |
|---|---|---|
| 时延 | 取数 P90 ≤ 30s | 各节点超时：澄清5s/召回3s/生成20s/执行10s；LLM 流式输出 |
| 并发 | 50 同时在线 | FastAPI 异步 + 连接池上限 20 + 单用户限流 |
| 可用性 | 99.5% | LLM 超时降级为"稍后重试"；Milvus 不可用降级为关键词召回 |
| 安全 | 见权限设计 | 只读账号、脱敏拦截、prompt injection 防护（见测试方案） |
| 可观测 | 全节点可归因 | trace_id 贯穿；Prometheus 指标 + Loki 日志；LLM token 消耗按用户统计 |

## 8. 部署拓扑（MVP）

```text
单机 Docker Compose：
  api(FastAPI) ×2  |  worker(巡检) ×1  |  mysql  |  redis  |  milvus  |  grafana/prometheus/loki
扩量触发条件（迁移 K8s）：日活 > 500 或 QPS > 20
```

## 9. 待评审决策项

- [ ] 后端语言最终确认（Python vs 团队 Java 栈）
- [ ] LLM 供应商与部署方式（依赖安全评审）
- [ ] 向量库起步规模（PGVector vs Milvus）
- [ ] 前端框架确认（Vue vs React）
- [ ] 导出任务审批流程是否复用公司现有 OA

## 10. 变更记录

| 日期 | 版本 | 变更 | 变更人 |
|---|---|---|---|
| 2026-11-20 | v0.1 | 初始骨架 | — |
