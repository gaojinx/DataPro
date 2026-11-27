# DataPro 前端遗留项 — 后端配套

| 文档信息 | 内容 |
|---|---|
| 产出目的 | 前端侧遗留的降级版/冻结项中，**需要后端配套**的部分整理成可独立实施的任务书，供另一开发窗口直接开工 |
| 上游依据 | 《DataPro-缺口分析-前端.md》《DataPro-缺口分析-后端.md》《DataPro-页面体验小功能-后端.md》；前端现状见 `datapro/frontend/src`（favorites/notices/branding/usage 均为本地或聚合实现） |
| 核对依据 | `app/db/models.py`（表清单）、`alembic/versions`（head = `0017_secret_ref`）、`app/services/permissions.py`（11 权限点）、`app/services/export_registry.py`（pending→approved/rejected）、`app/services/webhook.py`（飞书/钉钉/企微 payload）、`worker/inspection_worker.py`、`app/services/schedule_runner.py`（ok/rejected/failed）、`app/services/query_cache.py`（只缓存意图与口径问答） |
| 状态 | 供实施；**实现前先按 §7 在《DataPro-功能清单.md》登记（F64~F68）** |

> **一句话结论**：5 件事可做——收藏跨端（新表）、追问 chips LLM 化（result 事件加 1 字段）、通知中心阶段二（新表 + 4 个写入点）、品牌白标升级（SharedJson + 2 接口）、月度计量（复用 query_record）；多租户/交付控制台/站外演示维持冻结，不在本文范围。

---

## 0. 通用工程约定（每一项都要遵守）

| 约定 | 内容 |
|---|---|
| 迁移 | 业务库 Alembic，**head 现为 `0017_secret_ref`**，新表从 `0018` 顺延；`init_db()` 先迁数据源库、再迁业务库，勿动两棵链的顺序 |
| 权限 | 新接口挂权限点：本人数据接口要求登录即可（参照 `routes_access.py` 的 catalog 公开做法需说明理由）；管理接口用 `require_admin` 或既有权限点，**不新增权限点**除非确有必要 |
| 数据隔离 | 一切按 `username` 过滤；**删除用户时同步清理其数据**（参照 `db_access`/`schedule_registry` 的 `delete_by_owner` 模式，失败只记日志不阻断） |
| 错误 | 统一 `code/message/detail/trace_id`（`app/core/errors.py`），禁止裸 500；E007 用于限流语义，勿挪用 |
| 缓存红线 | 取数结果永不缓存（`query_cache.py` 既有约束）；新接口如需缓存只能缓存**元数据/配置类**并带身份指纹 |
| 审计 | 写操作（POST/PUT/DELETE）经 `audit` 服务留痕；读操作不强制 |
| 测试 | 每项 ≥2 项单测：①权限隔离（他人数据 404/403）②正常路径；放 `tests/`，mock 模式可跑 |
| 登记 | 实现同批次更新 `docs/DataPro-功能清单.md`（功能行 + 变更登记）与 `AGENTS.md §4.1` |

---

## 1. B-F02 收藏跨端（对应前端 U-F02，登记建议 F64）

**现状**：前端常用问法存 `localStorage`（`App.vue` 的 `favorites`/`favKey`，上限 20 条），换浏览器/换端丢失。

**存储**：新表 `favorite_question`（Alembic `0018_favorite_question`）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | Integer PK | |
| username | String(64) idx | 所属用户 |
| question | String(120) | 原文，前后去空格 |
| qkey | String(120) | 归一化键（去空白/转小写），与 username 联合唯一 |
| created_at | DateTime server_default=now() | |

**接口**（均要求登录，仅本人数据）：

| 方法/路径 | 行为 |
|---|---|
| `GET /api/v1/favorites` | 本人收藏，按 created_at 倒序，≤20 条 |
| `POST /api/v1/favorites` | body `{question}`；去空格、≤120 字；命中 `(username, qkey)` 去重（幂等返回已有）；超过 20 条挤掉最旧 |
| `DELETE /api/v1/favorites/{id}` | 仅本人，他人 id 返回 404 |

**前端对接点**：`App.vue` `loadFavorites/saveFavorite/removeFavorite` 改调接口；localStorage 可保留为离线缓存但以接口为准。`POST /api/v1/chat/feedback` 类似路径风格保持一致。

**验收**：同账号换端可见同一份收藏；重复添加幂等；删除用户后收藏清空；≥2 项单测。

**工作量**：S。

---

