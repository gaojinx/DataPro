# DataPro Tool Schema 与接口规范

| 文档信息 | 内容 |
|---|---|
| 版本 | v0.1（骨架） |
| 上游文档 | DataPro-架构设计HLD.md |
| 状态 | 待评审 |

---

## 1. 目的

统一定义两类契约：
1. **LLM Tool Schema**：暴露给模型的工具定义（function calling 格式）
2. **内部模块接口**：状态机各节点间的调用契约
3. **对外 REST API**：前端/管理端接口
4. **统一错误码**：全服务复用

## 2. LLM Tool Schema 规范

### 2.1 通用约定

- 遵循 OpenAI function calling 格式（兼容 DeepSeek/Qwen）
- 所有工具返回 JSON，必须包含 `status` 字段（`ok` / `error`）
- 参数必须全部显式声明，禁止 `additionalProperties`
- 描述文本用中文，直接影响生成质量

### 2.2 工具清单（MVP 4 个）

```json
[
  {
    "type": "function",
    "function": {
      "name": "search_schema",
      "description": "根据用户问题检索相关的数据表、字段及其业务口径。返回表结构、字段说明和关联口径。生成 SQL 前必须先调用本工具。",
      "parameters": {
        "type": "object",
        "properties": {
          "query": { "type": "string", "description": "用户数据需求的中文描述" },
          "top_k": { "type": "integer", "description": "返回表数量上限，默认 5" }
        },
        "required": ["query"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "get_caliber",
      "description": "查询业务指标的精确定义和计算口径。当问题涉及指标（如活跃用户、GMV）时必须调用，禁止凭记忆回答口径。",
      "parameters": {
        "type": "object",
        "properties": {
          "metric_name": { "type": "string", "description": "指标中文名，如：日活跃用户数" }
        },
        "required": ["metric_name"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "validate_sql",
      "description": "对生成的 SQL 执行静态校验：语法、写操作检测、表白名单、字段存在性、扫描量预估。返回每项检查结果。",
      "parameters": {
        "type": "object",
        "properties": {
          "sql": { "type": "string", "description": "待校验的 SQL 语句" }
        },
        "required": ["sql"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "execute_readonly_sql",
      "description": "执行一条只读 SELECT 查询并返回结果（自动截断至 200 行）。仅可执行已通过 validate_sql 的语句。",
      "parameters": {
        "type": "object",
        "properties": {
          "sql": { "type": "string", "description": "已通过校验的 SELECT 语句" },
          "reason": { "type": "string", "description": "本次查询意图，写入审计日志" }
        },
        "required": ["sql", "reason"]
      }
    }
  }
]
```

### 2.3 约束

- 状态机按节点控制可用工具：RECALL 阶段只暴露 `search_schema`/`get_caliber`；VALIDATE 只暴露 `validate_sql`——**不允许模型在同一轮自由选择工具**
- 模型输出必须是结构化 JSON（`sql` + `explanation` + `assumptions`），非结构化输出直接判生成失败

## 3. 内部模块接口契约

| 调用方 | 接口 | 入参 | 出参 |
|---|---|---|---|
| 编排 → 召回 | `recall(query, top_k)` | 问题文本 | `{tables: [TableSchema], calibers: [Caliber], score, hit: bool}` |
| 编排 → 生成 | `generate_sql(question, schemas, calibers, history, retry_errors?)` | 上下文 | `{sql, explanation, assumptions} / None` |
| 编排 → 校验 | `validate(sql)` | SQL | `{syntax_ok, write_op, tables_allowed, columns_exist, scan_cost, passed, errors[]}` |
| 编排 → 执行 | `execute(sql, user_ctx)` | SQL + 用户身份 | `{rows, columns, row_count, truncated}` |
| 编排 → 审计 | `audit(trace_id, node, payload)` | 异步，fire-and-forget | 无（失败仅记日志不重试阻塞） |

**强约束**：执行引擎只接受 `validated=true` 的 SQL（内存态传递校验凭证，防止绕过校验直接执行）。

## 4. 对外 REST API

### 4.1 对话主接口

```text
POST /api/v1/chat          (SSE 流式)
Request:  { session_id, message }
Events:   { type: "node", node: "RECALL", ... }        # 过程可见
          { type: "clarify", question: "..." }          # 追问
          { type: "result", sql, columns, rows, row_count }
          { type: "reject", reason_code, message }
          { type: "escalate", ticket_id }
          { type: "error", code, message }
```

### 4.2 其他接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/v1/sessions` | GET/POST/DELETE | 会话管理 |
| `/api/v1/queries/{id}/feedback` | POST | `{rating: up/down, reason?}` |
| `/api/v1/queries/{id}/export` | POST | 导出申请，返回任务状态 |
| `/api/v1/inspections` | GET | 巡检结果列表 |
| `/admin/metadata/*` | CRUD | 元数据/口径维护（RBAC：仅数据工程角色） |
| `/admin/eval/run` | POST | 触发评测，返回报告 ID |

## 5. 统一错误码

| code | HTTP | 场景 | 用户话术方向 |
|---|---|---|---|
| `E001` | 400 | 问题过于模糊（澄清 2 轮仍失败） | 引导用户补充表名/时间范围 |
| `E002` | 404 | 召回 0 命中/口径缺失 | "未找到相关数据口径，请联系数据团队" |
| `E003` | 403 | 越界请求（写操作/权限外表） | 明确负面清单提示 |
| `E004` | 422 | SQL 校验不通过（2 次修正后） | 展示具体校验错误 |
| `E005` | 408 | LLM 超时 | 建议稍后重试 |
| `E006` | 503 | 数据源不可用 | 建议联系管理员 |
| `E007` | 429 | 限流 | 提示稍后重试 |
| `E010` | 500 | 未预期异常 | 转人工并附 trace_id |

## 6. 变更记录

| 日期 | 版本 | 变更 | 变更人 |
|---|---|---|---|
| 2026-11-20 | v0.1 | 初始骨架 | — |