## 2. B-F07 追问 chips LLM 化（对应前端 U-F07，登记建议 F65）

**现状**：前端 `suggestionsFor(msg)` 按 IR 规则静态生成追问。

**方案**：在 PRESENT 阶段由 LLM 基于本轮「问题 + 指标 + 时间窗 + 结果摘要」生成 ≤3 条追问，随 result 事件下发：

```
result 事件新增字段：followups: ["按渠道拆一下", "上周对比", ...]   # 可为 None
```

实现要点：
- prompt 新文件 `prompts/followups.txt`（或并入 answer prompt）；JSON 输出 `{ "followups": [...] }`，复用 `llm/client.py` 的 JSON 容错；
- **降级链**：`LLM_MOCK=true` / 无可用模型 / 超时 / 解析失败 → 不下发字段，前端继续用静态规则（`App.vue suggestionsFor` 已是兜底，前端无需改逻辑，只在有字段时优先展示）；
- **不缓存**：追问随取数结果生成，属结果的一部分，`query_cache` 红线不变（取数结果永不缓存）；
- 追问文本只是建议，点击仍走完整意图识别与多轮槽位继承——不得在后端预执行或直出 SQL。

**验收**：真实模型下 result 事件含 ≤3 条与当轮指标相关的追问；mock/失败时字段缺省；评测 50 条种子集指标不回退（追问生成不改变取数链路）；≥2 项单测（字段透传 + 失败降级）。

**工作量**：S~M（prompt + 1 处状态机接线 + 事件字段）。

---

## 3. B-F11 通知中心阶段二（对应前端 U-F11 + U-F08，登记建议 F66）

**现状**：前端顶栏铃铛聚合巡检/导出/任务/权限申请记录，60s 轮询、已读存本地；审批无主动触达。

**存储**：新表 `notification`（Alembic `0019_notification`）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | Integer PK | |
| username | String(64) idx | 接收人 |
| kind | String(20) | inspection / export / schedule / access / system |
| title | String(80) | |
| body | String(200) | |
| link | String(120) | 前端跳转用（如 `settings:access`） |
| created_at | DateTime server_default=now() idx | |
| read_at | DateTime NULL | 未读判定 |

**写入点**（全部幂等：同一事件重复触发不产生重复通知，可用 `(username, kind, ref_id)` 去重或写入前查重）：

| 来源 | 时机 | 接收人 |
|---|---|---|
| `export_registry` 审批 | approved / rejected 时 | 申请人 |
| `access_requests` 审批 | approve / reject 时 | 申请人 |
| `worker/inspection_worker.py` | 巡检失败（fail 项） | 全体管理员（username 逐个写） |
| `schedule_runner` | status ∈ {rejected, failed} | 任务 owner |

**接口**：

| 方法/路径 | 权限 | 行为 |
|---|---|---|
| `GET /api/v1/notifications?unread=1&limit=50` | 登录 | 本人通知，created_at 倒序 |
| `POST /api/v1/notifications/read` | 登录 | body `{ids?: [...], all?: bool}`；只作用于本人 |

**可选触达**：审批结果复用 `app/services/webhook.py` 推 IM，新配置 `NOTIFY_WEBHOOK_URL`（默认空=关闭），格式与巡检告警一致。

**保留策略**：worker 每日清理 `read_at` 超过 30 天或 `created_at` 超过 90 天的记录（复用 retention 模式，`NOTIFICATION_RETENTION_DAYS`，0=不清）。

**前端对接点**：`App.vue loadNotices` 改读接口（保留 60s 轮询与角标）；`noticeReadAt` 本地已读逻辑废弃，改 `POST read`。

**验收**：审批一次导出 → 申请人收到一条通知且跨端已读一致；巡检失败 → 管理员可见；未读角标以服务端为准；同一审批重复回调不产生重复通知；≥3 项单测（隔离、写入去重、已读）。

**工作量**：M（表 + 4 个写入点 + 接口 + 清理）；可拆两批：先表+导出/权限申请写入点（S），再巡检/定时任务（S）。

---

## 4. B-S02 品牌白标升级（对应前端 S-F02，登记建议 F67）

**现状**：前端读静态 `public/branding.json`，改品牌要重新构建/改文件。

**方案**：`data/branding.json` 改由 `SharedJson` 管理（`data/` 已 gitignore），提供：

| 方法/路径 | 权限 | 行为 |
|---|---|---|
| `GET /api/v1/branding` | 公开 | 返回 `{product_name, logo_text, version, accent}`；文件缺失/字段非法时返回缺省 DataPro（与前端静态 fallback 同构） |
| `PUT /admin/branding` | `require_admin` | 全量覆盖；校验：accent 匹配 `^#[0-9a-fA-F]{6}$`、product_name ≤20 字、logo_text ≤4 字、version ≤10 字；非法字段 400 带错误码 |

要点：
- 纯文本字段，无 HTML/脚本面；不提供 logo 图片上传（后续再说）；
- `GET` 可带 `Cache-Control: max-age=60`，前端 `loadBranding` 优先接口、失败回落静态 `branding.json`（现有逻辑已是 fallback 结构，只需改数据源）；
- 不做多租户：一份部署一份品牌，**不做** `/branding?tenant=` 这类接口（那是 S-F03 解冻后的事）。

**验收**：管理员改品牌 → 前端免重启生效（刷新即可）；缺省即 DataPro；非法 accent 被拒；≥2 项单测（校验拒绝、公开可读）。

**工作量**：S。

---

## 5. B-S05 月度计量（对应前端 S-F05，登记建议 F68）

**现状**：用量页读 `GET /api/v1/usage/summary`（近 30 天/今日/结果分布/趋势），无月份口径、无上限。

**已拍板（2026-11-26）**：`QUOTA_MONTHLY` 超限**仅提示、不拦截**——用量页展示「本月 N / 上限 M（已超）」徽标，取数链路不受影响；后续如需收紧再单独立项（届时走 E007 语义并提示联系管理员调整）。

**方案**（不新建事件表，`query_record` 是唯一事实源）：

| 方法/路径 | 权限 | 行为 |
|---|---|---|
| `GET /api/v1/usage/monthly` | 登录 | 本人近 6 个月按月聚合 `{months: [{month, total, by_outcome}], this_month, quota, exceeded}`；`admin&all=1` 看全量（与 usage/summary 同参数风格） |

- `QUOTA_MONTHLY`（默认 0=不限，`app/config.py` 新增）；`exceeded` 仅在 quota>0 且本月 total≥quota 时为 true；**exceeded=true 只用于前端提示，任何路径都不得拦截取数**；
- **红线**：B14 的 Redis 限流计数不是计量，不得拿来当账单；计量只认 `query_record` 落库数据；
- 前端用量页加「本月 N / 上限 M」行（quota=0 显示「不限」；超限时显示提示徽标「本月额度已用完，仍可继续使用，如需调整请联系管理员」），不改其他结构。

**验收**：跨月聚合正确（按本地月界）；admin all=1 隔离正确；quota=0 恒不超限且 exceeded=false；超限时接口仍正常返回数据、仅 exceeded=true（不返回 4xx/5xx）；≥2 项单测。

**工作量**：S。

---

## 6. 维持冻结项（本窗口**不做**，防误开工）

| 项 | 冻结原因 | 解冻前提 |
|---|---|---|
| S-F03 多租户切换器 | L0 一客户一部署，概念不存在 | S-B01 多租户定位裁决 |
| S-F06 交付控制台 | 依赖租户/计量/健康度数据面 | S-B01 + S-B02 + S-B06 |
| S-F05 完整套餐/计费 | 内部工具无套餐概念 | 商业化定位裁决 |
| S-F01 站外真实后端演示 | 数据出域未裁决 | 出域裁决 + HTTPS 域名/CORS |
| X-A01 字号三档 | 前端改造项，与后端无关 | （前端窗口单独立项） |

---

## 7. 实施顺序与登记对照

| 建议顺序 | 项 | 登记号 | 工作量 | 依赖 |
|---|---|---|---|---|
| 1 | B-S02 品牌白标升级 | F67 | S | 无（纯后端 + 前端小改） |
| 2 | B-F02 收藏跨端 | F64 | S | 无 |
| 3 | B-S05 月度计量 | F68 | S | 已拍板：超限仅提示不拦截（2026-11-26） |
| 4 | B-F11 通知中心（先导出/权限申请批） | F66 | S→M | 无 |
| 5 | B-F07 追问 chips LLM 化 | F65 | S~M | 无（纯后端字段，前端自动兼容） |
| 6 | B-F11（巡检/定时任务批 + IM 触达） | F66 | S | 第 4 步 |

> 每完成一项：更新《DataPro-功能清单.md》（功能行 + 变更登记）→ `AGENTS.md §4.1` 补一条 → 跑 `pytest -q` → 通知前端窗口对接（本文每节已标注「前端对接点」）。
